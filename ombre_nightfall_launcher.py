"""Run Ombre with Night-Fall while keeping local Uvicorn security settings."""

from __future__ import annotations

import hmac
import json
import os
from urllib.parse import parse_qs

import uvicorn
from night_fall.launcher import (
    import_ombre_server,
    load_config,
    register_night_fall,
    run_ombre_server,
)


_PROTECTED_PREFIXES = ("/mcp", "/breath-hook", "/dream-hook")


def install_bearer_auth(app):
    """Require OMBRE_AUTH_TOKEN on MCP and hook paths.

    server.py only adds its bearer middleware in its own __main__ block. This
    launcher builds the ASGI app through Night-Fall instead, so until
    2026-09-26 /mcp on Fly accepted requests without any token. Same rules
    as server.py: Authorization: Bearer header or ?token= query parameter.
    No token configured (Gale's frozen loopback process) means no check.
    """
    token = os.environ.get("OMBRE_AUTH_TOKEN", "").strip()
    if not token:
        return app

    async def guarded(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        path = scope.get("path", "")
        protected = any(path == p or path.startswith(p + "/") for p in _PROTECTED_PREFIXES)
        if not protected or scope.get("method", "").upper() == "OPTIONS":
            return await app(scope, receive, send)
        provided = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                text = value.decode("latin-1")
                if text.startswith("Bearer "):
                    provided = text[7:].strip()
                break
        if not provided:
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            provided = (query.get("token") or [""])[0].strip()
        if provided and hmac.compare_digest(provided, token):
            return await app(scope, receive, send)
        body = json.dumps({"error": "unauthorized"}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    return guarded


def main() -> None:
    cfg = load_config(require_ombre=True)
    ombre_server = import_ombre_server(cfg.ombre_home)
    register_night_fall(ombre_server, cfg)

    upstream_uvicorn_run = uvicorn.run

    def run_uvicorn(app, host="0.0.0.0", port=8000, **kwargs):
        del host
        kwargs.pop("access_log", None)
        app = install_bearer_auth(app)
        app = ombre_server.install_gale_dash_guard(app)
        # Night-Fall owns the cloud ASGI launch path, so install the process
        # freeze here too. It remains a no-op for Evan because only Gale gets
        # the sentinel environment variable.
        app = ombre_server.install_freeze_all_guard(app)
        return upstream_uvicorn_run(
            app,
            host=os.environ.get("OMBRE_HOST", "0.0.0.0"),
            port=port,
            access_log=not bool(os.environ.get("GALE_MCP_SLUG", "").strip()),
            **kwargs,
        )

    uvicorn.run = run_uvicorn
    try:
        run_ombre_server(ombre_server)
    finally:
        uvicorn.run = upstream_uvicorn_run


if __name__ == "__main__":
    main()
