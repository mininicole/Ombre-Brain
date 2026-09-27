import importlib
import inspect
import json
import os
import time
from datetime import datetime, timedelta, timezone
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


def _time_canary_bucket(
    bucket_id,
    content,
    age_days,
    *,
    bucket_type="dynamic",
    tags=None,
    memory_lifecycle=None,
    valid_until=None,
):
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    metadata = {
        "id": bucket_id,
        "created": created.isoformat(),
        "type": bucket_type,
        "tags": tags or [],
        "domain": ["test"],
    }
    if memory_lifecycle:
        metadata["memory_lifecycle"] = memory_lifecycle
    if valid_until:
        metadata["valid_until"] = valid_until.isoformat()
    return {
        "id": bucket_id,
        "metadata": metadata,
        "content": content,
    }


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
    ("api/remember", "POST"),
    ("api/recall", "POST"),
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


@pytest.mark.asyncio
async def test_bucket_list_hides_soft_deleted_buckets_but_keeps_natural_archive(
    monkeypatch, server_module
):
    token = "valid-dashboard-session"
    server_module._sessions.clear()
    server_module._sessions[token] = time.time() + 60
    list_all = AsyncMock(
        return_value=[
            {
                "id": "active",
                "metadata": {"name": "Active", "type": "dynamic"},
                "content": "active memory",
            },
            {
                "id": "natural-archive",
                "metadata": {"name": "Archive", "type": "archived"},
                "content": "naturally archived memory",
            },
            {
                "id": "soft-deleted",
                "metadata": {
                    "name": "Deleted",
                    "type": "archived",
                    "deleted_at": "2026-07-29T12:00:00+08:00",
                },
                "content": "deleted memory",
            },
        ]
    )
    monkeypatch.setattr(server_module.bucket_mgr, "list_all", list_all)
    request = make_request(
        "GET",
        "api/buckets",
        headers=[(b"cookie", f"ombre_session={token}".encode("ascii"))],
    )

    response = await server_module.api_buckets(request)
    payload = json.loads(response.body)

    assert response.status_code == 200
    list_all.assert_awaited_once_with(include_archive=True)
    assert [bucket["id"] for bucket in payload] == ["active", "natural-archive"]


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


def test_api_remember_has_one_registered_source_definition(server_module):
    source = inspect.getsource(server_module)
    assert source.count('@mcp.custom_route("/api/remember"') == 1


@pytest.mark.asyncio
async def test_api_remember_preserves_production_hold_contract(monkeypatch, server_module):
    hold = AsyncMock(return_value="合并→existing-bucket tg-gale")
    monkeypatch.setattr(server_module, "hold", hold)
    request = make_request(
        "POST",
        "api/remember",
        body=json.dumps({
            "content": "Gale memory",
            "importance": "7",
            "feel": True,
            "pinned": True,
            "domain": "tg-gale, private",
            "tags": "亲密, 瞬间",
            "valence": "0.7",
            "arousal": 0.6,
            "source_bucket": "source-1",
            "quotes": ["short quote"],
        }).encode("utf-8"),
    )

    response = await server_module.api_remember(request)

    assert response.status_code == 200
    assert json.loads(response.body) == {"id": "合并→existing-bucket tg-gale"}
    hold.assert_awaited_once_with(
        content="Gale memory",
        feel=True,
        importance=7,
        pinned=True,
        domain="tg-gale,private",
        valence=0.7,
        arousal=0.6,
        tags="亲密,瞬间",
        source_bucket="source-1",
        quotes=["short quote"],
        memory_lifecycle="",
        source_timestamp="",
        valid_until="",
        allow_merge=True,
    )


@pytest.mark.asyncio
async def test_api_remember_accepts_array_tags_and_domains(monkeypatch, server_module):
    hold = AsyncMock(return_value="新建→array-bucket tg-wonderland")
    monkeypatch.setattr(server_module, "hold", hold)
    request = make_request(
        "POST",
        "api/remember",
        body=json.dumps({
            "content": "Wonderland digest",
            "domain": ["tg-wonderland", "private"],
            "tags": ["Wonderland", "群聊总结"],
        }).encode("utf-8"),
    )

    response = await server_module.api_remember(request)

    assert response.status_code == 200
    assert hold.await_args.kwargs["domain"] == "tg-wonderland,private"
    assert hold.await_args.kwargs["tags"] == "Wonderland,群聊总结"
    assert hold.await_args.kwargs["valence"] == -1
    assert hold.await_args.kwargs["arousal"] == -1


