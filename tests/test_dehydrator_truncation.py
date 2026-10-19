# ============================================================
# Dehydrator: thinking switch + cut-off answers (2026-10-19)
# deepseek-v4-flash 先"思考"，思考 token 算进 max_tokens，正文写一半就断：
# 压缩版缓存成半截 JSON、合并把桶内容截短、打标全失败。这里用假客户端验证修法。
# ============================================================

import asyncio
import json
from types import SimpleNamespace

import pytest

from dehydrator import Dehydrator


class FakeClient:
    """Scripted chat.completions.create: each call pops (text, finish_reason)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        text, finish = self.replies.pop(0)
        choice = SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)
        return SimpleNamespace(choices=[choice])


def make(tmp_path, replies, base_url="https://api.deepseek.com/v1", thinking=None):
    dehy = {"api_key": "test-key", "base_url": base_url, "model": "deepseek-v4-flash", "max_tokens": 1024}
    if thinking:
        dehy["thinking"] = thinking
    d = Dehydrator({"buckets_dir": str(tmp_path), "dehydration": dehy})
    d.client = FakeClient(replies)
    return d


LONG = "她今天去看了贝壳展，中午吃了菌汤锅，下午去鸟咖，晚上回家说要再看一遍银翼杀手。" * 6
GOOD_JSON = json.dumps({"core_facts": ["晚上回家要看银翼杀手"], "emotion_state": "放松",
                        "todos": [], "keywords": ["银翼杀手"], "summary": "贝壳展、鸟咖、银翼杀手"}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("OMBRE_DEHYDRATION_THINKING", raising=False)
    monkeypatch.delenv("OMBRE_READ_ONLY", raising=False)


def test_deepseek_calls_turn_thinking_off(tmp_path):
    d = make(tmp_path, [(GOOD_JSON, "stop")])
    asyncio.run(d.dehydrate(LONG, {"name": "t"}))
    assert d.client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_other_providers_are_not_sent_the_thinking_switch(tmp_path):
    d = make(tmp_path, [(GOOD_JSON, "stop")], base_url="https://generativelanguage.googleapis.com/v1beta/openai")
    asyncio.run(d.dehydrate(LONG, {"name": "t"}))
    assert "extra_body" not in d.client.calls[0]


def test_thinking_switch_can_be_forced_either_way(tmp_path, monkeypatch):
    d = make(tmp_path, [(GOOD_JSON, "stop")], thinking="on")
    asyncio.run(d.dehydrate(LONG, {"name": "t"}))
    assert "extra_body" not in d.client.calls[0]
    monkeypatch.setenv("OMBRE_DEHYDRATION_THINKING", "off")
    d2 = make(tmp_path / "b", [(GOOD_JSON, "stop")], base_url="http://localhost:11434/v1")
    asyncio.run(d2.dehydrate(LONG + "x", {"name": "t"}))
    assert d2.client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_cut_off_dehydration_is_retried_with_more_room(tmp_path):
    d = make(tmp_path, [('{"core_facts": ["晚上回家', "length"), (GOOD_JSON, "stop")])
    out = asyncio.run(d.dehydrate(LONG, {"name": "t"}))
    assert [c["max_tokens"] for c in d.client.calls] == [1024, 4096]
    assert "银翼杀手" in out
    assert d._get_cached_summary(LONG) == GOOD_JSON


def test_dehydration_still_cut_off_falls_back_to_the_original_text(tmp_path):
    d = make(tmp_path, [('{"core_facts": ["晚', "length"), ('{"core_facts": ["晚上', "length")])
    out = asyncio.run(d.dehydrate(LONG, {"name": "t"}))
    cached = d._get_cached_summary(LONG)
    assert cached == LONG[:1500]           # the whole original, never the half JSON
    assert "银翼杀手" in out


def test_cut_off_merge_is_never_written(tmp_path):
    d = make(tmp_path, [("合并后的前半段", "length"), ("合并后的前半段还是", "length")])
    with pytest.raises(RuntimeError):
        asyncio.run(d.merge("旧记忆" * 50, "新内容" * 50))
    assert d.client.calls[0]["max_tokens"] == 4096


def test_merge_that_finishes_is_returned(tmp_path):
    d = make(tmp_path, [("完整的合并结果。", "stop")])
    assert asyncio.run(d.merge("旧记忆", "新内容")) == "完整的合并结果。"


def test_tagging_gets_room_for_its_json_and_falls_back_when_cut(tmp_path):
    tags = json.dumps({"domain": ["影视"], "valence": 0.7, "arousal": 0.4, "tags": ["银翼杀手"],
                       "suggested_name": "回家看银翼杀手", "memory_lifecycle": "event"}, ensure_ascii=False)
    d = make(tmp_path, [(tags, "stop")])
    result = asyncio.run(d.analyze(LONG))
    assert result["suggested_name"] == "回家看银翼杀手"
    assert d.client.calls[0]["max_tokens"] == 1024
    d2 = make(tmp_path / "b", [('{"domain": ["影', "length"), ('{"domain": ["影视"', "length")])
    assert asyncio.run(d2.analyze(LONG))["suggested_name"] == ""
