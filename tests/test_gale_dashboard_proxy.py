import importlib
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.requests import Request


@pytest.fixture(scope="module")
def server_module(tmp_path_factory):
    """Import server.py with an isolated buckets directory."""
    buckets_dir = tmp_path_factory.mktemp("gale-dashboard-proxy") / "buckets"
    previous = os.environ.get("OMBRE_BUCKETS_DIR")
    os.environ["OMBRE_BUCKETS_DIR"] = str(buckets_dir)
    try:
        module = importlib.import_module("server")
        yield module
    finally:
        if previous is None:
            os.environ.pop("OMBRE_BUCKETS_DIR", None)
        else:
            os.environ["OMBRE_BUCKETS_DIR"] = previous


def make_request(
    method,
    path,
    *,
    raw_path=None,
    query=b"",
    headers=None,
    body=b"",
):
    full_path = "/gale-dash/" + path
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": full_path,
        "raw_path": raw_path or full_path.encode("ascii"),
        "query_string": query,
        "root_path": "",
        "headers": headers or [],
        "client": ("203.0.113.9", 12345),
        "server": ("example.test", 443),
        "path_params": {"path": path},
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class FakeUpstream:
    def __init__(self, *, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = httpx.Headers(headers or [])
        self.chunks = chunks or [b"ok"]
        self.closed = False

    async def aiter_raw(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class FakeClient:
    def __init__(self, upstream=None, error=None):
        self.upstream = upstream or FakeUpstream()
        self.error = error
        self.built = None
        self.body = None

    def build_request(self, method, url, *, headers, content):
        self.built = {
            "method": method,
            "url": url,
            "headers": list(headers),
            "content": content,
        }
        return SimpleNamespace(content=content)

    async def send(self, request, *, stream):
        assert stream is True
        if self.error is not None:
            raise self.error
        chunks = []
        async for chunk in request.content:
            chunks.append(chunk)
        self.body = b"".join(chunks)
        return self.upstream


async def response_body(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


ALLOWED_ROUTES = [
    ("dashboard", "GET"),
    ("auth/status", "GET"),
    ("auth/setup", "POST"),
    ("auth/login", "POST"),
    ("auth/logout", "POST"),
    ("auth/change-password", "POST"),
    ("api/status", "GET"),
    ("api/host-vault", "GET"),
    ("api/host-vault", "POST"),
    ("api/buckets", "GET"),
    ("api/bucket/bucket-123", "GET"),
    ("api/search", "GET"),
    ("api/forget/bucket-123", "DELETE"),
    ("api/edit/bucket-123", "POST"),
    ("api/config", "GET"),
    ("api/config", "POST"),
    ("api/import/upload", "POST"),
    ("api/import/status", "GET"),
    ("api/import/pause", "POST"),
    ("api/import/results", "GET"),
    ("api/import/patterns", "GET"),
    ("api/import/review", "POST"),
]


def test_fixed_upstream_and_finite_timeout(server_module):
    assert server_module._GALE_DASH_BASE == "http://127.0.0.1:8790"
    assert server_module._GALE_DASH_TIMEOUT.connect == 5.0
    assert server_module._GALE_DASH_TIMEOUT.read == 60.0
    assert server_module._GALE_DASH_TIMEOUT.write == 60.0
    assert server_module._GALE_DASH_TIMEOUT.pool == 5.0


@pytest.mark.parametrize(("path", "method"), ALLOWED_ROUTES)
def test_exact_route_and_method_whitelist(server_module, path, method):
    assert server_module._gale_dash_route_allowed(path, method)


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("dashboard", "POST"),
        ("auth/status", "POST"),
        ("auth/login", "GET"),
        ("api/buckets", "POST"),
        ("api/forget/bucket-123", "POST"),
        ("api/edit/bucket-123", "PATCH"),
        ("api/config", "DELETE"),
        ("api/import/upload", "PUT"),
        ("api/status", "OPTIONS"),
        ("dashboard", "HEAD"),
    ],
)
def test_wrong_method_is_not_allowed(server_module, path, method):
    assert not server_module._gale_dash_route_allowed(path, method)


@pytest.mark.parametrize(
    "path",
    [
        "mcp",
        "chat",
        "breath-hook",
        "dream-hook",
        "api/remember",
        "api/recall",
        "api/state",
        "api/poke",
        "api/bucket",
        "api/bucket/a/b",
    ],
)
def test_non_dashboard_paths_are_not_allowed(server_module, path):
    assert not server_module._gale_dash_route_allowed(path, "GET")
    assert not server_module._gale_dash_route_allowed(path, "POST")


@pytest.mark.parametrize(
    ("path", "raw_path"),
    [
        ("api/bucket/../secret", b"/gale-dash/api/bucket/../secret"),
        ("api/bucket/.", b"/gale-dash/api/bucket/."),
        ("api/bucket/a\\b", b"/gale-dash/api/bucket/a\\b"),
        ("api/bucket/a/b", b"/gale-dash/api/bucket/a%2fb"),
        ("api/bucket/a%2fb", b"/gale-dash/api/bucket/a%252fb"),
        ("api/bucket/../x", b"/gale-dash/api/bucket/%2e%2e/x"),
        ("api/bucket/%2e%2e", b"/gale-dash/api/bucket/%252e%252e"),
        ("api/bucket/%zz", b"/gale-dash/api/bucket/%zz"),
        ("api/bucket/different", b"/gale-dash/api/bucket/actual"),
        ("api//status", b"/gale-dash/api//status"),
    ],
)
def test_path_normalization_bypasses_are_rejected(server_module, path, raw_path):
    request = make_request("GET", path, raw_path=raw_path)
    assert not server_module._gale_dash_path_is_safe(request, path)


def test_request_cookie_mapping_and_hop_by_hop_filter(server_module):
    raw = [
        (b"host", b"public.example"),
        (b"connection", b"keep-alive, x-remove"),
        (b"x-remove", b"secret"),
        (b"content-type", b"application/json"),
        (b"cookie", b"ombre_session=evan; theme=dark"),
        (b"cookie", b"gale_session=gale-token; preference=compact"),
    ]
    headers = server_module._gale_dash_request_headers(raw)

    assert (b"host", b"public.example") not in headers
    assert (b"x-remove", b"secret") not in headers
    assert (b"content-type", b"application/json") in headers
    cookie_headers = [value for name, value in headers if name.lower() == b"cookie"]
    assert cookie_headers == [b"theme=dark; preference=compact; ombre_session=gale-token"]
    assert b"gale_session" not in cookie_headers[0]
    assert b"ombre_session=evan" not in cookie_headers[0]


def test_cookie_mapping_removes_sessions_when_gale_cookie_is_absent(server_module):
    headers = server_module._gale_dash_request_headers(
        [(b"cookie", b"ombre_session=evan; theme=dark")]
    )
    assert (b"cookie", b"theme=dark") in headers


@pytest.mark.asyncio
async def test_forget_rejects_unauthenticated_request_before_delete(
    monkeypatch, server_module
):
    server_module._sessions.clear()
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(server_module.bucket_mgr, "delete", delete)
    request = make_request("DELETE", "api/forget/bucket-123")
    request.scope["path_params"] = {"bucket_id": "bucket-123"}

    response = await server_module.api_forget(request)

    assert response.status_code == 401
    delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_allows_valid_session_and_runs_existing_delete(
    monkeypatch, server_module
):
    token = "valid-dashboard-session"
    server_module._sessions.clear()
    server_module._sessions[token] = time.time() + 60
    delete = AsyncMock(return_value=True)
    delete_embedding = MagicMock()
    monkeypatch.setattr(server_module.bucket_mgr, "delete", delete)
    monkeypatch.setattr(server_module.embedding_engine, "delete_embedding", delete_embedding)
    request = make_request(
        "DELETE",
        "api/forget/bucket-123",
        headers=[(b"cookie", f"ombre_session={token}".encode("ascii"))],
    )
    request.scope["path_params"] = {"bucket_id": "bucket-123"}

    response = await server_module.api_forget(request)

    assert response.status_code == 200
    delete.assert_awaited_once_with("bucket-123")
    delete_embedding.assert_called_once_with("bucket-123")


def test_multiple_set_cookie_headers_preserve_expires_and_logout(server_module):
    raw = [
        (
            b"set-cookie",
            b"ombre_session=abc; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800; Expires=Wed, 21 Oct 2026 07:28:00 GMT",
        ),
        (b"set-cookie", b"theme=dark; Path=/; SameSite=Strict"),
        (
            b"set-cookie",
            b"ombre_session=\"\"; Path=/; HttpOnly; SameSite=lax; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        ),
    ]
    headers = server_module._gale_dash_response_headers(raw)
    cookies = [value for name, value in headers if name.lower() == b"set-cookie"]

    assert len(cookies) == 3
    assert cookies[0] == (
        b"gale_session=abc; Path=/gale-dash; HttpOnly; Secure; SameSite=Lax; "
        b"Max-Age=604800; Expires=Wed, 21 Oct 2026 07:28:00 GMT"
    )
    assert cookies[1] == b"theme=dark; Path=/; SameSite=Strict"
    assert cookies[2] == (
        b"gale_session=\"\"; Path=/gale-dash; HttpOnly; SameSite=lax; Max-Age=0; "
        b"Expires=Thu, 01 Jan 1970 00:00:00 GMT"
    )


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (b"/dashboard", b"/gale-dash/dashboard"),
        (b"/auth/login?next=%2Fdashboard", b"/gale-dash/auth/login?next=%2Fdashboard"),
        (b"/api/status#ready", b"/gale-dash/api/status#ready"),
        (
            b"http://127.0.0.1:8790/api/status?full=1",
            b"/gale-dash/api/status?full=1",
        ),
        (b"http://127.0.0.1:8790/mcp", b"/gale-dash/mcp"),
        (b"https://external.example/dashboard", b"https://external.example/dashboard"),
        (b"//external.example/dashboard", b"//external.example/dashboard"),
        (b"dashboard", b"dashboard"),
    ],
)
def test_location_rewrite(server_module, location, expected):
    assert server_module._gale_dash_rewrite_location(location) == expected