@pytest.mark.asyncio
async def test_api_remember_rejects_nested_metadata_without_writing(
    monkeypatch, server_module
):
    hold = AsyncMock()
    monkeypatch.setattr(server_module, "hold", hold)
    request = make_request(
        "POST",
        "api/remember",
        body=json.dumps({"content": "bad metadata", "tags": [["nested"]]}).encode(
            "utf-8"
        ),
    )

    response = await server_module.api_remember(request)

    assert response.status_code == 400
    hold.assert_not_awaited()


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
async def test_breath_domain_boundary_covers_vector_and_random_supplements(
    monkeypatch, server_module
):
    safe = {
        "id": "group-safe",
        "metadata": {"domain": ["tg-gale-group-safe-v1"]},
        "content": "群里可以知道的共同梗",
    }
    private = {
        "id": "private-memory",
        "metadata": {"domain": ["恋爱"], "created": "2026-08-01T00:00:00Z"},
        "content": "绝不能进入大群上下文的私密内容",
    }
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "search",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "list_all",
        AsyncMock(side_effect=[[], [private]]),
    )
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "get",
        AsyncMock(side_effect=lambda bucket_id: {
            "group-safe": safe,
            "private-memory": private,
        }[bucket_id]),
    )
    monkeypatch.setattr(
        server_module.embedding_engine,
        "search_similar",
        AsyncMock(return_value=[("private-memory", 0.99), ("group-safe", 0.90)]),
    )
    monkeypatch.setattr(
        server_module.dehydrator,
        "dehydrate",
        AsyncMock(side_effect=lambda content, _meta: content),
    )
    monkeypatch.setattr(server_module.bucket_mgr, "touch", AsyncMock(return_value=True))
    monkeypatch.setattr(server_module.decay_engine, "calculate_score", lambda _meta: 1.0)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)

    result = await server_module.breath(
        query="共同经历",
        domain="tg-gale-group-safe-v1",
        max_results=3,
        max_tokens=1200,
        include_recent=0,
    )

    assert "[bucket_id:group-safe]" in result
    assert "private-memory" not in result
    assert "绝不能进入大群上下文" not in result


@pytest.mark.asyncio
async def test_group_safe_visibility_preserves_existing_domains(
    monkeypatch, server_module
):
    get_bucket = AsyncMock(return_value={
        "id": "shared-memory",
        "metadata": {"domain": ["旅行", "共同经历"]},
        "content": "群里可以知道的旅行",
    })
    update_bucket = AsyncMock(return_value=True)
    monkeypatch.setattr(server_module.bucket_mgr, "get", get_bucket)
    monkeypatch.setattr(server_module.bucket_mgr, "update", update_bucket)

    shared = await server_module._set_group_safe_visibility(
        "shared-memory", True
    )

    assert shared["updated"] is True
    assert shared["shared"] is True
    update_bucket.assert_awaited_once_with(
        "shared-memory",
        domain=["旅行", "共同经历", "tg-gale-group-safe-v1"],
    )


@pytest.mark.asyncio
async def test_breath_counts_pinned_buckets_against_token_budget(monkeypatch, server_module):
    pinned = {
        "id": "pinned-core",
        "metadata": {"domain": ["tg-private"], "pinned": True},
        "content": "核心" * 80,
    }
    match = {
        "id": "search-match",
        "metadata": {"domain": ["tg-private"]},
        "content": "检索" * 80,
    }
    monkeypatch.setattr(server_module.bucket_mgr, "list_all", AsyncMock(return_value=[pinned]))
    monkeypatch.setattr(server_module.bucket_mgr, "search", AsyncMock(return_value=[match]))
    monkeypatch.setattr(server_module.bucket_mgr, "touch", AsyncMock(return_value=True))
    monkeypatch.setattr(server_module.embedding_engine, "search_similar", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        server_module.dehydrator,
        "dehydrate",
        AsyncMock(side_effect=lambda content, _meta: content),
    )
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)

    result = await server_module.breath(
        query="核心",
        domain="tg-private",
        max_results=3,
        max_tokens=300,
        include_recent=0,
    )

    assert "[bucket_id:pinned-core]" in result
    assert "[bucket_id:search-match]" not in result


