import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from typing import Any
from starlette.formparsers import MultiPartParser

MultiPartParser.max_part_size = 60 * 1024 * 1024  # 与 uploads.py 的 MAX_FILE_BYTES 对齐

from app import auth
from app.actor import ActorBusyError
from app.claude import (
    PROJECT_DIR,
    SessionResumeError,
    available_models,
    stream_chat,
    summarize_conversation,
    summarize_thinking,
    summarize_tool_use,
    summarize_traces,
)
from app.codex_api import stream_codex_chat
from app.memory import (
    MAX_MEMORY_CHARS,
    add_saved_memory,
    import_claude_export_memories,
    read_diary_entries,
    read_memory,
    read_profile,
    write_memory,
    write_profile,
)
from app.memory_search import recall as recall_memory
from app.sessions import (
    remove_session,
    session_list,
    session_messages,
    set_session_starred,
    set_session_title,
)
from app.splash import current_period, random_line
from app.registry import configure_registry, get_registry
from app.store import (
    ConversationNotFound,
    begin_turn,
    complete_turn,
    conversation_messages,
    ensure_conversation,
    get_compaction_state,
    initialize_store,
    prepare_edit_turn,
    prepare_retry_turn,
    resolve_conversation,
    restore_branch,
    set_compaction_state,
)
from app.uploads import (
    remove_conversation_uploads,
    save_uploads,
    validated_attachments,
    validated_file,
)


logger = logging.getLogger(__name__)
timing_logger = logging.getLogger("uvicorn.error")
STATIC = ROOT / "static"
chat_lock = asyncio.Lock()
# 存活中的聊天泵任务（防 GC；断线后回合仍要跑完入库）
_pump_tasks: set[asyncio.Task] = set()
initialize_store()
TRACE_CONTENT_CHARS = 20_000


def trace_content(value: Any) -> str:
    text = str(value or "")
    if len(text) <= TRACE_CONTENT_CHARS:
        return text
    return text[:TRACE_CONTENT_CHARS] + "\n\n[output truncated]"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 会话目录必须跟 claude.py 里 SDK 的 cwd 一致（都以 AGENT_APP_ROOT 为准），
    # 否则 actor 的 cwd 校验会把每一轮对话都当成"会话恢复失败"
    registry = configure_registry(PROJECT_DIR)
    await registry.start()
    try:
        yield
    finally:
        await registry.stop()


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

AUTH_MODE = os.environ.get("AUTH_MODE", "app").strip().lower()
if AUTH_MODE not in {"app", "both"}:
    logger.warning("Unsupported AUTH_MODE=%r; falling back to app", AUTH_MODE)
    AUTH_MODE = "app"
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")
OUTER_AUTH_COOKIE = "claude_outer_auth"
OUTER_BASIC_AUTH_ENABLED = AUTH_MODE == "both" and bool(BASIC_AUTH_USER and BASIC_AUTH_PASSWORD)


def outer_auth_token() -> str:
    return hmac.new(
        os.environ["CHAT_SECRET"].encode(),
        f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}:outer-v1".encode(),
        "sha256",
    ).hexdigest()


def basic_auth_ok(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    return bool(sep) and hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(password, BASIC_AUTH_PASSWORD)


def request_is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


@app.middleware("http")
async def outer_basic_auth(request: Request, call_next):
    public_paths = (
        "/health",
        "/marked.min.js",
        "/favicon.ico",
        "/static/manifest.webmanifest",
        "/static/css/typography-locked.css",
        "/static/design-system.css",
    )
    if not OUTER_BASIC_AUTH_ENABLED or request.url.path in public_paths:
        return await call_next(request)
    token = request.cookies.get(OUTER_AUTH_COOKIE, "")
    if token and hmac.compare_digest(token, outer_auth_token()):
        return await call_next(request)
    if not basic_auth_ok(request.headers.get("authorization", "")):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Agent App"'},
        )
    response = await call_next(request)
    response.set_cookie(
        OUTER_AUTH_COOKIE,
        outer_auth_token(),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=request_is_https(request),
        samesite="lax",
    )
    return response


def require_auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


class AuthBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ChatBody(BaseModel):
    message: str = Field(default="", max_length=20_000)
    conversation_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    edit_message_id: int | None = Field(default=None, ge=1)
    retry_message_id: int | None = Field(default=None, ge=1)
    model: str = Field(default="claude-sonnet-4-6", max_length=64)
    effort: str = Field(default="medium", max_length=16)
    extended: bool = True
    attachments: list[str] = Field(default_factory=list, max_length=10)


class ToolCaptionBody(BaseModel):
    tool_name: str = Field(min_length=1, max_length=128)
    tool_input: Any = None
    tool_output: str = Field(default="", max_length=20000)


class MemoryBody(BaseModel):
    content: str = Field(max_length=MAX_MEMORY_CHARS)


class ProfileBody(BaseModel):
    fullName: str = Field(default="", max_length=200)
    nickname: str = Field(default="", max_length=200)
    savedMemories: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    preferences: dict[str, Any] = Field(default_factory=dict)
    claudeExportImport: dict[str, Any] = Field(default_factory=dict)
    updatedAt: int | None = None


class ThinkingSummaryBody(BaseModel):
    thinking: str = Field(min_length=1, max_length=50_000)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class StarBody(BaseModel):
    starred: bool


def render_context_prompt(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    if len(messages) == 1 and last["role"] == "user":
        prompt = last["text"]
    else:
        lines = []
        for message in messages:
            role = "User" if message["role"] == "user" else "Assistant"
            text = (message.get("text") or "").strip()
            attachments = message.get("attachments") or []
            if attachments:
                paths = "\n".join(
                    item.get("path", "")
                    for item in attachments
                    if item.get("path")
                )
                if paths:
                    text = f"{text}\n[attachments]\n{paths}".strip()
            lines.append(f"{role}: {text}")
        prompt = (
            "<conversation-context>\n"
            + "\n\n".join(lines[:-1])
            + "\n</conversation-context>\n\n"
            "Please continue from the context above and answer only the final "
            "user message below. Do not repeat the previous transcript.\n\n"
            f"User: {last.get('text', '').strip()}"
        )
    attachments = last.get("attachments") or []
    paths = "\n".join(
        item.get("path", "")
        for item in attachments
        if item.get("path")
    )
    if paths:
        prompt += (
            "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
            f"{paths}\n]"
        )
    return prompt


# 长对话滚动压缩（默认关闭，CHAT_COMPACT_AFTER_TURNS=0 即停用）：
# 单位是「轮」（一问一答=2 条消息），代码内部 ×2 换成消息数。
#   AFTER  ：对话超过这么多轮才启用压缩，短对话完全走原来的会话恢复路径
#   KEEP   ：无论怎么压，最近这么多轮永远保留原文
#   STEP   ：每积累这么多轮的新内容，就重新把旧内容折进摘要一次
COMPACT_AFTER_TURNS = int(os.environ.get("CHAT_COMPACT_AFTER_TURNS", "0"))
COMPACT_KEEP_TURNS = int(os.environ.get("CHAT_COMPACT_KEEP_TURNS", "8"))
COMPACT_STEP_TURNS = int(os.environ.get("CHAT_COMPACT_STEP_TURNS", "50"))


def _render_transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        role = "用户" if message["role"] == "user" else "Evan"
        text = (message.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n\n".join(lines)


async def maybe_compact(conv_id: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """长对话滚动压缩。返回 (window_messages, summary)：
    - (None, None)  → 无需压缩，照常走会话恢复
    - (window, summary) → 走无会话路径，把 window 当上下文文本喂，summary 作前情提要
    摘要生成失败时降级到"只保留最近对话"，绝不抛错、也绝不删库里的数据。"""
    if COMPACT_AFTER_TURNS <= 0:
        return None, None
    threshold = COMPACT_AFTER_TURNS * 2
    rows, _, _ = conversation_messages(conv_id)
    if len(rows) <= threshold:
        return None, None
    keep = max(2, COMPACT_KEEP_TURNS * 2)
    step = max(2, COMPACT_STEP_TURNS * 2)
    older = rows[:-keep]
    recent = rows[-keep:]
    summary, through_id = get_compaction_state(conv_id)
    unsummarized = [m for m in older if m["id"] > through_id]
    if older and (through_id == 0 or len(unsummarized) >= step):
        transcript = _render_transcript(unsummarized)
        try:
            new_summary = await summarize_conversation(transcript, prev_summary=summary)
        except Exception:
            logger.exception("conversation compaction summary failed conv=%s", conv_id)
            new_summary = ""
        if new_summary:
            summary, through_id = new_summary, older[-1]["id"]
            set_compaction_state(conv_id, summary, through_id)
        elif through_id == 0:
            # 摘要暂时生成不了、又还没有任何旧摘要兜底：
            # 只保留最近对话以避免撞上下文上限（完整记录仍在库里）。
            return recent, "（较早对话的摘要暂时无法生成，已仅保留最近对话；完整记录仍在数据库里。）"
    window = [m for m in rows if m["id"] > through_id]
    return window, (summary or None)


# 可选的"品牌版"首页：私有素材不进公开仓库，运行时若数据目录里有
# index.branded.html 就优先用它（每次请求都查，替换文件后无需重启）。
BRANDED_INDEX = Path(
    os.environ.get("BRANDED_INDEX_FILE", PROJECT_DIR + "/index.branded.html")
).expanduser()


@app.get("/")
async def index() -> FileResponse:
    page = BRANDED_INDEX if BRANDED_INDEX.is_file() else STATIC / "index.html"
    return FileResponse(
        page,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/static/manifest.webmanifest", include_in_schema=False)
async def manifest_webmanifest() -> Response:
    return Response(status_code=204)


@app.get("/static/css/typography-locked.css", include_in_schema=False)
async def typography_locked() -> Response:
    return Response(status_code=204)


@app.get("/marked.min.js")
async def marked_js() -> FileResponse:
    return FileResponse(
        STATIC / "marked.min.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/static/design-system.css")
async def design_system_css() -> FileResponse:
    return FileResponse(
        STATIC / "design-system.css",
        media_type="text/css",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/auth")
async def login(body: AuthBody) -> dict:
    token = auth.issue_token(body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"token": token}


@app.post("/api/chat", dependencies=[Depends(require_auth)])
async def chat(body: ChatBody) -> StreamingResponse:
    request_id = uuid4().hex[:12]
    request_started = perf_counter()
    timing_stages = {"request_received"}

    def log_timing(stage: str) -> None:
        if stage in timing_stages:
            return
        timing_stages.add(stage)
        timing_logger.info(
            "chat_timing request_id=%s stage=%s at_utc=%s elapsed_ms=%.1f",
            request_id,
            stage,
            datetime.now(UTC).isoformat(timespec="milliseconds"),
            (perf_counter() - request_started) * 1000,
        )

    timing_logger.info(
        "chat_timing request_id=%s stage=request_received at_utc=%s "
        "elapsed_ms=0.0",
        request_id,
        datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    is_branch_turn = body.edit_message_id is not None or body.retry_message_id is not None
    if body.edit_message_id is not None and body.retry_message_id is not None:
        raise HTTPException(status_code=400, detail="一次只能执行一种分支操作")
    if not is_branch_turn and not body.message.strip() and not body.attachments:
        raise HTTPException(status_code=400, detail="消息或附件不能为空")
    requested_conv_id = body.conversation_id or body.session_id
    if is_branch_turn and not requested_conv_id:
        raise HTTPException(status_code=400, detail="分支操作缺少会话标识")
    if body.attachments and not requested_conv_id:
        raise HTTPException(status_code=400, detail="附件缺少会话标识")
    attachment_items = (
        validated_attachments(requested_conv_id, body.attachments)
        if body.attachments and requested_conv_id
        else []
    )

    async def sse():
        # 旧实现是 locked() 就直接报错——前端 roll 时先 abort 再立刻发新请求，
        # 服务器还没来得及感知断开，必撞"上一条消息仍在回复"。改成等锁：
        # 旧的 SSE 生成器最迟在下一次心跳（15s）被取消并放锁，20s 足够。
        try:
            await asyncio.wait_for(chat_lock.acquire(), timeout=20)
        except asyncio.TimeoutError:
            payload = json.dumps(
                {"message": "上一条消息仍在回复"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
            return
        conv_id = None
        user_message_id = None
        branch_restore_id = None
        branch_committed = False
        response_text = ""
        response_thinking = ""
        response_traces: list[dict] = []
        try:
            # roll/重发时旧回合可能还在后台跑：先打断它再开新回合，
            # 而不是直接把用户顶回去
            await get_registry().interrupt_busy()
            await get_registry().assert_available()
            display_message = body.message.strip()
            current_attachment_items = attachment_items
            context_messages = None
            earlier_summary = None
            if body.edit_message_id is not None:
                prepared = prepare_edit_turn(
                    requested_conv_id or "",
                    body.edit_message_id,
                    display_message,
                )
                conv_id = prepared["conv_id"]
                resume_id = prepared["resume_id"]
                user_message_id = prepared["user_message_id"]
                display_message = prepared["message"]
                current_attachment_items = prepared["attachments"]
                context_messages = prepared["context_messages"]
                branch_restore_id = prepared.get("branch_id")
            elif body.retry_message_id is not None:
                prepared = prepare_retry_turn(
                    requested_conv_id or "",
                    body.retry_message_id,
                )
                conv_id = prepared["conv_id"]
                resume_id = prepared["resume_id"]
                user_message_id = prepared["user_message_id"]
                display_message = prepared["message"]
                current_attachment_items = prepared["attachments"]
                context_messages = prepared["context_messages"]
                branch_restore_id = prepared.get("branch_id")
            else:
                conv_id, resume_id, user_message_id = begin_turn(
                    display_message,
                    body.conversation_id,
                    body.session_id,
                    current_attachment_items,
                )
            payload = json.dumps(
                {
                    "conversation_id": conv_id,
                    "user_message_id": user_message_id,
                },
                ensure_ascii=False,
            )
            yield f"event: conversation\ndata: {payload}\n\n"
            # 长对话滚动压缩：只对普通新回合生效（编辑/重试已自带上下文）。
            # 命中时改走"无会话 + 历史当文本喂"路径，把上下文长度压回可控范围。
            if not is_branch_turn:
                window_messages, earlier_summary = await maybe_compact(conv_id)
                if window_messages is not None:
                    context_messages = window_messages
                    resume_id = None
            prompt = (
                render_context_prompt(context_messages)
                if context_messages
                else display_message
            )
            if earlier_summary:
                prompt = (
                    "<对话前情摘要>\n"
                    "以下是这段对话较早内容的摘要（原文已从上下文省略以控制长度，"
                    "但仍完整存在于记录里）。把它当作你和对方之前聊过的背景：\n\n"
                    f"{earlier_summary}\n"
                    "</对话前情摘要>\n\n"
                    f"{prompt}"
                )
            recalled = recall_memory(display_message)
            if recalled:
                prompt = (
                    "<recalled-memory>\n"
                    "以下是从家用记忆里检索到的相关片段，按相关度排序。"
                    "可能与这次请求相关，参考着用；不相关就忽略。\n\n"
                    f"{recalled}\n"
                    "</recalled-memory>\n\n"
                    f"{prompt}"
                )
            if current_attachment_items and not context_messages:
                paths = "\n".join(item["path"] for item in current_attachment_items)
                prompt += (
                    "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
                    f"{paths}\n]"
                )
            chat_args = (prompt, conv_id, resume_id, body.model,
                         body.effort, body.extended, log_timing)

            async def _merged():
                # 把"拉第一个 chunk"也放进这个生成器里，让下面的心跳循环
                # （每 15s 一次 : heartbeat）也罩住冷启动 + breath 的漫长等待。
                # 小内存机器冷启动 + 醒井常要 60~100s 才吐第一个字，这段
                # 若没有字节外流，会被 Cloudflare 在 ~100s 静默后掐断连接
                # （前端表现为"连接意外结束"）。
                if body.model == "codex":
                    async for c in stream_codex_chat(*chat_args):
                        yield c
                    return
                stream = stream_chat(*chat_args)
                try:
                    first = await stream.__anext__()
                except (SessionResumeError, StopAsyncIteration):
                    logger.info("session resume failed for conv=%s, retrying without session", conv_id)
                    retry_args = (prompt, conv_id, None, body.model,
                                  body.effort, body.extended, log_timing)
                    stream = stream_chat(*retry_args)
                    first = await stream.__anext__()
                yield first
                async for c in stream:
                    yield c

            # ⚠️ 心跳绝不能用 wait_for 直接包生成器的 __anext__：
            # 超时那一下会把 CancelledError 扔进生成器，整条流被连根关掉，
            # 下一次 __anext__ 直接 StopAsyncIteration——表现为
            # "模型思考超过 15 秒的回合全部静默断流、回复丢失"。
            # 现在改成：后台泵任务消费生成器 → 队列，心跳只对队列 get 超时。
            # 泵不依赖客户端连接：浏览器断了，回合照样跑完、照样入库。
            outq: asyncio.Queue = asyncio.Queue()

            async def pump():
                nonlocal response_text, response_thinking
                nonlocal branch_committed
                try:
                    async for chunk in _merged():
                        if chunk["event"] == "delta":
                            response_text += chunk.get("text", "")
                        elif chunk["event"] == "thinking":
                            response_thinking += chunk.get("text", "")
                        elif chunk["event"] == "tool_use":
                            response_traces.append({
                                "type": "tool_use",
                                "id": chunk.get("id"),
                                "name": chunk.get("name"),
                                "input": chunk.get("input"),
                                "text_offset": len(response_text.rstrip()),
                            })
                        elif chunk["event"] == "tool_result":
                            response_traces.append({
                                "type": "tool_result",
                                "tool_use_id": chunk.get("tool_use_id"),
                                "content": trace_content(chunk.get("content")),
                                "is_error": chunk.get("is_error", False),
                            })
                        elif chunk["event"] == "done":
                            logger.info(
                                "claude_raw_response request_id=%s conv_id=%s raw=%r",
                                request_id,
                                conv_id,
                                response_text,
                            )
                            if response_traces and not response_thinking:
                                try:
                                    trace_sum = await summarize_traces(
                                        response_traces,
                                    )
                                    if trace_sum:
                                        response_traces.insert(0, {
                                            "type": "summary",
                                            "text": trace_sum,
                                        })
                                        await outq.put(("chunk", {
                                            "event": "trace_summary",
                                            "text": trace_sum,
                                        }))
                                except Exception:
                                    logger.exception("trace summary failed")
                            assistant_message_id = complete_turn(
                                conv_id,
                                chunk["session_id"],
                                response_text,
                                response_thinking,
                                response_traces,
                            )
                            chunk["conversation_id"] = conv_id
                            chunk["assistant_message_id"] = assistant_message_id
                            branch_committed = True
                        await outq.put(("chunk", chunk))
                    await outq.put(("end", None))
                except Exception as exc:
                    await outq.put(("exc", exc))

            pump_task = asyncio.create_task(
                pump(), name=f"chat-pump-{request_id}",
            )
            # 生成器被取消后本地引用会消失，挂到模块级集合防 GC 掐掉泵
            _pump_tasks.add(pump_task)
            pump_task.add_done_callback(_pump_tasks.discard)

            heartbeat_interval = 15
            while True:
                try:
                    kind, item = await asyncio.wait_for(
                        outq.get(),
                        timeout=heartbeat_interval,
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if kind == "end":
                    break
                if kind == "exc":
                    raise item
                name = item.pop("event")
                data = json.dumps(item, ensure_ascii=False)
                yield f"event: {name}\ndata: {data}\n\n"
        except ConversationNotFound:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": "会话不存在或已被删除"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except ValueError as exc:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": str(exc) or "这条消息不能这样操作"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except SessionResumeError:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": "会话恢复失败"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except ActorBusyError as exc:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            payload = json.dumps(
                {"message": str(exc)},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        except Exception as exc:
            if branch_restore_id and not branch_committed:
                restore_branch(branch_restore_id)
            logger.exception("Claude SDK request failed")
            detail = str(exc)
            if "not available" in detail.lower() or "invalid model" in detail.lower():
                message = f"所选模型当前不可用：{body.model}"
            else:
                message = "模型暂时没有响应，请稍后重试。"
            payload = json.dumps(
                {"message": message},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
        finally:
            chat_lock.release()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/thinking-summary", dependencies=[Depends(require_auth)])
async def thinking_summary(body: ThinkingSummaryBody) -> dict:
    try:
        summary = await summarize_thinking(body.thinking)
    except Exception:
        logger.exception("thinking summary failed")
        summary = ""
    return {"summary": summary}


@app.post("/api/tool-caption", dependencies=[Depends(require_auth)])
async def tool_caption(body: ToolCaptionBody) -> dict:
    try:
        caption = await summarize_tool_use(body.tool_name, body.tool_input, body.tool_output)
    except Exception:
        logger.exception("tool caption failed")
        caption = ""
    return {"caption": caption}


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload(
    files: list[UploadFile] = File(...),
    conversation_id: str | None = Form(default=None),
) -> dict:
    conv_id = ensure_conversation(conversation_id)
    attachments = await save_uploads(conv_id, files)
    return {
        "conversation_id": conv_id,
        "attachments": attachments,
    }


@app.get(
    "/api/uploads/{conversation_id}/{filename}",
    dependencies=[Depends(require_auth)],
)
async def uploaded_file(conversation_id: str, filename: str) -> FileResponse:
    path = validated_file(conversation_id, filename)
    return FileResponse(path)


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def sessions() -> dict:
    return {"sessions": session_list()}


@app.get("/api/sessions/{session_id}/messages", dependencies=[Depends(require_auth)])
async def messages(
    session_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict:
    try:
        items, has_more, next_before_id = session_messages(session_id, before_id, limit)
        return {
            "messages": items,
            "has_more": has_more,
            "next_before_id": next_before_id,
        }
    except Exception as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@app.patch("/api/sessions/{session_id}/title", dependencies=[Depends(require_auth)])
async def rename(session_id: str, body: RenameBody) -> dict:
    set_session_title(session_id, body.title)
    return {"renamed": True}


@app.patch("/api/sessions/{session_id}/star", dependencies=[Depends(require_auth)])
async def star(session_id: str, body: StarBody) -> dict:
    set_session_starred(session_id, body.starred)
    return {"starred": body.starred}


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(require_auth)])
async def delete(session_id: str) -> dict:
    await get_registry().invalidate(resolve_conversation(session_id))
    remove_session(session_id)
    remove_conversation_uploads(session_id)
    return {"deleted": True}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def get_memory() -> dict:
    return {"content": read_memory()}


@app.put("/api/memory", dependencies=[Depends(require_auth)])
async def put_memory(body: MemoryBody) -> dict:
    write_memory(body.content)
    await get_registry().invalidate()
    return {"saved": True}


@app.get("/api/profile", dependencies=[Depends(require_auth)])
async def get_profile() -> dict:
    profile, imported_count, found_count = import_claude_export_memories(read_profile())
    if imported_count:
        await get_registry().invalidate()
    return {
        "profile": profile,
        "importedCount": imported_count,
        "foundCount": found_count,
    }


@app.put("/api/profile", dependencies=[Depends(require_auth)])
async def put_profile(body: ProfileBody) -> dict:
    data = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    profile = write_profile(data)
    await get_registry().invalidate()
    return {"saved": True, "profile": profile}


@app.post("/api/profile/memory", dependencies=[Depends(require_auth)])
async def post_memory(body: dict) -> dict:
    content = body.get("content", "").strip()
    if not content:
        return {"saved": False, "reason": "empty content"}
    result = add_saved_memory(content)
    if result is None:
        return {"saved": False, "reason": "duplicate or limit reached"}
    return {"saved": True, "memory": result}


@app.get("/api/diary", dependencies=[Depends(require_auth)])
async def get_diary() -> dict:
    entries = read_diary_entries()
    return {"entries": entries}


@app.get("/api/splash")
async def splash() -> dict:
    period = current_period()
    return {"period": period, "line": random_line(period)}


@app.get("/api/models")
async def models() -> dict:
    return {"models": available_models()}
