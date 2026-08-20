"""Validation and rendering for opt-in verbatim memory quotes.

Quotes are deliberately stored in bucket frontmatter, excluded from ordinary
surfacing and embeddings, and returned only when a caller explicitly asks for
them during a search.
"""

from __future__ import annotations

from typing import Any


MAX_QUOTES = 3
MAX_QUOTE_CHARS = 100
MAX_SPEAKER_CHARS = 40
MAX_AT_CHARS = 32

_UNSAFE_TRANSLATION = {
    codepoint: None
    for codepoint in (
        list(range(0x00, 0x09))
        + [0x0B, 0x0C]
        + list(range(0x0E, 0x20))
        + [0x7F]
        + list(range(0x202A, 0x202F))
        + list(range(0x2066, 0x206A))
    )
}


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").translate(_UNSAFE_TRANSLATION).strip()[:limit]


def normalize_quotes(value: Any) -> list[dict[str, str]]:
    """Validate and normalize strings or ``{text, speaker, at}`` objects."""

    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise ValueError("quotes 必须是列表")
    if len(value) > MAX_QUOTES:
        raise ValueError(f"引语最多 {MAX_QUOTES} 条（给了 {len(value)} 条）")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise ValueError("quotes 每项必须是字符串或对象")

        raw_text = str(item.get("text") or "").strip()
        if not raw_text:
            raise ValueError("quotes 每项必须有非空的 text")
        if len(raw_text) > MAX_QUOTE_CHARS:
            raise ValueError(
                f"单条引语最多 {MAX_QUOTE_CHARS} 字（这条 {len(raw_text)} 字）；"
                "不会截断原话"
            )

        text = _clean_text(raw_text, MAX_QUOTE_CHARS)
        if not text:
            raise ValueError("quotes 的 text 清洗后为空")
        speaker = _clean_text(item.get("speaker"), MAX_SPEAKER_CHARS)
        at = _clean_text(item.get("at"), MAX_AT_CHARS)
        key = (text, speaker)
        if key in seen:
            continue
        seen.add(key)

        entry = {"text": text}
        if speaker:
            entry["speaker"] = speaker
        if at:
            entry["at"] = at
        normalized.append(entry)
    return normalized


def quotes_from_metadata(metadata: dict | None) -> list[dict[str, str]]:
    """Read quotes defensively so malformed hand-edited data cannot break recall."""

    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("quotes")
    if not isinstance(raw, list):
        return []

    salvaged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        try:
            candidates = normalize_quotes([item])
        except ValueError:
            continue
        for quote in candidates:
            key = (quote["text"], quote.get("speaker", ""))
            if key in seen:
                continue
            seen.add(key)
            salvaged.append(quote)
            if len(salvaged) >= MAX_QUOTES:
                return salvaged
    return salvaged


def merge_quotes(existing: Any, incoming: Any) -> tuple[list[dict[str, str]], int]:
    """Append unique quotes, preserving the earliest entries under the hard cap."""

    groups = (
        quotes_from_metadata({"quotes": existing}),
        normalize_quotes(incoming),
    )
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for quote in group:
            key = (quote["text"], quote.get("speaker", ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(quote)
    dropped = max(0, len(merged) - MAX_QUOTES)
    return merged[:MAX_QUOTES], dropped


def render_quotes(quotes: list[dict[str, str]]) -> str:
    """Render stored quotes verbatim for the explicit search-only exit."""

    lines = []
    for quote in quotes:
        line = f'🗣️ 「{quote["text"]}」'
        suffix = " / ".join(
            part for part in (quote.get("speaker", ""), quote.get("at", "")) if part
        )
        if suffix:
            line += f"  —— {suffix}"
        lines.append(line)
    return "\n".join(lines)
