from datetime import datetime, timedelta, timezone

from recall_time import evaluate_recall_candidate, format_memory_injection

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=30)).isoformat()


def legacy_bucket():
    return {"id": "legacy-1", "metadata": {"created": OLD}, "content": "今天她在厨房做饭"}


def typed_bucket():
    return {
        "id": "typed-1",
        "metadata": {"created": OLD, "memory_lifecycle": "transient_state"},
        "content": "她现在很累",
    }


def test_legacy_gate_default_on_hides_old_relative_time_bucket(monkeypatch):
    monkeypatch.delenv("OMBRE_TIME_GATE_LEGACY", raising=False)
    decision = evaluate_recall_candidate(legacy_bucket(), "", 1.0, now=NOW)
    assert decision.lifecycle == "transient_state"
    assert not decision.inject


def test_legacy_gate_off_keeps_old_bucket_and_compact_format(monkeypatch):
    monkeypatch.setenv("OMBRE_TIME_GATE_LEGACY", "off")
    bucket = legacy_bucket()
    decision = evaluate_recall_candidate(bucket, "", 0.5, now=NOW)
    assert decision.inject
    assert decision.reason == "legacy_ungated"
    assert format_memory_injection(bucket, "摘要", decision) == "[bucket_id:legacy-1] 摘要"


def test_legacy_gate_off_still_gates_explicit_lifecycle(monkeypatch):
    monkeypatch.setenv("OMBRE_TIME_GATE_LEGACY", "off")
    decision = evaluate_recall_candidate(typed_bucket(), "", 1.0, now=NOW)
    assert decision.reason == "expired_transient_state"
    assert not decision.inject
