import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import server
from dehydrator import Dehydrator
from embedding_engine import EmbeddingEngine
from handoff_store import HandoffStore


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_sqlite_components_open_query_only_without_mutating(tmp_path, monkeypatch):
    buckets = tmp_path / "buckets"
    buckets.mkdir()
    embeddings = buckets / "embeddings.db"
    cache = buckets / "dehydration_cache.db"

    with sqlite3.connect(embeddings) as conn:
        conn.execute(
            "CREATE TABLE embeddings (bucket_id TEXT PRIMARY KEY, embedding TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE handoffs (agent_id TEXT PRIMARY KEY, current_topic TEXT NOT NULL DEFAULT '', active_goal TEXT NOT NULL DEFAULT '', current_state TEXT NOT NULL DEFAULT '', unresolved_json TEXT NOT NULL DEFAULT '[]', recent_decisions_json TEXT NOT NULL DEFAULT '[]', current_scene TEXT NOT NULL DEFAULT '', last_meaningful_user_intent TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT)"
        )
    with sqlite3.connect(cache) as conn:
        conn.execute(
            "CREATE TABLE dehydration_cache (content_hash TEXT PRIMARY KEY, summary TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )

    before = (_sha256(embeddings), _sha256(cache))
    monkeypatch.setenv("OMBRE_READ_ONLY", "1")
    config = {
        "buckets_dir": str(buckets),
        "dehydration": {"api_key": "", "model": "test"},
        "embedding": {"api_key": "", "enabled": False},
    }

    engine = EmbeddingEngine(config)
    dehydrator = Dehydrator(config)
    handoff = HandoffStore(str(embeddings), ["gale"])
    assert asyncio.run(engine.get_embedding("missing")) is None
    assert handoff.read("gale")["active"] is False
    dehydrator._set_cached_summary("body", "summary")

    with pytest.raises(sqlite3.OperationalError):
        handoff.update("gale", current_state="blocked")
    assert (_sha256(embeddings), _sha256(cache)) == before


def test_bucket_touch_is_noop_in_read_only(test_config, monkeypatch):
    from bucket_manager import BucketManager

    manager = BucketManager(test_config)
    bucket_id = asyncio.run(manager.create(content="opaque state", tags=["x"]))
    path = manager._find_bucket_file(bucket_id)
    before = _sha256(path)
    monkeypatch.setenv("OMBRE_READ_ONLY", "true")
    asyncio.run(manager.touch(bucket_id))
    assert _sha256(path) == before


def test_write_tools_fail_closed_before_business_logic(monkeypatch):
    monkeypatch.setenv("OMBRE_READ_ONLY", "yes")
    expected = {"ok": False, "error": "memory_read_only"}
    assert json.loads(asyncio.run(server.hold(content="must not persist"))) == expected
    assert json.loads(asyncio.run(server.grow(content="must not persist"))) == expected
    assert json.loads(asyncio.run(server.presence(topic="x", action="update"))) == expected
    assert json.loads(asyncio.run(server.beat(msg="x"))) == expected


def test_read_only_http_guard_blocks_mutation_and_passes_read(monkeypatch):
    monkeypatch.setenv("OMBRE_READ_ONLY", "1")
    passed = []

    async def downstream(scope, receive, send):
        passed.append((scope["method"], scope["path"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guard = server.MemoryReadOnlyGuardMiddleware(downstream)

    async def invoke(method, path):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await guard(
            {"type": "http", "method": method, "path": path}, receive, send
        )
        return sent

    blocked = asyncio.run(invoke("POST", "/api/remember"))
    assert blocked[0]["status"] == 503
    assert passed == []

    allowed = asyncio.run(invoke("POST", "/api/recall"))
    assert allowed[0]["status"] == 204
    assert passed == [("POST", "/api/recall")]
