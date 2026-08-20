"""Process-local idempotency for exact ``grow`` retries.

Long grow operations may finish writing after an MCP/client response timeout.
An exact retry within a short window joins or reuses that operation instead of
starting another digest pass and creating duplicate buckets.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


RETRY_WINDOW_SECONDS = 30 * 60
_IN_PROGRESS_MESSAGE = "⏳ 相同的 grow 仍在后台处理中；无需重复提交，完成后会自动入库。"
_REUSED_RESULT_PREFIX = "✅ 已识别为刚才 grow 的重试；未重复写入。\n"


@dataclass
class _LoopState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inflight: dict[str, asyncio.Task[str]] = field(default_factory=dict)
    completed: dict[str, tuple[float, str]] = field(default_factory=dict)


_states: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = (
    weakref.WeakKeyDictionary()
)


def request_fingerprint(content: str) -> str:
    """Return a privacy-preserving fingerprint for the normalized payload."""

    normalized = (content or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _state_for_running_loop() -> _LoopState:
    loop = asyncio.get_running_loop()
    state = _states.get(loop)
    if state is None:
        state = _LoopState()
        _states[loop] = state
    return state


def _consume_background_exception(task: asyncio.Task[str]) -> None:
    if not task.cancelled():
        task.exception()


async def run_once(
    fingerprint: str,
    operation: Callable[[], Awaitable[str]],
    *,
    retry_window_seconds: float = RETRY_WINDOW_SECONDS,
) -> str:
    """Run one grow request and recognize exact in-flight/completed retries."""

    state = _state_for_running_loop()
    now = time.monotonic()
    async with state.lock:
        expired = [
            key
            for key, (finished_at, _result) in state.completed.items()
            if now - finished_at > retry_window_seconds
        ]
        for key in expired:
            state.completed.pop(key, None)

        completed = state.completed.get(fingerprint)
        if completed is not None:
            return _REUSED_RESULT_PREFIX + completed[1]
        if fingerprint in state.inflight:
            return _IN_PROGRESS_MESSAGE

        async def execute() -> str:
            try:
                result = await operation()
            except BaseException:
                async with state.lock:
                    state.inflight.pop(fingerprint, None)
                raise
            async with state.lock:
                state.inflight.pop(fingerprint, None)
                state.completed[fingerprint] = (time.monotonic(), result)
            return result

        task = asyncio.create_task(execute(), name=f"grow:{fingerprint[:12]}")
        task.add_done_callback(_consume_background_exception)
        state.inflight[fingerprint] = task

    return await asyncio.shield(task)


def reset_for_tests() -> None:
    _states.clear()