@pytest.mark.asyncio
async def test_breath_reserves_budget_for_two_recent_buckets(monkeypatch, server_module):
    pinned = {
        "id": "pinned-core",
        "metadata": {"pinned": True, "created": "2026-08-01T00:00:00Z"},
        "content": "核心" * 80,
    }
    match = {
        "id": "search-match",
        "metadata": {"created": "2026-08-08T00:00:00Z"},
        "content": "检索" * 80,
    }
    recent_one = {
        "id": "recent-one",
        "metadata": {
            "created": "2026-09-08T02:00:00Z",
            "memory_lifecycle": "stable_fact",
        },
        "content": "最近甲" * 40,
    }
    recent_two = {
        "id": "recent-two",
        "metadata": {
            "created": "2026-09-08T01:00:00Z",
            "memory_lifecycle": "stable_fact",
        },
        "content": "最近乙" * 40,
    }
    monkeypatch.setattr(
        server_module.bucket_mgr,
        "list_all",
        AsyncMock(side_effect=[[pinned], [pinned, match, recent_one, recent_two]]),
    )
    monkeypatch.setattr(server_module.bucket_mgr, "search", AsyncMock(return_value=[match]))
    monkeypatch.setattr(server_module.bucket_mgr, "touch", AsyncMock(return_value=True))
    monkeypatch.setattr(server_module.embedding_engine, "search_similar", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        server_module.dehydrator,
        "dehydrate",
        AsyncMock(side_effect=lambda content, _meta: content),
    )
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)

    result = await server_module.breath(
        query="今天",
        max_results=2,
        max_tokens=1000,
        include_recent=2,
    )

    assert "[bucket_id:pinned-core]" in result
    assert "[bucket_id:recent-one]" in result
    assert "[bucket_id:recent-two]" in result
    assert "[bucket_id:search-match]" not in result


@pytest.mark.asyncio
async def test_breath_noquery_skips_pinned_bucket_that_exceeds_budget(monkeypatch, server_module):
    pinned = [
        {
            "id": "small-core",
            "metadata": {"pinned": True},
            "content": "短核心" * 30,
        },
        {
            "id": "oversized-core",
            "metadata": {"pinned": True},
            "content": "超长核心" * 200,
        },
    ]
    monkeypatch.setattr(server_module.bucket_mgr, "list_all", AsyncMock(return_value=pinned))
    monkeypatch.setattr(
        server_module.dehydrator,
        "dehydrate",
        AsyncMock(side_effect=lambda content, _meta: content),
    )
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)

    result = await server_module.breath(query="", max_tokens=300, max_results=3)

    assert "[bucket_id:small-core]" in result
    assert "[bucket_id:oversized-core]" not in result


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


