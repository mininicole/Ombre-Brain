"""Hybrid local memory search: ChromaDB vectors + BM25.

This is the open-source-safe version of the memory retrieval service. It keeps
all paths configurable and ships without private memories, Chroma state, logs,
or database files. Users generate their own local index with:

    python -m memory_search_service.vectorize
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import chromadb
import jieba
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi


ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path.cwd())).expanduser().resolve()
DB_DIR = Path(os.environ.get("MEMORY_CHROMA_DIR", ROOT / "memory" / "chroma_db")).expanduser()
if not DB_DIR.is_absolute():
    DB_DIR = ROOT / DB_DIR
STATE_FILE = Path(os.environ.get("MEMORY_STATE_FILE", ROOT / "memory" / "chroma_state.json")).expanduser()
if not STATE_FILE.is_absolute():
    STATE_FILE = ROOT / STATE_FILE

COLLECTION = os.environ.get("MEMORY_SEARCH_COLLECTION", "memories")
DEFAULT_TOP_K = int(os.environ.get("MEMORY_SEARCH_TOP_K", "8"))
DEFAULT_BUDGET = int(os.environ.get("MEMORY_SEARCH_BUDGET_CHARS", "2000"))
VEC_POOL = int(os.environ.get("MEMORY_VEC_POOL", "8"))
BM25_POOL = int(os.environ.get("MEMORY_BM25_POOL", "12"))
RRF_K = int(os.environ.get("MEMORY_RRF_K", "60"))
REFRESH_CHECK_INTERVAL = float(os.environ.get("MEMORY_SEARCH_REFRESH_SECONDS", "60"))

_TOKEN_OK = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")

app = FastAPI(title="Local Memory Search")
_INDEX: "Index | None" = None
_INDEX_LOCK = threading.Lock()


class SearchBody(BaseModel):
    query: str = Field(default="", max_length=4000)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=30)
    budget_chars: int = Field(default=DEFAULT_BUDGET, ge=200, le=12000)


def tokenize(text: str) -> list[str]:
    return [token for token in jieba.lcut(text) if _TOKEN_OK.search(token)]


class Index:
    """Vector and BM25 indexes over the same Chroma collection."""

    def __init__(self) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.client = chromadb.PersistentClient(path=str(DB_DIR))
        self.coll = self.client.get_or_create_collection(COLLECTION)
        self.ids: list[str] = []
        self.docs: list[str] = []
        self.metas: list[dict[str, Any]] = []
        self.id_to_pos: dict[str, int] = {}
        self.bm25: BM25Okapi | None = None
        self.signature: tuple[int, int] = (-1, -1)
        self.loaded = False
        self.last_refresh_check = 0.0
        self.refresh(force=True)

    def _signature(self) -> tuple[int, int]:
        state_mtime = 0
        try:
            state_mtime = int(STATE_FILE.stat().st_mtime)
        except OSError:
            pass
        return (self.coll.count(), state_mtime)

    def refresh(self, force: bool = False) -> None:
        with self.lock:
            now = time.monotonic()
            if (
                not force
                and self.loaded
                and now - self.last_refresh_check < REFRESH_CHECK_INTERVAL
            ):
                return
            self.last_refresh_check = now
            sig = self._signature()
            if sig == self.signature and self.loaded:
                return
            data = self.coll.get(include=["documents", "metadatas"])
            ids = data.get("ids") or []
            docs = data.get("documents") or []
            metas = data.get("metadatas") or []
            tokens = [tokenize(doc) for doc in docs]
            self.ids = ids
            self.docs = docs
            self.metas = metas
            self.id_to_pos = {doc_id: i for i, doc_id in enumerate(ids)}
            self.bm25 = BM25Okapi(tokens) if tokens else None
            self.signature = sig
            self.loaded = True
            print(f"[memory] built BM25 over {len(ids)} chunks")

    def vec_search(self, query: str, k: int) -> list[tuple[str, float]]:
        if k <= 0 or not query.strip() or self.coll.count() <= 0:
            return []
        n_results = min(k, self.coll.count())
        try:
            res = self.coll.query(query_texts=[query], n_results=n_results)
        except Exception as exc:
            print(f"[memory] vector search failed: {exc}")
            return []
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        return [(doc_id, max(0.0, 1.0 - float(dist))) for doc_id, dist in zip(ids, dists)]

    def bm25_search(self, query: str, k: int) -> list[tuple[str, float]]:
        if self.bm25 is None or not query.strip():
            return []
        toks = tokenize(query)
        if not toks:
            return []
        scores = self.bm25.get_scores(toks)
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:k]
        return [(self.ids[i], float(score)) for i, score in ranked if score > 0]

    def lookup(self, doc_id: str) -> tuple[str, dict[str, Any]] | None:
        idx = self.id_to_pos.get(doc_id)
        if idx is None:
            return None
        return self.docs[idx], (self.metas[idx] or {})


def get_index() -> Index:
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = Index()
        return _INDEX


def rrf_fuse(*rankings: list[tuple[str, float]], k: int = RRF_K) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranked in rankings:
        for rank, (doc_id, _score) in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def truncate_to_budget(items: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    used = 0
    for item in items:
        size = len(item["text"]) + len(item.get("heading") or "") + 32
        if used + size > budget and out:
            break
        out.append(item)
        used += size
    return out


def run_search(query: str, top_k: int, budget_chars: int) -> dict[str, Any]:
    started = time.perf_counter()
    index = get_index()
    index.refresh()
    vec = index.vec_search(query, VEC_POOL)
    bm25 = index.bm25_search(query, BM25_POOL)
    fused = rrf_fuse(vec, bm25)[:top_k]

    results = []
    for doc_id, score in fused:
        got = index.lookup(doc_id)
        if not got:
            continue
        text, meta = got
        results.append({
            "id": doc_id,
            "text": text,
            "source": meta.get("filename") or meta.get("source"),
            "heading": meta.get("heading"),
            "score": round(score, 4),
        })
    results = truncate_to_budget(results, budget_chars)
    joined = "\n\n".join(
        f"[{item['source']}{' · ' + item['heading'] if item.get('heading') else ''}]\n{item['text']}"
        for item in results
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "joined": joined,
        "results": results,
        "stats": {
            "chunks": len(index.ids),
            "vec_hits": len(vec),
            "bm25_hits": len(bm25),
            "ms": elapsed_ms,
        },
    }


@app.get("/health")
def health() -> dict[str, Any]:
    index = get_index()
    return {
        "ok": True,
        "chunks": len(index.ids),
        "db_dir": str(DB_DIR),
        "collection": COLLECTION,
        "vec_pool": VEC_POOL,
        "bm25_pool": BM25_POOL,
    }


@app.post("/search")
def search(body: SearchBody) -> dict[str, Any]:
    query = body.query.strip()
    if len(query) < 2:
        return {"joined": "", "results": [], "stats": {"chunks": 0, "vec_hits": 0, "bm25_hits": 0, "ms": 0}}
    return run_search(query, body.top_k, body.budget_chars)


def main() -> None:
    host = os.environ.get("MEMORY_SEARCH_HOST", "127.0.0.1")
    port = int(os.environ.get("MEMORY_SEARCH_PORT", "3900"))
    uvicorn.run("memory_search_service.server:app", host=host, port=port)
