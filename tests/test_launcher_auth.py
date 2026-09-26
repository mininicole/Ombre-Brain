import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import ombre_nightfall_launcher as launcher


async def ok(_request):
    return PlainTextResponse("ok")


def build(monkeypatch, token):
    if token is None:
        monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OMBRE_AUTH_TOKEN", token)
    app = Starlette(routes=[
        Route("/mcp", ok, methods=["POST", "OPTIONS"]),
        Route("/breath-hook", ok, methods=["POST"]),
        Route("/api/recall", ok, methods=["POST"]),
        Route("/health", ok, methods=["GET"]),
    ])
    return launcher.install_bearer_auth(app)


@pytest.mark.asyncio
async def test_launcher_guard_requires_token_on_mcp_and_hooks(monkeypatch):
    app = build(monkeypatch, "expected-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t.example") as c:
        assert (await c.post("/mcp")).status_code == 401
        assert (await c.post("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await c.post("/breath-hook")).status_code == 401
        assert (await c.post("/mcp", headers={"Authorization": "Bearer expected-token"})).status_code == 200
        assert (await c.post("/mcp?token=expected-token")).status_code == 200
        assert (await c.options("/mcp")).status_code == 200
        # REST and health keep their existing behaviour.
        assert (await c.post("/api/recall")).status_code == 200
        assert (await c.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_launcher_guard_is_noop_without_configured_token(monkeypatch):
    app = build(monkeypatch, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t.example") as c:
        assert (await c.post("/mcp")).status_code == 200