async def _run_time_aware_breath(
    monkeypatch,
    server_module,
    bucket,
    query,
    score,
    *,
    history_mode=False,
    current_state_override=False,
):
    monkeypatch.setattr(server_module.bucket_mgr, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(server_module.bucket_mgr, "list_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(server_module.bucket_mgr, "get", AsyncMock(return_value=bucket))
    monkeypatch.setattr(server_module.bucket_mgr, "touch", AsyncMock(return_value=True))
    monkeypatch.setattr(
        server_module.embedding_engine,
        "search_similar",
        AsyncMock(return_value=[(bucket["id"], score)]),
    )
    monkeypatch.setattr(
        server_module.dehydrator,
        "dehydrate",
        AsyncMock(side_effect=lambda content, _meta: content),
    )
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)
    return await server_module.breath(
        query=query,
        max_results=3,
        max_tokens=5000,
        include_recent=0,
        history_mode=history_mode,
        current_state_override=current_state_override,
    )


@pytest.mark.asyncio
async def test_breath_time_canary_old_coffee_is_not_injected(monkeypatch, server_module):
    bucket = _time_canary_bucket(
        "coffee-old",
        "今天买了特调咖啡，正在慢慢喝。",
        4,
    )
    result = await _run_time_aware_breath(
        monkeypatch, server_module, bucket, "我刚忙完", 0.86
    )

    assert "coffee-old" not in result
    assert "特调咖啡" not in result


@pytest.mark.asyncio
async def test_breath_time_canary_current_message_overrides_old_state(
    monkeypatch, server_module
):
    bucket = _time_canary_bucket(
        "insomnia-old",
        "昨晚失眠，现在还是很累。",
        4,
    )
    result = await _run_time_aware_breath(
        monkeypatch, server_module, bucket, "昨晚睡得很好，今天很精神。", 0.93
    )

    assert "insomnia-old" not in result
    assert "昨晚失眠" not in result


@pytest.mark.asyncio
async def test_breath_time_canary_old_stable_fact_survives(monkeypatch, server_module):
    bucket = _time_canary_bucket(
        "stable-tea",
        "用户长期喜欢无糖乌龙茶。",
        184,
        bucket_type="permanent",
        tags=["偏好"],
    )
    result = await _run_time_aware_breath(
        monkeypatch, server_module, bucket, "我平时喜欢喝什么？", 0.78
    )

    assert "[Stable memory]" in result
    assert "[bucket_id:stable-tea]" in result
    assert "无糖乌龙茶" in result


@pytest.mark.asyncio
async def test_breath_time_canary_historical_query_reenables_event(
    monkeypatch, server_module
):
    bucket = _time_canary_bucket(
        "coffee-history",
        "今天买了榛果特调咖啡。",
        4,
    )
    result = await _run_time_aware_breath(
        monkeypatch, server_module, bucket, "前几天我买了什么咖啡？", 0.86
    )

    assert "[Historical memory]" in result
    assert "[bucket_id:coffee-history]" in result
    assert "榛果特调咖啡" in result
    assert "Never present it as a current state" in result


@pytest.mark.asyncio
async def test_breath_time_canary_relative_time_is_anchored(monkeypatch, server_module):
    bucket = _time_canary_bucket(
        "relative-history",
        "昨天去了山里，今晚住在木屋。",
        8,
    )
    result = await _run_time_aware_breath(
        monkeypatch, server_module, bucket, "上次去山里是什么时候？", 0.82
    )

    assert "[Historical memory]" in result
    assert "Recorded:" in result
    assert "Asia/Shanghai" in result
    assert "Relative-time words are anchored to Recorded" in result


@pytest.mark.asyncio
async def test_breath_generic_expired_transient_is_filtered_without_content_clues(
    monkeypatch, server_module
):
    bucket = _time_canary_bucket(
        "opaque-expired",
        "opaque-payload",
        3,
        memory_lifecycle="transient_state",
    )
    result = await _run_time_aware_breath(
        monkeypatch, server_module, bucket, "opaque-query", 0.99
    )

    assert "opaque-expired" not in result
    assert "opaque-payload" not in result


@pytest.mark.asyncio
async def test_breath_generic_unexpired_transient_is_only_candidate_context(
    monkeypatch, server_module
):
    bucket = _time_canary_bucket(
        "opaque-recent",
        "opaque-payload",
        0,
        memory_lifecycle="transient_state",
        valid_until=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    result = await _run_time_aware_breath(
        monkeypatch, server_module, bucket, "opaque-query", 0.9
    )

    assert "[Recent reported state]" in result
    assert "Unexpired does not mean still true" in result
    assert "discard the recalled state even when it is unexpired" in result


@pytest.mark.asyncio
async def test_breath_generic_current_state_override_filters_unexpired_transient(
    monkeypatch, server_module
):
    bucket = _time_canary_bucket(
        "opaque-overridden",
        "opaque-payload",
        0,
        memory_lifecycle="transient_state",
        valid_until=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    result = await _run_time_aware_breath(
        monkeypatch,
        server_module,
        bucket,
        "opaque-current-message",
        0.99,
        current_state_override=True,
    )

    assert "opaque-overridden" not in result
    assert "opaque-payload" not in result


@pytest.mark.asyncio
async def test_breath_generic_explicit_history_mode_reenables_expired_transient(
    monkeypatch, server_module
):
    bucket = _time_canary_bucket(
        "opaque-history",
        "opaque-payload",
        30,
        memory_lifecycle="transient_state",
    )
    result = await _run_time_aware_breath(
        monkeypatch,
        server_module,
        bucket,
        "opaque-query-without-history-words",
        0.9,
        history_mode=True,
    )

    assert "[Historical memory]" in result
    assert "[bucket_id:opaque-history]" in result


@pytest.mark.asyncio
async def test_noquery_surface_filters_expired_transient(monkeypatch, server_module):
    bucket = _time_canary_bucket(
        "opaque-surface-expired",
        "opaque-payload",
        3,
        memory_lifecycle="transient_state",
    )
    monkeypatch.setattr(server_module.bucket_mgr, "list_all", AsyncMock(return_value=[bucket]))
    monkeypatch.setattr(
        server_module.dehydrator,
        "dehydrate",
        AsyncMock(side_effect=lambda content, _meta: content),
    )
    monkeypatch.setattr(server_module.decay_engine, "calculate_score", lambda _meta: 1.0)
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)

    result = await server_module.breath(query="", max_results=2, max_tokens=5000)

    assert "opaque-surface-expired" not in result
    assert "opaque-payload" not in result


@pytest.mark.asyncio
async def test_hold_passes_explicit_temporal_metadata_from_existing_analysis_call(
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
            "domain": ["test"],
            "valence": 0.5,
            "arousal": 0.5,
            "tags": [],
            "suggested_name": "opaque",
            "memory_lifecycle": "event",
        }),
    )
    monkeypatch.setattr(server_module.bucket_mgr, "search", AsyncMock(return_value=[]))
    create = AsyncMock(return_value="opaque-id")
    monkeypatch.setattr(server_module.bucket_mgr, "create", create)
    monkeypatch.setattr(
        server_module.embedding_engine,
        "generate_and_store",
        AsyncMock(return_value=None),
    )

    await server_module.hold(content="opaque-payload")

    assert create.await_args.kwargs["memory_lifecycle"] == "event"
    assert create.await_args.kwargs["source_timestamp"].endswith("+00:00")


