from datetime import datetime, timezone

import pytest

from recall_time import evaluate_recall_candidate, format_memory_injection


NOW = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)


def bucket(bucket_id, content, created, *, bucket_type="dynamic", tags=None, domain=None):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "id": bucket_id,
            "created": created,
            "type": bucket_type,
            "tags": tags or [],
            "domain": domain or ["test"],
        },
    }


def test_old_coffee_is_not_injected_as_current_state():
    memory = bucket(
        "coffee-old",
        "今天买了特调咖啡，正在慢慢喝。",
        "2026-09-05T08:20:00+00:00",
    )
    decision = evaluate_recall_candidate(memory, "我刚忙完", 0.86, now=NOW)

    assert decision.lifecycle == "transient_state"
    assert decision.reason == "expired_transient_state"
    assert decision.time_weight == 0.0
    assert decision.time_state_adjustment == -0.86
    assert decision.inject is False


def test_current_message_overrides_old_insomnia_state():
    memory = bucket(
        "insomnia-old",
        "昨晚失眠，现在还是很累。",
        "2026-09-04T16:00:00+00:00",
    )
    decision = evaluate_recall_candidate(
        memory,
        "昨晚睡得很好，今天很精神。",
        0.93,
        now=NOW,
    )

    assert decision.inject is False
    assert decision.reason == "expired_transient_state"


def test_months_old_stable_preference_survives_weak_decay():
    memory = bucket(
        "stable-tea",
        "用户长期喜欢无糖乌龙茶。",
        "2026-03-09T00:00:00+00:00",
        bucket_type="permanent",
        tags=["偏好"],
        domain=["偏好"],
    )
    decision = evaluate_recall_candidate(memory, "我平时喜欢喝什么？", 0.78, now=NOW)

    assert decision.lifecycle == "stable_fact"
    assert decision.time_weight >= 0.92
    assert decision.inject is True
    rendered = format_memory_injection(memory, memory["content"], decision)
    assert rendered.startswith("[Stable memory]")
    assert "current user message > current Handoff" in rendered


def test_explicit_historical_query_reenables_old_coffee():
    memory = bucket(
        "coffee-history",
        "今天买了榛果特调咖啡。",
        "2026-09-05T08:20:00+00:00",
    )
    decision = evaluate_recall_candidate(
        memory,
        "前几天我买了什么咖啡？",
        0.86,
        now=NOW,
    )

    assert decision.historical_query is True
    assert decision.reason == "historical_query_reenables_transient"
    assert decision.inject is True
    rendered = format_memory_injection(memory, memory["content"], decision)
    assert rendered.startswith("[Historical memory]")
    assert "Never present it as a current state" in rendered


def test_relative_time_is_anchored_to_recorded_date():
    memory = bucket(
        "relative-old",
        "昨天去了山里，今晚住在木屋。",
        "2026-09-01T02:00:00+00:00",
    )
    decision = evaluate_recall_candidate(
        memory,
        "上次去山里是什么时候？",
        0.82,
        now=NOW,
    )
    rendered = format_memory_injection(memory, memory["content"], decision)

    assert decision.inject is True
    assert decision.contains_relative_time is True
    assert "Recorded: 2026-09-01 10:00 Asia/Shanghai" in rendered
    assert "Relative-time words are anchored to Recorded" in rendered


def test_transient_without_absolute_time_is_suppressed():
    memory = bucket("missing-time", "现在头疼。", None)
    decision = evaluate_recall_candidate(memory, "我忙完了", 0.9, now=NOW)

    assert decision.inject is False
    assert decision.reason == "transient_without_absolute_time"


def test_event_without_absolute_time_stays_compatible_but_is_marked_unknown():
    memory = bucket("missing-event-time", "Wonderland summary", None)
    memory["lexical_only"] = True
    decision = evaluate_recall_candidate(memory, "Wonderland", 1.0, now=NOW)
    rendered = format_memory_injection(memory, memory["content"], decision)

    assert decision.lifecycle == "event"
    assert decision.inject is True
    assert decision.recorded_at is None
    assert "Recorded: unknown" in rendered
    assert "Semantic score: unavailable; lexical fallback score" in rendered
    assert "[bucket_id:missing-event-time]" in rendered


@pytest.mark.parametrize("field", ["source_timestamp", "source_date", "created_at", "created"])
def test_absolute_time_field_priority_is_supported(field):
    memory = bucket("time-field", "发生过一件事。", None)
    memory["metadata"][field] = "2026-09-08T00:00:00+00:00"
    decision = evaluate_recall_candidate(memory, "那件事", 0.8, now=NOW)

    assert decision.recorded_source == field
    assert decision.recorded_at is not None
