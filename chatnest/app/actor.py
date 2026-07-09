"""Single-owner actor for one persistent Claude SDK client."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from claude_agent_sdk.types import (
    AssistantMessage as _AssistantMessage,
    UserMessage as _UserMessage,
    TextBlock as _TextBlock,
    ToolUseBlock as _ToolUseBlock,
    ToolResultBlock as _ToolResultBlock,
)
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import StreamEvent


logger = logging.getLogger(__name__)


class ActorBusyError(RuntimeError):
    pass


@dataclass
class TurnRequest:
    prompt: str
    options: ClaudeAgentOptions
    fingerprint: str
    timing_callback: Callable[[str], None] | None
    outbox: asyncio.Queue


class ConvActor:
    def __init__(self, conv_id: str, project_dir: str) -> None:
        self.conv_id = conv_id
        self.project_dir = project_dir
        self.last_active = monotonic()
        self.busy = False
        self.closed = False
        self._client: ClaudeSDKClient | None = None
        self._fingerprint: str | None = None
        self._session_id: str | None = None  # 当前连接正在跑的会话 id
        self._inbox: asyncio.Queue[TurnRequest | None] = asyncio.Queue()
        self._state_lock = asyncio.Lock()
        self._task = asyncio.create_task(
            self._run(),
            name=f"claude-actor-{conv_id[:8]}",
        )

    @property
    def alive(self) -> bool:
        return not self.closed and not self._task.done()

    def is_warm_for(self, fingerprint: str) -> bool:
        return (
            self.alive
            and self._client is not None
            and self._fingerprint == fingerprint
        )

    async def submit(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        fingerprint: str,
        timing_callback: Callable[[str], None] | None,
    ) -> asyncio.Queue:
        async with self._state_lock:
            if not self.alive:
                raise RuntimeError("Claude 会话连接已失效")
            if self.busy:
                raise ActorBusyError("上一条消息仍在回复")
            self.busy = True
        outbox: asyncio.Queue = asyncio.Queue()
        await self._inbox.put(
            TurnRequest(
                prompt=prompt,
                options=options,
                fingerprint=fingerprint,
                timing_callback=timing_callback,
                outbox=outbox,
            )
        )
        return outbox

    async def interrupt(self) -> None:
        """打断正在进行的回合（roll/新消息顶掉旧回合用）。"""
        client = self._client
        if client is None or not self.busy:
            return
        try:
            await client.interrupt()
        except Exception:
            logger.exception("interrupt failed for conv=%s", self.conv_id)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._inbox.put(None)
        await self._task

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("Persistent Claude client disconnect failed")
            finally:
                self._client = None
                self._fingerprint = None
                self._session_id = None

    async def _ensure_client(self, request: TurnRequest) -> None:
        if self._client is not None and self._fingerprint == request.fingerprint:
            # resume 语义：None=要求全新会话，具体 id=要求接着那个会话。
            # 持久连接只有在"它正是被要求的那个会话"时才能复用——否则必须
            # 真的断开重连。以前这里不看 resume：滚动压缩说"开新会话"被当
            # 耳旁风，摘要+原文全部塞回同一个越滚越大的老会话，token 永远
            # 降不下来，压缩每轮触发（2026-07-09 实锤 21.7 万 token 的会话）。
            requested = getattr(request.options, "resume", None)
            if requested is None and self._session_id is None:
                return  # 新连接还没跑过回合，本来就是全新会话
            if requested is not None and requested == self._session_id:
                return  # 要的就是当前会话，接着聊
            logger.info(
                "session switch for conv=%s: %s -> %s",
                self.conv_id, self._session_id, requested or "(new)",
            )
        await self._disconnect()
        client = ClaudeSDKClient(request.options)
        await client.connect()
        self._client = client
        self._fingerprint = request.fingerprint

    async def _handle_turn(self, request: TurnRequest) -> None:
        callback = request.timing_callback
        await self._ensure_client(request)
        if self._client is None:
            raise RuntimeError("Claude 会话连接失败")

        await self._client.query(request.prompt)
        first_sdk_event_seen = False
        first_text_token_seen = False
        got_streaming_text = False
        result_seen = False
        last_message_usage: dict | None = None
        async for sdk_message in self._client.receive_response():
            if not first_sdk_event_seen:
                first_sdk_event_seen = True
                if callback:
                    callback("sdk_first_event")
            if isinstance(sdk_message, SystemMessage):
                if sdk_message.subtype == "init":
                    initialized_cwd = sdk_message.data.get("cwd")
                    if initialized_cwd != self.project_dir:
                        logger.warning(
                            "cwd mismatch: sdk=%r expected=%r",
                            initialized_cwd, self.project_dir,
                        )
                        raise RuntimeError("会话恢复失败")
            elif isinstance(sdk_message, StreamEvent):
                event = sdk_message.event
                if event.get("type") == "message_start":
                    # 每次 API 调用开头都带这一次调用的真实上下文用量。
                    # 只留最后一次的——它才是"会话现在有多大"。
                    msg_usage = (event.get("message") or {}).get("usage") or {}
                    if msg_usage:
                        last_message_usage = msg_usage
                    continue
                if event.get("type") != "content_block_delta":
                    continue
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text and not first_text_token_seen:
                        first_text_token_seen = True
                        if callback:
                            callback("first_text_token")
                    got_streaming_text = True
                    await request.outbox.put({"event": "delta", "text": text})
                elif delta.get("type") == "thinking_delta":
                    # Fable 5 经旧版 CLI 会吐出全空的 thinking 块，
                    # 空串转发出去只会让前端画一条空白思考链
                    thinking_text = delta.get("thinking", "")
                    if thinking_text:
                        await request.outbox.put(
                            {"event": "thinking", "text": thinking_text}
                        )
            elif isinstance(sdk_message, (_AssistantMessage, _UserMessage)):
                for block in getattr(sdk_message, "content", []) or []:
                    if isinstance(block, _TextBlock) and not got_streaming_text:
                        text = block.text or ""
                        if text and not first_text_token_seen:
                            first_text_token_seen = True
                            if callback:
                                callback("first_text_token")
                        if text:
                            await request.outbox.put({"event": "delta", "text": text})
                    elif isinstance(block, _ToolUseBlock):
                        await request.outbox.put({
                            "event": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                    elif isinstance(block, _ToolResultBlock):
                        content = block.content
                        if isinstance(content, list):
                            content = "".join(
                                c.get("text", "") if isinstance(c, dict) else str(c)
                                for c in content
                            )
                        await request.outbox.put({
                            "event": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": content or "",
                            "is_error": bool(block.is_error),
                        })
            elif isinstance(sdk_message, ResultMessage):
                result_seen = True
                self._session_id = sdk_message.session_id
                # 上下文体积 = 本回合【最后一次】API 调用的 input + cache 读写。
                # 绝不能用 ResultMessage.usage 的合计——工具调用的回合里每一步
                # 都重读全部上下文，加总会虚高好几倍，曾把压缩逼成每轮触发。
                # 没捕获到 message_start 就报 0（宁可不触发，轮数兜底还在）。
                context_tokens = 0
                if last_message_usage:
                    for key in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ):
                        try:
                            context_tokens += int(last_message_usage.get(key) or 0)
                        except (TypeError, ValueError):
                            pass
                await request.outbox.put(
                    {
                        "event": "done",
                        "session_id": sdk_message.session_id,
                        "context_tokens": context_tokens,
                    }
                )
        if not result_seen:
            raise RuntimeError("Claude 连接提前结束")

    async def _run(self) -> None:
        try:
            while True:
                request = await self._inbox.get()
                if request is None:
                    break
                try:
                    await self._handle_turn(request)
                except Exception as exc:
                    await request.outbox.put(exc)
                    await self._disconnect()
                finally:
                    self.last_active = monotonic()
                    async with self._state_lock:
                        self.busy = False
                    await request.outbox.put(None)
        finally:
            await self._disconnect()
            self.closed = True
