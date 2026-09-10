"""Process-scoped maintenance freeze for Gale Memory."""

from __future__ import annotations

import json
import os
from collections.abc import Callable


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_INSTALL_MARKER = "_ombre_freeze_all_guard_installed"


def freeze_all_enabled(environ: dict[str, str] | None = None) -> bool:
    """Return whether this process must reject every memory read and write."""

    values = os.environ if environ is None else environ
    if str(values.get("OMBRE_FREEZE_ALL", "")).strip().lower() in _TRUE_VALUES:
        return True
    sentinel = str(values.get("OMBRE_FREEZE_ALL_SENTINEL", "")).strip()
    return bool(sentinel and os.path.isfile(sentinel))


def frozen_health_payload() -> dict[str, object]:
    """Minimal health response that does not inspect memory state."""

    return {"status": "frozen", "frozen": True, "reads": False, "writes": False}


class FreezeAllMiddleware:
    """Reject all HTTP/WebSocket traffic except the side-effect-free health check."""

    def __init__(
        self,
        app,
        *,
        enabled: bool | Callable[[], bool] | None = None,
    ) -> None:
        self.app = app
        if enabled is None:
            self._enabled = freeze_all_enabled
        elif callable(enabled):
            self._enabled = enabled
        else:
            self._enabled = lambda: bool(enabled)
        setattr(self, _INSTALL_MARKER, True)

    async def __call__(self, scope, receive, send):
        if not self._enabled():
            await self.app(scope, receive, send)
            return

        scope_type = scope.get("type")
        path = scope.get("path", "")
        if scope_type == "http" and path == "/health":
            await self.app(scope, receive, send)
            return
        if scope_type == "http":
            body = json.dumps(
                {"error": "gale_memory_frozen", "reads": False, "writes": False},
                separators=(",", ":"),
            ).encode("utf-8")
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"retry-after", b"300"),
                (b"x-gale-memory-frozen", b"true"),
            ]
            await send({"type": "http.response.start", "status": 503, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1013, "reason": "Gale Memory frozen"})
            return
        await self.app(scope, receive, send)


def install_freeze_all_guard(app, *, enabled=None):
    """Wrap an ASGI app once; the environment/sentinel is checked per request."""

    if getattr(app, _INSTALL_MARKER, False):
        return app
    return FreezeAllMiddleware(app, enabled=enabled)
