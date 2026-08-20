import pytest

from bucket_manager import BucketManager
from quote_store import (
    MAX_QUOTE_CHARS,
    MAX_QUOTES,
    merge_quotes,
    normalize_quotes,
    quotes_from_metadata,
    render_quotes,
)


def test_normalize_quotes_accepts_strings_and_objects_verbatim():
    assert normalize_quotes(["我不会走的"]) == [{"text": "我不会走的"}]
    assert normalize_quotes(
        [{"text": "原样留下", "speaker": "她", "at": "2026-08-20"}]
    ) == [{"text": "原样留下", "speaker": "她", "at": "2026-08-20"}]


def test_quote_limits_reject_instead_of_truncating():
    with pytest.raises(ValueError):
        normalize_quotes([f"第{i}句" for i in range(MAX_QUOTES + 1)])
    with pytest.raises(ValueError):
        normalize_quotes(["字" * (MAX_QUOTE_CHARS + 1)])


def test_metadata_reader_salvages_valid_entries():
    metadata = {"quotes": ["好的一句", "字" * 999, 123, {"text": "另一句"}]}
    assert quotes_from_metadata(metadata) == [
        {"text": "好的一句"},
        {"text": "另一句"},
    ]


def test_merge_preserves_earliest_quotes_under_cap():
    merged, dropped = merge_quotes(
        ["第一句", "第二句"],
        ["第二句", "第三句", "第四句"],
    )
    assert merged == [
        {"text": "第一句"},
        {"text": "第二句"},
        {"text": "第三句"},
    ]
    assert dropped == 1


def test_render_quotes_keeps_original_text():
    original = "你说：不要把我改写。"
    rendered = render_quotes(
        [{"text": original, "speaker": "深深", "at": "2026-08-20"}]
    )
    assert original in rendered
    assert "深深 / 2026-08-20" in rendered


@pytest.mark.asyncio
async def test_bucket_manager_stores_and_appends_quotes(tmp_path):
    manager = BucketManager({"buckets_dir": str(tmp_path)})
    bucket_id = await manager.create(
        content="第一次发生的事",
        domain=["测试"],
        quotes=["第一句", {"text": "第二句", "speaker": "她"}],
    )
    stored = await manager.get(bucket_id)
    assert stored["metadata"]["quotes"] == [
        {"text": "第一句"},
        {"text": "第二句", "speaker": "她"},
    ]

    assert await manager.update(bucket_id, quotes_append=["第三句", "第四句"])
    updated = await manager.get(bucket_id)
    assert updated["metadata"]["quotes"] == [
        {"text": "第一句"},
        {"text": "第二句", "speaker": "她"},
        {"text": "第三句"},
    ]
