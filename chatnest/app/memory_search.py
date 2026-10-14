"""Client for the optional local memory search service (memory_search_service @ localhost:3900).

Failures are swallowed silently — memory recall is best-effort context;
it should never block or break the chat flow.
"""
import json
import os
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# 2026-10-14：本地回忆搜索服务（memory_search_service）从没部署过，Evan 走 ombre 记忆；默认关，设了 MEMORY_SEARCH_URL 才查。
SEARCH_URL = os.environ.get("MEMORY_SEARCH_URL", "")
TIMEOUT = 1.5  # seconds — bail out rather than block the chat
MIN_QUERY_CHARS = 4


def recall(query: str, budget_chars: int = 2000, top_k: int = 8) -> str:
    """Query the memory search service. Returns joined text or '' on failure."""
    q = (query or "").strip()
    if not SEARCH_URL or len(q) < MIN_QUERY_CHARS:
        return ""
    try:
        payload = json.dumps(
            {"query": q, "top_k": top_k, "budget_chars": budget_chars},
        ).encode("utf-8")
        req = urllib.request.Request(
            SEARCH_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("joined", "")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        logger.warning("memory recall failed", exc_info=True)
        return ""
