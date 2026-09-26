"""Content-independent acceptance tests for Ombre's minimal time sense."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from dehydrator import Dehydrator
from recall_time import evaluate_recall_candidate, format_memory_injection


NOW = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)


def _memory(
    lifecycle: str,
    *,
    age: timedelta,
    content: str = "opaque-payload-with-no-temporal-clues",
    valid_until: datetime | None = None,
) -> dict:
    recorded = NOW - age
    metadata = {
        "id": "opaque-id",
        "created": recorded.isoformat(),
        "source_timestamp": recorded.isoformat(),
        "memory_lifecycle": lifecycle,
        "type": "dynamic",
        "domain": ["test"],
        "tags": [],
    }
    if valid_until is not None:
        metadata["valid_until"] = valid_until.isoformat()
    return {"id": "opaque-id", "content": content, "metadata": metadata}


@pytest.mark.parametrize(
    "content",
    [
        "opaque-A",
        "完全不含时间词的任意状态正文",
        "文本看起来像长期事实但显式生命周期仍为临时状态",
    ],
)
def test_expired_transient_is_never_current_regardless_of_content(content):
    memory = _memory(
        "transient_state",
        age=timedelta(hours=1),
        content=content,
        valid_until=NOW - timedelta(seconds=1),
    )

    decision = evaluate_recall_candidate(memory, "opaque-current-message", 0.99, now=NOW)

    assert decision.reason == "expired_transient_state"
    assert decision.time_weight == 0.0
    assert decision.inject is False


def test_unexpired_transient_is_only_a_candidate_not_current_truth():
    memory = _memory(
        "transient_state",
        age=timedelta(hours=1),
        valid_until=NOW + timedelta(hours=47),
    )

    decision = evaluate_recall_candidate(memory, "opaque-current-message", 0.9, now=NOW)
    rendered = format_memory_injection(memory, memory["content"], decision)

    assert decision.inject is True
    assert rendered.startswith("[Recent reported state]")
    assert "Unexpired does not mean still true" in rendered
    assert "If the current user reports a new or conflicting state, discard the recalled state" in rendered


def test_explicit_current_state_override_filters_even_unexpired_transient():
    memory = _memory(
        "transient_state",
        age=timedelta(hours=1),
        valid_until=NOW + timedelta(hours=47),
    )

    decision = evaluate_recall_candidate(
        memory,
        "opaque-current-message",
        0.99,
        now=NOW,
        current_state_override=True,
    )

    assert decision.reason == "current_message_overrides_transient"
    assert decision.time_weight == 0.0
    assert decision.inject is False


def test_very_old_stable_fact_remains_recallable_without_content_hints():
    memory = _memory("stable_fact", age=timedelta(days=3650))

    decision = evaluate_recall_candidate(memory, "opaque-query", 0.8, now=NOW)
    rendered = format_memory_injection(memory, memory["content"], decision)

    assert decision.inject is True
    assert decision.lifecycle == "stable_fact"
    assert rendered.startswith("[Stable memory]")


def test_event_is_presented_as_past_not_as_current_state():
    memory = _memory("event", age=timedelta(days=5))

    decision = evaluate_recall_candidate(memory, "opaque-query", 0.9, now=NOW)
    rendered = format_memory_injection(memory, memory["content"], decision)

    assert decision.inject is True
    assert rendered.startswith("[Past event]")
    assert "what happened, not what is happening now" in rendered


@pytest.mark.parametrize("lifecycle", ["event", "transient_state"])
def test_explicit_history_mode_recalls_old_memory_as_history(lifecycle):
    memory = _memory(lifecycle, age=timedelta(days=30))

    decision = evaluate_recall_candidate(
        memory,
        "opaque-query-with-no-history-keywords",
        0.9,
        now=NOW,
        history_mode=True,
    )
    rendered = format_memory_injection(memory, memory["content"], decision)

    assert decision.inject is True
    assert decision.historical_query is True
    assert rendered.startswith("[Historical memory]")
    assert "Never present it as a current state" in rendered


def test_explicit_lifecycle_wins_over_misleading_wording():
    stable = _memory(
        "stable_fact",
        age=timedelta(days=500),
        content="现在、刚刚、今天——这些词不能覆盖显式 lifecycle",
    )
    transient = _memory(
        "transient_state",
        age=timedelta(days=3),
        content="正文完全没有任何相对时间提示",
    )

    stable_decision = evaluate_recall_candidate(stable, "opaque", 0.8, now=NOW)
    transient_decision = evaluate_recall_candidate(transient, "opaque", 0.99, now=NOW)

    assert stable_decision.lifecycle == "stable_fact"
    assert stable_decision.inject is True
    assert transient_decision.lifecycle == "transient_state"
    assert transient_decision.inject is False


def test_legacy_relative_state_wins_over_preference_like_tag():
    memory = _memory(
        "transient_state",
        age=timedelta(days=3),
        content="今天对某个临时对象表示喜欢。",
    )
    memory["metadata"].pop("memory_lifecycle")
    memory["metadata"]["tags"] = ["偏好"]

    decision = evaluate_recall_candidate(memory, "opaque", 0.99, now=NOW)

    assert decision.lifecycle == "transient_state"
    assert decision.inject is False


def test_default_transient_window_has_exact_48_hour_boundary():
    at_boundary = _memory("transient_state", age=timedelta(hours=48))
    past_boundary = _memory(
        "transient_state",
        age=timedelta(hours=48, seconds=1),
    )

    assert evaluate_recall_candidate(at_boundary, "opaque", 0.9, now=NOW).inject is True
    assert evaluate_recall_candidate(past_boundary, "opaque", 0.9, now=NOW).inject is False


def test_relative_time_is_anchored_to_recorded_time():
    memory = _memory(
        "event",
        age=timedelta(days=8),
        content="昨天完成了某件事。",
    )
    decision = evaluate_recall_candidate(
        memory,
        "opaque",
        0.9,
        now=NOW,
        history_mode=True,
    )
    rendered = format_memory_injection(memory, memory["content"], decision)

    assert "Recorded: 2026-09-01 08:00 Asia/Shanghai" in rendered
    assert "Relative-time words are anchored to Recorded" in rendered


def test_analyze_parser_emits_lifecycle_and_fails_closed():
    dehydrator = Dehydrator.__new__(Dehydrator)
    valid = dehydrator._parse_analysis(json.dumps({
        "domain": ["test"],
        "valence": 0.5,
        "arousal": 0.5,
        "tags": [],
        "suggested_name": "opaque",
        "memory_lifecycle": "event",
    }))
    missing = dehydrator._parse_analysis(json.dumps({
        "domain": ["test"],
        "valence": 0.5,
        "arousal": 0.5,
        "tags": [],
        "suggested_name": "opaque",
    }))

    assert valid["memory_lifecycle"] == "event"
    assert missing["memory_lifecycle"] == "transient_state"


@pytest.mark.asyncio
async def test_bucket_write_contract_persists_absolute_time_and_expiry(bucket_mgr):
    source = "2026-09-09T00:00:00+00:00"
    bucket_id = await bucket_mgr.create(
        content="opaque",
        memory_lifecycle="transient_state",
        source_timestamp=source,
    )
    saved = await bucket_mgr.get(bucket_id)

    assert saved["metadata"]["memory_lifecycle"] == "transient_state"
    assert saved["metadata"]["source_timestamp"] == source
    assert saved["metadata"]["valid_until"] == "2026-09-11T00:00:00+00:00"


@pytest.mark.asyncio
async def test_changing_transient_to_event_removes_expiry(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="opaque",
        memory_lifecycle="transient_state",
        source_timestamp="2026-09-09T00:00:00+00:00",
    )

    assert await bucket_mgr.update(bucket_id, memory_lifecycle="event") is True
    saved = await bucket_mgr.get(bucket_id)

    assert saved["metadata"]["memory_lifecycle"] == "event"
    assert "valid_until" not in saved["metadata"]


@pytest.mark.asyncio
async def test_merged_transient_refreshes_source_time_and_expiry(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="opaque-old",
        memory_lifecycle="transient_state",
        source_timestamp="2026-09-01T00:00:00+00:00",
    )

    assert await bucket_mgr.update(
        bucket_id,
        content="opaque-new",
        memory_lifecycle="transient_state",
        source_timestamp="2026-09-09T00:00:00+00:00",
        valid_until="",
    ) is True
    saved = await bucket_mgr.get(bucket_id)

    assert saved["metadata"]["source_timestamp"] == "2026-09-09T00:00:00+00:00"
    assert saved["metadata"]["valid_until"] == "2026-09-11T00:00:00+00:00"


def test_digest_parser_emits_lifecycle_and_fails_closed():
    dehydrator = Dehydrator.__new__(Dehydrator)
    parsed = dehydrator._parse_digest(json.dumps([
        {
            "name": "one",
            "content": "opaque-one",
            "domain": ["test"],
            "valence": 0.5,
            "arousal": 0.5,
            "tags": [],
            "importance": 5,
            "memory_lifecycle": "stable_fact",
        },
        {
            "name": "two",
            "content": "opaque-two",
            "domain": ["test"],
            "valence": 0.5,
            "arousal": 0.5,
            "tags": [],
            "importance": 5,
        },
    ]))

    assert parsed[0]["memory_lifecycle"] == "stable_fact"
    assert parsed[1]["memory_lifecycle"] == "transient_state"
