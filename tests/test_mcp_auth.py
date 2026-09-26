import importlib

import pytest
from types import SimpleNamespace

import server


def _request(*, authorization="", query=None):
    return SimpleNamespace(
        headers={"Authorization": authorization} if authorization else {},
        query_params=query or {},
    )


def test_header_bearer_is_accepted():
    request = _request(authorization="Bearer candidate-secret")
    assert server._request_has_valid_bearer(request, "candidate-secret") is True


def test_wrong_or_missing_header_is_rejected():
    assert server._request_has_valid_bearer(_request(), "candidate-secret") is False
    assert (
        server._request_has_valid_bearer(
            _request(authorization="Bearer wrong"),
            "candidate-secret",
        )
        is False
    )


def test_query_token_is_rejected_by_default():
    request = _request(query={"token": "candidate-secret"})
    assert server._request_has_valid_bearer(request, "candidate-secret") is False


def test_query_token_requires_explicit_legacy_opt_in():
    request = _request(query={"token": "candidate-secret"})
    assert (
        server._request_has_valid_bearer(
            request,
            "candidate-secret",
            allow_query_token=True,
        )
        is True
    )


def test_empty_expected_token_always_fails_closed():
    request = _request(authorization="Bearer anything")
    assert server._request_has_valid_bearer(request, "") is False


@pytest.mark.asyncio
async def test_bearer_guard_protects_mcp_on_every_launch_path(monkeypatch):
    """The Night-Fall launcher wraps its own app; the guard must still apply."""
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "expected-token")
    monkeypatch.delenv("OMBRE_ALLOW_QUERY_TOKEN", raising=False)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "_oauth_runtime", None)

    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route("/mcp", ok, methods=["POST", "OPTIONS"]),
        Route("/health", ok, methods=["GET"]),
    ])
    guarded = server.install_bearer_auth(app)
    assert server.install_bearer_auth(guarded) is guarded
    transport = httpx.ASGITransport(app=guarded)
    async with httpx.AsyncClient(transport=transport, base_url="https://t.example") as client:
        missing = await client.post("/mcp")
        wrong = await client.post("/mcp", headers={"Authorization": "Bearer nope"})
        query = await client.post("/mcp?token=expected-token")
        good = await client.post("/mcp", headers={"Authorization": "Bearer expected-token"})
        preflight = await client.options("/mcp")
        health = await client.get("/health")

    assert (missing.status_code, wrong.status_code, query.status_code) == (401, 401, 401)
    assert good.status_code == 200
    assert preflight.status_code == 200
    assert health.status_code == 200