@pytest.mark.asyncio
async def test_asgi_guard_runs_outside_cors_and_leaves_other_paths_unchanged(
    monkeypatch, server_module
):
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    outside_calls = 0

    async def outside(_request):
        nonlocal outside_calls
        outside_calls += 1
        return PlainTextResponse("outside")

    app = Starlette(routes=[
        Route(
            "/gale-dash/{path:path}",
            server_module.gale_dash_proxy,
            methods=server_module._GALE_DASH_METHODS,
        ),
        Route("/outside", outside, methods=["GET"]),
    ])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    guarded_app = server_module.install_gale_dash_guard(app)
    assert server_module.install_gale_dash_guard(guarded_app) is guarded_app

    upstream = FakeUpstream(chunks=[b"dashboard"])
    proxy_client = FakeClient(upstream)
    monkeypatch.setattr(server_module, "_get_gale_dash_client", lambda: proxy_client)
    transport = httpx.ASGITransport(app=guarded_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        blocked = await client.options(
            "/gale-dash/api/remember",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        allowed = await client.get("/gale-dash/dashboard")
        outside = await client.get("/outside")
        outside_preflight = await client.options(
            "/outside",
            headers={
                "Origin": "https://example.test",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert blocked.status_code == 404
    assert "access-control-allow-origin" not in blocked.headers
    assert allowed.status_code == 200
    assert allowed.content == b"dashboard"
    assert proxy_client.built["url"] == "/dashboard"
    assert upstream.closed
    assert outside.status_code == 200
    assert outside.text == "outside"
    assert outside_calls == 1
    assert outside_preflight.status_code == 200
    assert outside_preflight.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_proxy_forwards_query_body_multipart_and_response(monkeypatch, server_module):
    upstream = FakeUpstream(
        status=201,
        headers=[
            (b"content-type", b"application/json"),
            (b"x-upstream", b"gale"),
            (b"connection", b"x-drop"),
            (b"x-drop", b"no"),
        ],
        chunks=[b'{"ok":', b"true}"],
    )
    client = FakeClient(upstream)
    monkeypatch.setattr(server_module, "_get_gale_dash_client", lambda: client)
    content_type = b"multipart/form-data; boundary=----gale-boundary"
    body = b"------gale-boundary\r\ncontent\r\n------gale-boundary--\r\n"
    request = make_request(
        "POST",
        "api/import/upload",
        query=b"preserve_raw=1&domain=tg-gale&domain=private",
        headers=[
            (b"host", b"attacker.example:9999"),
            (b"content-type", content_type),
            (b"cookie", b"ombre_session=evan; gale_session=gale"),
        ],
        body=body,
    )

    response = await server_module.gale_dash_proxy(request)

    assert client.built["method"] == "POST"
    assert client.built["url"] == (
        "/api/import/upload?preserve_raw=1&domain=tg-gale&domain=private"
    )
    assert all(name.lower() != b"host" for name, _ in client.built["headers"])
    assert (b"content-type", content_type) in client.built["headers"]
    assert (b"cookie", b"ombre_session=gale") in client.built["headers"]
    assert client.body == body
    assert response.status_code == 201
    assert (b"x-upstream", b"gale") in response.raw_headers
    assert (b"x-drop", b"no") not in response.raw_headers
    assert await response_body(response) == b'{"ok":true}'
    assert upstream.closed


@pytest.mark.asyncio
async def test_proxy_closes_upstream_when_response_iteration_is_cancelled(
    monkeypatch, server_module
):
    upstream = FakeUpstream(chunks=[b"first", b"second"])
    client = FakeClient(upstream)
    monkeypatch.setattr(server_module, "_get_gale_dash_client", lambda: client)
    response = await server_module.gale_dash_proxy(make_request("GET", "dashboard"))

    iterator = response.body_iterator
    assert await anext(iterator) == b"first"
    await iterator.aclose()
    assert upstream.closed


@pytest.mark.asyncio
async def test_api_remember_forwards_explicit_domain(monkeypatch, server_module):
    hold = AsyncMock(return_value="新建→wonderland-bucket tg-wonderland")
    monkeypatch.setattr(server_module, "hold", hold)
    request = make_request(
        "POST",
        "api/remember",
        body=json.dumps({
            "content": "Wonderland 群聊阶段总结",
            "importance": 6,
            "domain": "tg-wonderland",
            "tags": "Wonderland,群聊总结",
        }).encode("utf-8"),
    )

    response = await server_module.api_remember(request)

    assert response.status_code == 200
    hold.assert_awaited_once()
    assert hold.await_args.kwargs["domain"] == "tg-wonderland"


@pytest.mark.asyncio
async def test_hold_explicit_domain_overrides_auto_classification(
    monkeypatch, server_module
):
    monkeypatch.setattr(
        server_module.decay_engine,
        "ensure_started",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        server_module.dehydrator,
        "analyze",
        AsyncMock(return_value={
            "domain": ["恋爱", "社交"],
            "valence": 0.7,
            "arousal": 0.5,
            "tags": ["自动标签"],
            "suggested_name": "群聊总结",
        }),
    )
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "search",
        AsyncMock(return_value=[]),
    )
    create = AsyncMock(return_value="wonderland-bucket")
    monkeypatch.setattr(server_module.bucket_mgr, "create", create)
    monkeypatch.setattr(
        server_module.embedding_engine,
        "generate_and_store",
        AsyncMock(return_value=None),
    )

    result = await server_module.hold(
        content="Wonderland 群聊阶段总结",
        domain="tg-wonderland",
    )

    assert result.startswith("新建→wonderland-bucket")
    assert create.await_args.kwargs["domain"] == ["tg-wonderland"]


@pytest.mark.asyncio
async def test_breath_honors_max_results(monkeypatch, server_module):
    matches = [
        {
            "id": f"wonderland-{index}",
            "metadata": {"domain": ["tg-wonderland"]},
            "content": f"Wonderland summary {index}",
        }
        for index in range(5)
    ]
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "search",
        AsyncMock(return_value=matches),
    )
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "list_all",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "touch",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        server_module.embedding_engine,
        "search_similar",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        server_module.dehydrator,
        "dehydrate",
        AsyncMock(side_effect=lambda content, _meta: content),
    )

    result = await server_module.breath(
        query="Wonderland",
        domain="tg-wonderland",
        max_results=2,
        max_tokens=5000,
        include_recent=0,
    )

    assert "[bucket_id:wonderland-0]" in result
    assert "[bucket_id:wonderland-1]" in result
    assert "[bucket_id:wonderland-2]" not in result


@pytest.mark.asyncio
async def test_upstream_failure_returns_sanitized_502(monkeypatch, server_module):
    error = httpx.ConnectError(
        "cannot connect to http://127.0.0.1:8790",
        request=httpx.Request("GET", "http://127.0.0.1:8790/dashboard"),
    )
    client = FakeClient(error=error)
    monkeypatch.setattr(server_module, "_get_gale_dash_client", lambda: client)

    response = await server_module.gale_dash_proxy(make_request("GET", "dashboard"))

    assert response.status_code == 502
    assert response.body == b"bad gateway"
    assert b"8790" not in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("mcp", "GET"),
        ("chat", "GET"),
        ("breath-hook", "GET"),
        ("dream-hook", "GET"),
        ("api/remember", "POST"),
        ("api/recall", "POST"),
        ("dashboard", "POST"),
        ("api/status", "PATCH"),
        ("api/status", "OPTIONS"),
    ],
)
async def test_proxy_returns_404_before_contacting_upstream(
    monkeypatch, server_module, path, method
):
    def unexpected_client():
        raise AssertionError("disallowed request reached upstream client")

    monkeypatch.setattr(server_module, "_get_gale_dash_client", unexpected_client)
    response = await server_module.gale_dash_proxy(make_request(method, path))
    assert response.status_code == 404
    assert response.body == b"not found"
