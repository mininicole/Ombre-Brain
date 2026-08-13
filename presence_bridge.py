"""Short-lived, non-memory presence for Guardian proactive messages."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone


DEFAULT_TTL_SECONDS = 72 * 60 * 60
MAX_TOPIC_CHARS = 160
ALLOWED_SOURCES = {"chat", "work", "codex"}
CONTEXT_SOURCES = {"chat", "work"}
BEAT_SOURCE_MAP = {"chatctx": "chat", "workctx": "work"}


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()


def _clean_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", str(topic or "")).strip()[:MAX_TOPIC_CHARS]


def _clean_source(source: str) -> str:
    value = str(source or "").strip().lower()
    if value not in ALLOWED_SOURCES:
        raise ValueError("source must be chat, work, or codex")
    return value


def beat_presence_request(msg: str, source: str):
    """Decode the cached beat tool's reserved presence-only source values."""
    mapped = BEAT_SOURCE_MAP.get(str(source or "").strip().lower())
    if not mapped:
        return None
    topic = _clean_topic(msg)
    if not topic:
        raise ValueError("msg must contain a topic for chatctx/workctx")
    return {"source": mapped, "topic": topic}


def _load_sources(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("sources"), dict):
        return payload["sources"]
    # Version 1 stored a single record. Preserve it during the transition.
    source = str(payload.get("source") or "").lower()
    if source in ALLOWED_SOURCES and payload.get("last_user_at"):
        return {source: payload}
    return {}


def write_presence(
    path: str,
    topic: str = "",
    source: str = "codex",
    *,
    now: float | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict:
    """Atomically update one source without overwriting the other surfaces."""
    source = _clean_source(source)
    clean_topic = _clean_topic(topic)
    if source in CONTEXT_SOURCES and not clean_topic:
        raise ValueError("topic must not be empty for chat or work")
    # Codex is presence-only by design. Discard any supplied technical topic.
    if source == "codex":
        clean_topic = ""
    timestamp = time.time() if now is None else float(now)
    ttl = max(60, min(int(ttl_seconds), 7 * 24 * 60 * 60))
    record = {
        "source": source,
        "last_user_at": _iso_utc(timestamp),
        "expires_at": _iso_utc(timestamp + ttl),
        "topic": clean_topic,
    }
    sources = _load_sources(path)
    sources[source] = record
    payload = {"version": 2, "sources": sources}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    return record


def read_presence(path: str, *, now: float | None = None) -> dict:
    """Return active source records plus separate activity/context winners."""
    timestamp = time.time() if now is None else float(now)
    active = []
    for source, raw in _load_sources(path).items():
        if source not in ALLOWED_SOURCES or not isinstance(raw, dict):
            continue
        try:
            last_ts = _parse_time(raw.get("last_user_at", ""))
            expiry_ts = _parse_time(raw.get("expires_at", ""))
        except (TypeError, ValueError):
            continue
        if expiry_ts <= timestamp or last_ts > timestamp + 5 * 60:
            continue
        topic = _clean_topic(raw.get("topic", ""))
        if source == "codex":
            topic = ""
        if source in CONTEXT_SOURCES and not topic:
            continue
        active.append(
            {
                "source": source,
                "last_user_at": _iso_utc(last_ts),
                "expires_at": _iso_utc(expiry_ts),
                "topic": topic,
            }
        )
    active.sort(key=lambda item: item["last_user_at"], reverse=True)
    if not active:
        return {"active": False, "reason": "missing_or_expired", "sources": []}
    contexts = [item for item in active if item["source"] in CONTEXT_SOURCES]
    return {
        "active": True,
        "version": 2,
        "sources": active,
        "latest_activity": active[0],
        "latest_context": contexts[0] if contexts else None,
    }
