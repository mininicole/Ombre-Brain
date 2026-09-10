import asyncio
import json

from freeze_guard import (
    freeze_all_enabled,
    frozen_health_payload,
    install_freeze_all_guard,
)


def test_freeze_flag_and_sentinel_are_process_scoped(tmp_path):
    sentinel = tmp_path / "freeze"
    assert not freeze_all_enabled({})
    assert freeze_all_enabled({"OMBRE_FREEZE_ALL": "true"})
    assert not freeze_all_enabled({"OMBRE_FREEZE_ALL_SENTINEL": str(sentinel)})
    sentinel.touch()
    assert freeze_all_enabled({"OMBRE_FREEZE_ALL_SENTINEL": str(sentinel)})


def test_frozen_health_payload_contains_no_memory_data():
    assert frozen_health_payload() == {
        "status": "frozen",
        "frozen": True,
        "reads": False,
        "writes": False,
    }


def test_guard_blocks_http_reads_and_writes_but_allows_health():
    called = []

    async def app(scope, receive, send):
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = install_freeze_all_guard(app, enabled=True)

    async def request(path):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await guarded({"type": "http", "path": path}, receive, send)
        return sent

    for path in ("/mcp", "/api/recall", "/api/remember", "/api/handoff", "/dashboard"):
        messages = asyncio.run(request(path))
        assert messages[0]["status"] == 503
        assert json.loads(messages[1]["body"])["error"] == "gale_memory_frozen"

    health = asyncio.run(request("/health"))
    assert health[0]["status"] == 200
    assert called == ["/health"]


def test_guard_is_noop_when_disabled_and_install_is_idempotent():
    called = []

    async def app(scope, receive, send):
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guarded = install_freeze_all_guard(app, enabled=False)
    assert install_freeze_all_guard(guarded, enabled=True) is guarded

    async def run():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await guarded({"type": "http", "path": "/api/remember"}, receive, send)
        return sent

    messages = asyncio.run(run())
    assert messages[0]["status"] == 204
    assert called == ["/api/remember"]


def test_start_script_enables_freeze_only_for_gale_process():
    script = open("start.sh", encoding="utf-8").read()
    assert script.count("OMBRE_FREEZE_ALL_SENTINEL=") == 1
    assert "GALE_FREEZE_SENTINEL=/app/buckets/gale/.migration-freeze-all" in script
    assert script.index("OMBRE_PID=$!") < script.index("OMBRE_FREEZE_ALL_SENTINEL=")


def test_night_fall_launcher_installs_freeze_on_cloud_asgi_path():
    launcher = open("ombre_nightfall_launcher.py", encoding="utf-8").read()
    dash_guard = "app = ombre_server.install_gale_dash_guard(app)"
    freeze_guard = "app = ombre_server.install_freeze_all_guard(app)"
    uvicorn_call = "return upstream_uvicorn_run("

    assert launcher.count(freeze_guard) == 1
    assert launcher.index(dash_guard) < launcher.index(freeze_guard)
    assert launcher.index(freeze_guard) < launcher.index(uvicorn_call)
