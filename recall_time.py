"""Time- and state-aware reranking for recalled Ombre buckets.

This module deliberately runs after semantic retrieval.  It never changes an
embedding and never rewrites memory content; it only decides whether and how a
retrieved bucket may be presented to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import math
from pathlib import Path
import re
from typing import Iterable
from zoneinfo import ZoneInfo


LIFECYCLES = {"stable_fact", "event", "transient_state"}
TIME_FIELDS = ("source_timestamp", "source_date", "created_at", "created")
VALID_UNTIL_FIELDS = ("valid_until", "expires_at")
DEFAULT_TRANSIENT_VALID_HOURS = 48.0
RELATIVE_TIME_TERMS = (
    "今天",
    "昨天",
    "昨晚",
    "刚刚",
    "刚才",
    "明天",
    "今晚",
    "今早",
    "这两天",
    "最近",
    "现在",
    "此刻",
    "正在",
    "today",
    "yesterday",
    "tomorrow",
    "just now",
    "right now",
    "currently",
)
HISTORICAL_QUERY_TERMS = (
    "以前",
    "之前",
    "上次",
    "前几天",
    "那天",
    "当时",
    "曾经",
    "过去",
    "还记得",
    "回忆",
    "哪天",
    "什么时候",
    "history",
    "historical",
    "before",
    "previous",
    "last time",
    "days ago",
    "used to",
)
STABLE_HINTS = (
    "偏好",
    "身份",
    "规则",
    "原则",
    "长期",
    "喜欢",
    "不喜欢",
    "名字",
    "生日",
    "家人",
    "belief",
    "preference",
    "identity",
    "always",
)


@dataclass(frozen=True)
class RecallDecision:
    semantic_score: float
    time_weight: float
    time_state_adjustment: float
    final_score: float
    inject: bool
    reason: str
    lifecycle: str
    recorded_at: datetime | None
    recorded_source: str
    time_confidence: str
    age_days: float | None
    valid_until: datetime | None
    validity_source: str
    historical_query: bool
    contains_relative_time: bool


def _normalized_text(values: Iterable[object]) -> str:
    return " ".join(str(value or "").casefold() for value in values)


def query_requests_history(query: str) -> bool:
    normalized = str(query or "").casefold()
    return any(term in normalized for term in HISTORICAL_QUERY_TERMS)


def contains_relative_time(content: str) -> bool:
    normalized = str(content or "").casefold()
    return any(term in normalized for term in RELATIVE_TIME_TERMS)


def classify_lifecycle(bucket: dict) -> str:
    metadata = bucket.get("metadata", {}) or {}
    for field in ("memory_lifecycle", "lifecycle", "memory_type"):
        explicit = str(metadata.get(field) or "").strip().casefold()
        if explicit in LIFECYCLES:
            return explicit

    bucket_type = str(metadata.get("type") or "").strip().casefold()
    if metadata.get("pinned") or metadata.get("protected") or bucket_type == "permanent":
        return "stable_fact"

    # For legacy buckets without explicit lifecycle, a relative-time statement
    # is safer as transient even if old tags happen to look preference-like.
    # Explicit metadata and permanent/pinned buckets above still win.
    if contains_relative_time(str(bucket.get("content") or "")):
        return "transient_state"

    tags = metadata.get("tags") or []
    domains = metadata.get("domain") or []
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(domains, str):
        domains = [domains]
    stable_haystack = _normalized_text([*tags, *domains, metadata.get("name", "")])
    if any(hint in stable_haystack for hint in STABLE_HINTS):
        return "stable_fact"

    return "event"


def _parse_datetime(value: object, naive_timezone: ZoneInfo) -> tuple[datetime | None, str]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif value is None:
        return None, "missing"
    else:
        text = str(value).strip()
        if not text:
            return None, "missing"
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(text), time.min)
            except ValueError:
                return None, "invalid"

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=naive_timezone), "timezone_inferred"
    return parsed, "explicit"


def resolve_recorded_at(
    bucket: dict,
    *,
    naive_source_timezone: str = "UTC",
) -> tuple[datetime | None, str, str]:
    metadata = bucket.get("metadata", {}) or {}
    naive_zone = ZoneInfo(naive_source_timezone)
    for field in TIME_FIELDS:
        parsed, confidence = _parse_datetime(metadata.get(field), naive_zone)
        if parsed is not None:
            return parsed.astimezone(timezone.utc), field, confidence

    path_value = bucket.get("path")
    if path_value:
        try:
            mtime = Path(str(path_value)).stat().st_mtime
            return (
                datetime.fromtimestamp(mtime, timezone.utc),
                "file_mtime",
                "filesystem_fallback",
            )
        except OSError:
            pass
    return None, "missing", "missing"


def resolve_valid_until(
    bucket: dict,
    *,
    naive_source_timezone: str = "UTC",
) -> tuple[datetime | None, str]:
    metadata = bucket.get("metadata", {}) or {}
    naive_zone = ZoneInfo(naive_source_timezone)
    for field in VALID_UNTIL_FIELDS:
        parsed, _confidence = _parse_datetime(metadata.get(field), naive_zone)
        if parsed is not None:
            return parsed.astimezone(timezone.utc), field
    return None, "default_window"


def evaluate_recall_candidate(
    bucket: dict,
    query: str,
    semantic_score: float,
    *,
    now: datetime | None = None,
    naive_source_timezone: str = "UTC",
    history_mode: bool | None = None,
    current_state_override: bool = False,
) -> RecallDecision:
    now_value = now or datetime.now(timezone.utc)
    if now_value.tzinfo is None or now_value.utcoffset() is None:
        now_value = now_value.replace(tzinfo=timezone.utc)
    now_value = now_value.astimezone(timezone.utc)
    score = max(0.0, min(1.0, float(semantic_score)))
    lifecycle = classify_lifecycle(bucket)
    recorded, source, confidence = resolve_recorded_at(
        bucket,
        naive_source_timezone=naive_source_timezone,
    )
    historical = query_requests_history(query) if history_mode is None else bool(history_mode)
    relative = contains_relative_time(str(bucket.get("content") or ""))
    valid_until, validity_source = resolve_valid_until(
        bucket,
        naive_source_timezone=naive_source_timezone,
    )
    age_days = None
    if recorded is not None:
        age_days = max(0.0, (now_value - recorded).total_seconds() / 86400.0)

    if lifecycle == "stable_fact":
        if age_days is None:
            weight = 0.92
        else:
            weight = max(0.92, math.exp(-math.log(2) * age_days / 3650.0))
        inject = score >= 0.20
        reason = "stable_fact_weak_decay"
    elif lifecycle == "transient_state":
        # A transient's expiry is metadata-driven. Legacy buckets without an
        # explicit valid_until use one conservative 48-hour compatibility
        # window; wording never changes the duration.
        window_days = DEFAULT_TRANSIENT_VALID_HOURS / 24.0
        expired = (
            now_value > valid_until
            if valid_until is not None
            else age_days is not None and age_days > window_days
        )
        if historical:
            effective_age = age_days if age_days is not None else 30.0
            weight = max(0.25, math.exp(-math.log(2) * effective_age / 30.0))
            inject = score >= 0.15
            reason = "historical_query_reenables_transient"
        elif current_state_override:
            # The caller has already established that the current user message
            # reports a new/current state. No recalled transient may compete
            # with it, even when the recalled state has not expired yet.
            weight = 0.0
            inject = False
            reason = "current_message_overrides_transient"
        elif age_days is None:
            weight = 0.0
            inject = False
            reason = "transient_without_absolute_time"
        elif expired:
            weight = 0.0
            inject = False
            reason = "expired_transient_state"
        else:
            weight = max(0.20, math.exp(-math.log(2) * age_days / window_days))
            inject = score * weight >= 0.20
            reason = "recent_transient_state"
    else:
        effective_age = age_days if age_days is not None else 180.0
        weight = max(0.35, math.exp(-math.log(2) * effective_age / 90.0))
        inject = score * weight >= (0.15 if historical else 0.20)
        reason = "event_medium_decay"

    final_score = score * weight
    return RecallDecision(
        semantic_score=round(score, 6),
        time_weight=round(weight, 6),
        time_state_adjustment=round(final_score - score, 6),
        final_score=round(final_score, 6),
        inject=bool(inject),
        reason=reason,
        lifecycle=lifecycle,
        recorded_at=recorded,
        recorded_source=source,
        time_confidence=confidence,
        age_days=round(age_days, 6) if age_days is not None else None,
        valid_until=valid_until,
        validity_source=validity_source,
        historical_query=historical,
        contains_relative_time=relative,
    )


def rerank_recall_candidates(
    candidates: Iterable[dict],
    query: str,
    *,
    now: datetime | None = None,
    naive_source_timezone: str = "UTC",
    history_mode: bool | None = None,
    current_state_override: bool = False,
) -> list[tuple[dict, RecallDecision]]:
    evaluated: list[tuple[dict, RecallDecision]] = []
    for bucket in candidates:
        raw_score = bucket.get("semantic_score")
        if raw_score is None:
            raw_score = bucket.get("score", 0.0)
            if float(raw_score or 0.0) > 1.0:
                raw_score = float(raw_score) / 100.0
        decision = evaluate_recall_candidate(
            bucket,
            query,
            float(raw_score or 0.0),
            now=now,
            naive_source_timezone=naive_source_timezone,
            history_mode=history_mode,
            current_state_override=current_state_override,
        )
        evaluated.append((bucket, decision))
    evaluated.sort(key=lambda item: item[1].final_score, reverse=True)
    return evaluated


def _age_label(age_days: float | None) -> str:
    if age_days is None:
        return "unknown"
    if age_days < 1:
        hours = max(0, round(age_days * 24))
        return f"{hours} hours"
    rounded = round(age_days)
    return f"{rounded} days"


def format_memory_injection(
    bucket: dict,
    summary: str,
    decision: RecallDecision,
    *,
    display_timezone: str = "Asia/Shanghai",
) -> str:
    if decision.lifecycle == "stable_fact":
        heading = "[Stable memory]"
        note = "Long-term fact or preference, unless the current user message corrects or updates it."
    elif decision.historical_query:
        heading = "[Historical memory]"
        note = "Past context only. Never present it as a current state."
    elif decision.lifecycle == "transient_state":
        heading = "[Recent reported state]"
        note = (
            "Candidate context only. Unexpired does not mean still true. "
            "Never claim it is current unless the current user message or current Handoff confirms it."
        )
    else:
        heading = "[Past event]"
        note = "This says what happened, not what is happening now."

    if decision.recorded_at is None:
        recorded_label = "unknown"
    else:
        display_zone = ZoneInfo(display_timezone)
        recorded_label = decision.recorded_at.astimezone(display_zone).strftime(
            "%Y-%m-%d %H:%M"
        ) + f" {display_timezone}"
    if decision.contains_relative_time:
        note += " Relative-time words are anchored to Recorded, never to the current date."

    if decision.valid_until is None:
        validity_label = (
            f"{int(DEFAULT_TRANSIENT_VALID_HOURS)} hours from Recorded (legacy/default)"
            if decision.lifecycle == "transient_state"
            else "not applicable"
        )
    else:
        display_zone = ZoneInfo(display_timezone)
        validity_label = decision.valid_until.astimezone(display_zone).strftime(
            "%Y-%m-%d %H:%M"
        ) + f" {display_timezone} ({decision.validity_source})"

    bucket_id = str(bucket.get("id") or bucket.get("metadata", {}).get("id") or "unknown")
    content_lines = str(summary or "").strip().splitlines() or [""]
    indented_content = "\n".join(
        f"  {line}" if index else line
        for index, line in enumerate(content_lines)
    )
    return "\n".join(
        (
            heading,
            f"[bucket_id:{bucket_id}]",
            f"Recorded: {recorded_label}",
            f"Age: {_age_label(decision.age_days)}",
            f"Type: {decision.lifecycle}",
            f"Validity: {validity_label}",
            f"Source: bucket:{bucket_id} ({decision.recorded_source})",
            (
                f"Semantic score: unavailable; lexical fallback score: "
                f"{decision.semantic_score:.6f}"
                if bucket.get("lexical_only")
                else f"Semantic score: {decision.semantic_score:.6f}"
            ),
            f"Time/state adjustment: {decision.time_state_adjustment:+.6f}",
            f"Content: {indented_content}",
            f"Note: {note}",
            (
                "Authority: current user message > current Handoff > recalled memory. "
                "If the current user reports a new or conflicting state, discard the recalled state "
                "even when it is unexpired."
            ),
        )
    )
