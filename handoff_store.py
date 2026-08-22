"""Small, structured current-task state kept outside Ombre memory buckets."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator


HANDOFF_STATUSES = frozenset(
    {"active", "pending", "blocked", "stale", "done", "dropped"}
)
READABLE_STATUSES = frozenset({"active", "pending", "blocked"})
STALE_ELIGIBLE_STATUSES = frozenset({"active", "pending"})

MAX_TEXT_LENGTHS = {
    "current_topic": 160,
    "active_goal": 500,
    "current_state": 1000,
    "current_scene": 300,
    "last_meaningful_user_intent": 500,
}
MAX_LIST_ITEMS = 20
MAX_LIST_ITEM_CHARS = 300
MAX_HANDOFF_TOTAL_CHARS = 6000
MAX_STALE_AFTER_SECONDS = 365 * 24 * 60 * 60

_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _as_utc(value: datetime | float | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("time values must include a timezone")
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), timezone.utc)


def _format_time(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _clean_text(value: str, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) > max_chars:
        raise ValueError(f"{field} must be at most {max_chars} characters")
    return cleaned


def _clean_list(value: list[str], field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a string list")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{field} must contain at most {MAX_LIST_ITEMS} items")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _clean_text(item, f"{field} item", MAX_LIST_ITEM_CHARS)
        if not normalized:
            raise ValueError(f"{field} items must not be empty")
        if normalized not in seen:
            cleaned.append(normalized)
            seen.add(normalized)
    return cleaned


class HandoffStore:
    """One compact current handoff per allowed agent, stored in SQLite."""

    def __init__(self, db_path: str, allowed_agent_ids: Iterable[str]):
        self.db_path = str(db_path)
        allowed = {str(agent).strip().lower() for agent in allowed_agent_ids if str(agent).strip()}
        if not allowed:
            raise ValueError("allowed_agent_ids must not be empty")
        for agent_id in allowed:
            if not _AGENT_ID_RE.fullmatch(agent_id):
                raise ValueError(f"invalid allowed agent_id: {agent_id}")
        self.allowed_agent_ids = frozenset(allowed)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handoffs (
                    agent_id TEXT PRIMARY KEY,
                    current_topic TEXT NOT NULL DEFAULT '',
                    active_goal TEXT NOT NULL DEFAULT '',
                    current_state TEXT NOT NULL DEFAULT '',
                    unresolved_json TEXT NOT NULL DEFAULT '[]',
                    recent_decisions_json TEXT NOT NULL DEFAULT '[]',
                    current_scene TEXT NOT NULL DEFAULT '',
                    last_meaningful_user_intent TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN (
                            'active', 'pending', 'blocked',
                            'stale', 'done', 'dropped'
                        )),
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_handoffs_status_updated_at
                ON handoffs (status, updated_at)
                """
            )

    def _agent_id(self, value: str) -> str:
        agent_id = str(value or "").strip().lower()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise ValueError("agent_id must be a lowercase identifier")
        if agent_id not in self.allowed_agent_ids:
            allowed = ", ".join(sorted(self.allowed_agent_ids))
            raise ValueError(f"agent_id is not enabled; allowed: {allowed}")
        return agent_id

    @staticmethod
    def _decode_list(value: str, field: str) -> list[str]:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid stored {field}") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise RuntimeError(f"invalid stored {field}")
        return parsed

    def _row_to_handoff(self, row: sqlite3.Row) -> dict:
        return {
            "agent_id": row["agent_id"],
            "current_topic": row["current_topic"],
            "active_goal": row["active_goal"],
            "current_state": row["current_state"],
            "unresolved": self._decode_list(row["unresolved_json"], "unresolved"),
            "recent_decisions": self._decode_list(
                row["recent_decisions_json"], "recent_decisions"
            ),
            "current_scene": row["current_scene"],
            "last_meaningful_user_intent": row["last_meaningful_user_intent"],
            "status": row["status"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _empty(agent_id: str, reason: str, *, status: str | None = None) -> dict:
        result = {
            "active": False,
            "agent_id": agent_id,
            "reason": reason,
            "handoff": None,
        }
        if status:
            result["status"] = status
        return result

    def read(self, agent_id: str, *, now: datetime | float | None = None) -> dict:
        agent_id = self._agent_id(agent_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM handoffs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            return self._empty(agent_id, "missing")
        handoff = self._row_to_handoff(row)
        if handoff["status"] not in READABLE_STATUSES:
            return self._empty(agent_id, "inactive", status=handoff["status"])
        if handoff["expires_at"]:
            expiry = _parse_time(handoff["expires_at"], "stored expires_at")
            if expiry <= _as_utc(now):
                return self._empty(agent_id, "expired", status=handoff["status"])
        return {"active": True, "agent_id": agent_id, "handoff": handoff}

    def update(
        self,
        agent_id: str,
        *,
        current_topic: str | None = None,
        active_goal: str | None = None,
        current_state: str | None = None,
        unresolved: list[str] | None = None,
        recent_decisions: list[str] | None = None,
        current_scene: str | None = None,
        last_meaningful_user_intent: str | None = None,
        status: str | None = None,
        expires_at: str | None = None,
        now: datetime | float | None = None,
    ) -> dict:
        agent_id = self._agent_id(agent_id)
        updates: dict[str, object] = {}
        text_values = {
            "current_topic": current_topic,
            "active_goal": active_goal,
            "current_state": current_state,
            "current_scene": current_scene,
            "last_meaningful_user_intent": last_meaningful_user_intent,
        }
        for field, value in text_values.items():
            if value is not None:
                updates[field] = _clean_text(value, field, MAX_TEXT_LENGTHS[field])
        if unresolved is not None:
            updates["unresolved_json"] = json.dumps(
                _clean_list(unresolved, "unresolved"), ensure_ascii=False
            )
        if recent_decisions is not None:
            updates["recent_decisions_json"] = json.dumps(
                _clean_list(recent_decisions, "recent_decisions"), ensure_ascii=False
            )
        if status is not None:
            clean_status = str(status).strip().lower()
            if clean_status not in HANDOFF_STATUSES:
                raise ValueError(
                    "status must be active, pending, blocked, stale, done, or dropped"
                )
            updates["status"] = clean_status
        if expires_at is not None:
            clean_expiry = str(expires_at).strip()
            updates["expires_at"] = (
                _format_time(_parse_time(clean_expiry, "expires_at"))
                if clean_expiry
                else None
            )

        updated_at = _format_time(_as_utc(now))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM handoffs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                values: dict[str, object] = {
                    "agent_id": agent_id,
                    "current_topic": "",
                    "active_goal": "",
                    "current_state": "",
                    "unresolved_json": "[]",
                    "recent_decisions_json": "[]",
                    "current_scene": "",
                    "last_meaningful_user_intent": "",
                    "status": "active",
                    "updated_at": updated_at,
                    "expires_at": None,
                }
            else:
                values = dict(row)
                values["updated_at"] = updated_at
            values.update(updates)
            values["updated_at"] = updated_at
            payload_chars = sum(
                len(str(values[field] or ""))
                for field in (
                    "current_topic",
                    "active_goal",
                    "current_state",
                    "unresolved_json",
                    "recent_decisions_json",
                    "current_scene",
                    "last_meaningful_user_intent",
                )
            )
            if payload_chars > MAX_HANDOFF_TOTAL_CHARS:
                raise ValueError(
                    f"handoff content must be at most {MAX_HANDOFF_TOTAL_CHARS} characters"
                )
            connection.execute(
                """
                INSERT INTO handoffs (
                    agent_id, current_topic, active_goal, current_state,
                    unresolved_json, recent_decisions_json, current_scene,
                    last_meaningful_user_intent, status, updated_at, expires_at
                ) VALUES (
                    :agent_id, :current_topic, :active_goal, :current_state,
                    :unresolved_json, :recent_decisions_json, :current_scene,
                    :last_meaningful_user_intent, :status, :updated_at, :expires_at
                )
                ON CONFLICT(agent_id) DO UPDATE SET
                    current_topic = excluded.current_topic,
                    active_goal = excluded.active_goal,
                    current_state = excluded.current_state,
                    unresolved_json = excluded.unresolved_json,
                    recent_decisions_json = excluded.recent_decisions_json,
                    current_scene = excluded.current_scene,
                    last_meaningful_user_intent = excluded.last_meaningful_user_intent,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                values,
            )
            saved = connection.execute(
                "SELECT * FROM handoffs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return {"updated": True, "handoff": self._row_to_handoff(saved)}

    def complete(
        self,
        agent_id: str,
        item: str = "",
        *,
        now: datetime | float | None = None,
    ) -> dict:
        agent_id = self._agent_id(agent_id)
        clean_item = _clean_text(item, "item", MAX_LIST_ITEM_CHARS)
        updated_at = _format_time(_as_utc(now))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM handoffs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return {"completed": False, "agent_id": agent_id, "reason": "missing"}
            if clean_item:
                unresolved = self._decode_list(row["unresolved_json"], "unresolved")
                if clean_item not in unresolved:
                    return {
                        "completed": False,
                        "agent_id": agent_id,
                        "reason": "item_not_found",
                    }
                unresolved = [value for value in unresolved if value != clean_item]
                connection.execute(
                    """
                    UPDATE handoffs
                    SET unresolved_json = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (json.dumps(unresolved, ensure_ascii=False), updated_at, agent_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE handoffs SET status = 'done', updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (updated_at, agent_id),
                )
            saved = connection.execute(
                "SELECT * FROM handoffs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return {
            "completed": True,
            "agent_id": agent_id,
            "completed_item": clean_item or None,
            "handoff": self._row_to_handoff(saved),
        }

    def clear(self, agent_id: str) -> dict:
        agent_id = self._agent_id(agent_id)
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM handoffs WHERE agent_id = ?", (agent_id,)
            )
        return {"cleared": cursor.rowcount > 0, "agent_id": agent_id}

    def expire_stale(
        self,
        agent_id: str,
        stale_after_seconds: int,
        *,
        now: datetime | float | None = None,
    ) -> dict:
        agent_id = self._agent_id(agent_id)
        if isinstance(stale_after_seconds, bool):
            raise ValueError("stale_after_seconds must be an integer")
        try:
            stale_after = int(stale_after_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("stale_after_seconds must be an integer") from exc
        if stale_after < 60 or stale_after > MAX_STALE_AFTER_SECONDS:
            raise ValueError(
                f"stale_after_seconds must be between 60 and {MAX_STALE_AFTER_SECONDS}"
            )
        threshold = _as_utc(now).timestamp() - stale_after
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM handoffs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                return {"staled": False, "agent_id": agent_id, "reason": "missing"}
            if row["status"] not in STALE_ELIGIBLE_STATUSES:
                return {
                    "staled": False,
                    "agent_id": agent_id,
                    "reason": "status_not_eligible",
                    "status": row["status"],
                }
            last_update = _parse_time(row["updated_at"], "stored updated_at").timestamp()
            if last_update > threshold:
                return {
                    "staled": False,
                    "agent_id": agent_id,
                    "reason": "recent",
                    "status": row["status"],
                }
            connection.execute(
                "UPDATE handoffs SET status = 'stale' WHERE agent_id = ?",
                (agent_id,),
            )
        return {
            "staled": True,
            "agent_id": agent_id,
            "previous_status": row["status"],
            "status": "stale",
        }
