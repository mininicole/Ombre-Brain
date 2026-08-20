import pytest

from embedding_engine import EmbeddingEngine


@pytest.mark.asyncio
async def test_search_similar_can_be_scoped_to_feel_bucket_ids(tmp_path):
    engine = EmbeddingEngine(
        {"buckets_dir": str(tmp_path), "embedding": {"enabled": False}}
    )
    engine.enabled = True

    async def fake_query_embedding(_text):
        return [1.0, 0.0]

    engine._generate_embedding = fake_query_embedding
    engine._store_embedding("ordinary-best", [1.0, 0.0])
    engine._store_embedding("feel-related", [0.8, 0.2])

    results = await engine.search_similar(
        "same topic",
        top_k=10,
        allowed_bucket_ids={"feel-related"},
    )
    assert [bucket_id for bucket_id, _score in results] == ["feel-related"]
