#!/usr/bin/env python3
"""Why did a recall miss? Prints scores and bucket ids only, never content.

usage: probe_recall_miss.py <query> <bucket_id> [<bucket_id> ...]
Run from the release dir with the candidate env (OMBRE_BUCKETS_DIR etc.).
"""

import asyncio
import sys

sys.path.insert(0, ".")
from utils import load_config  # noqa: E402
from bucket_manager import BucketManager  # noqa: E402
from embedding_engine import EmbeddingEngine  # noqa: E402

query, targets = sys.argv[1], sys.argv[2:]


async def main():
    config = load_config()
    embed = EmbeddingEngine(config)
    mgr = BucketManager(config, embedding_engine=embed)
    for bucket_id in targets:
        bucket = await mgr.get(bucket_id)
        if not bucket:
            print(bucket_id, "NOT FOUND")
            continue
        meta = bucket["metadata"]
        print(bucket_id, "name_len", len(str(meta.get("name", ""))), "tags", len(meta.get("tags") or []),
              "domain", meta.get("domain"), "resolved", meta.get("resolved"), "created", meta.get("created"))

    lexical = await mgr.search(query, limit=20)
    ranked = [(b["id"], round(float(b.get("score", 0)), 2)) for b in lexical]
    print("lexical top20:", len(ranked))
    for bucket_id in targets:
        pos = next((i for i, (bid, _) in enumerate(ranked) if bid == bucket_id), None)
        print("  lexical", bucket_id, "rank", pos, "score", ranked[pos][1] if pos is not None else None)
    print("  lexical top3 scores:", [s for _, s in ranked[:3]])

    if embed.enabled:
        vec = await embed.search_similar(query, top_k=20)
        for bucket_id in targets:
            pos = next((i for i, (bid, _) in enumerate(vec) if bid == bucket_id), None)
            print("  semantic", bucket_id, "rank", pos, "sim", round(vec[pos][1], 3) if pos is not None else None)
        print("  semantic top3 sims:", [round(s, 3) for _, s in vec[:3]])


asyncio.run(main())