@pytest.mark.asyncio
async def test_gale_proxy_switch_off_blocks_every_gale_path(monkeypatch, server_module):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    forwarded = 0

    async def should_not_run(_request):
        nonlocal forwarded
        forwarded += 1
        return PlainTextResponse("forwarded")

    app = Starlette(routes=[
        Route("/gale-dash/{path:path}", should_not_run, methods=["GET", "POST"]),
        Route("/api/night_fall/generate_gale", should_not_run, methods=["POST"]),
        Route("/outside", lambda _r: PlainTextResponse("outside"), methods=["GET"]),
    ])
    monkeypatch.setattr(server_module, "_GALE_PROXY_ENABLED", False)
    guarded_app = server_module.install_gale_dash_guard(app)
    transport = httpx.ASGITransport(app=guarded_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
        dashboard = await client.get("/gale-dash/dashboard")
        generate = await client.post("/api/night_fall/generate_gale")
        outside = await client.get("/outside")

    assert dashboard.status_code == 404
    assert generate.status_code == 404
    assert outside.status_code == 200
    assert forwarded == 0


@pytest.mark.asyncio
async def test_breath_include_pinned_false_leaves_budget_for_search(monkeypatch, server_module):
    pinned = {
        "id": "pinned-core",
        "metadata": {"pinned": True, "created": "2026-08-01T00:00:00Z"},
        "content": "核心" * 250,
    }
    match = {
        "id": "search-match",
        "metadata": {"created": "2026-09-20T00:00:00Z", "memory_lifecycle": "stable_fact"},
        "content": "检索" * 40,
        "score": 90,
    }
    monkeypatch.setattr(server_module.bucket_mgr, "list_all", AsyncMock(return_value=[pinned, match]))
    monkeypatch.setattr(server_module.bucket_mgr, "search", AsyncMock(return_value=[match]))
    monkeypatch.setattr(server_module.bucket_mgr, "touch", AsyncMock(return_value=True))
    monkeypatch.setattr(server_module.embedding_engine, "search_similar", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        server_module.dehydrator, "dehydrate", AsyncMock(side_effect=lambda content, _meta: content)
    )
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)

    with_pinned = await server_module.breath(query="项目", max_results=3, max_tokens=900)
    without_pinned = await server_module.breath(
        query="项目", max_results=3, max_tokens=900, include_pinned=False
    )

    assert "[bucket_id:pinned-core]" in with_pinned
    assert "[bucket_id:pinned-core]" not in without_pinned
    assert "[bucket_id:search-match]" in without_pinned


@pytest.mark.asyncio
async def test_api_pinned_returns_only_pinned_within_domain(monkeypatch, server_module):
    evan = {"id": "evan-pin", "metadata": {"pinned": True, "domain": ["tg-private"]}, "content": "A"}
    other = {"id": "other-pin", "metadata": {"pinned": True, "domain": ["tg-gale"]}, "content": "B"}
    plain = {"id": "plain", "metadata": {"domain": ["tg-private"]}, "content": "C"}
    monkeypatch.setattr(server_module.bucket_mgr, "list_all", AsyncMock(return_value=[evan, other, plain]))
    monkeypatch.setattr(
        server_module.dehydrator, "dehydrate", AsyncMock(side_effect=lambda content, _meta: content)
    )
    from starlette.requests import Request as StarletteRequest

    def req(query):
        return StarletteRequest({"type": "http", "method": "GET", "path": "/api/pinned",
                                 "query_string": query, "headers": []})

    everything = json.loads((await server_module.api_pinned(req(b""))).body)
    scoped = json.loads((await server_module.api_pinned(req(b"domain=tg-private"))).body)
    assert everything["count"] == 2 and "plain" not in everything["text"]
    assert scoped["count"] == 1 and "evan-pin" in scoped["text"] and "other-pin" not in scoped["text"]
