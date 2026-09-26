#!/usr/bin/env python3
"""Aggregate-only audit: how would the recall time gate treat Evan's buckets?

Runs recall_time.evaluate_recall_candidate over every bucket in a restored
data root, the way breath() does for no-query surfacing (score 1.0) and for
the recent top-up used by evan-bot private chats (score 0.5). Prints counts
only, never bucket content, names or IDs.

usage: audit_evan_time_gate.py <data_root> <repo_root>
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import frontmatter

data_root = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(Path(sys.argv[2]).resolve(strict=True)))
from recall_time import classify_lifecycle, contains_relative_time, evaluate_recall_candidate  # noqa: E402

lifecycle = Counter()
hidden = Counter()
reasons = Counter()
relative = 0
total = 0
by_folder_hidden = Counter()

for folder in ("permanent", "dynamic", "archive", "feel"):
    for path in sorted((data_root / folder).rglob("*.md")):
        post = frontmatter.load(path)
        if not str(post.content or "").strip():
            continue
        bucket = {"id": str(post.get("id") or ""), "metadata": dict(post.metadata), "content": post.content}
        total += 1
        kind = classify_lifecycle(bucket)
        lifecycle[kind] += 1
        relative += contains_relative_time(post.content)
        for label, score in (("surface_1.0", 1.0), ("recent_topup_0.5", 0.5)):
            decision = evaluate_recall_candidate(bucket, "", score)
            if not decision.inject:
                hidden[label] += 1
                reasons[(label, kind, decision.reason)] += 1
                if label == "recent_topup_0.5":
                    by_folder_hidden[folder] += 1

print("buckets:", total)
print("inferred lifecycle:", dict(lifecycle))
print("contain relative-time words:", relative)
print("hidden by gate:", dict(hidden))
print("hidden (recent top-up) by folder:", dict(by_folder_hidden))
for key, count in sorted(reasons.items(), key=lambda item: -item[1]):
    print("  ", key, count)
