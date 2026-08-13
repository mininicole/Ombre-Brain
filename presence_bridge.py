"""Short-lived, non-memory context for Guardian proactive messages."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone


DEFAULT_TTL_SECONDS = 72 * 60 * 60
MAX_TOPIC_CHARS = 160


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", str(topic or "")).strip()[:MAX_TOPIC_CHARS]


def _clean_source(source: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]", "", str(source or "codex"))[:32]
    return value or "codex"


def write_presence(
    path: str,
    topic: str,
    source: str = "codex",
    *,
    now: float | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict:
    """Atomically replace the transient presence record."""
    clean_topic = _clean_topic(topic)
    if not clean_topic:
        raise ValueError("topic must not be empty")
    timestamp = time.time() if now is None else float(now)
    ttl = max(60, min(int(ttl_seconds), 7 * 24 * 60 * 60))
    payload = {
        "version": 1,
        "source": _clean_source(source),
        "last_user_at": _iso_utc(timestamp),
        "expires_at": _iso_utc(timestamp + ttl),
        "topic": clean_topic,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    return payload


def read_presence(path: str, *, now: float | None = None) -> dict:
    """Return an active record, or a reason-only inactive response."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"active": False, "reason": "missing"}
    except (OSError, ValueError, TypeError):
        return {"active": False, "reason": "invalid"}

    if not isinstance(payload, dict):
        return {"active": False, "reason": "invalid"}
    topic = _clean_topic(payload.get("topic", ""))
    try:
        expires_at = datetime.fromisoformat(
            str(payload.get("expires_at", "")).replace("Z", "+00:00")
        ).timestamp()
        last_user_at = datetime.fromisoformat(
            str(payload.get("last_user_at", "")).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return {"active": False, "reason": "invalid"}

    timestamp = time.time() if now is None else float(now)
    if not topic or expires_at <= timestamp:
        return {"active": False, "reason": "expired"}
    if last_user_at > timestamp + 5 * 60:
        return {"active": False, "reason": "future_timestamp"}

    return {
        "active": True,
        "version": 1,
        "source": _clean_source(payload.get("source", "codex")),
        "last_user_at": _iso_utc(last_user_at),
        "expires_at": _iso_utc(expires_at),
        "topic": topic,
    }
