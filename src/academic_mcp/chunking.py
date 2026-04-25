"""Chunking for the semantic index.

Turns Zotero items into one or more chunks suitable for embedding.

Strategy (per the user's decision):
  * If the item has a PDF attachment with a .zotero-ft-cache file,
    the fulltext is split into overlapping ~400-token windows.
    Character offsets are preserved so chunk-level hits can be fed
    to fetch_fulltext(mode="range", ...).
  * If the item has no ft-cache (books, pre-PDF imports, metadata-only
    items), a single chunk of title + abstract is emitted.

Tokenization: we use a char-based approximation (~4 chars/token for
English). This is imprecise but (a) the target chunk size is a
soft preference, not a hard limit — Qwen3 handles overflow with its
32k context; (b) adding tiktoken would inflate the dependency surface
for something that doesn't benefit from millisecond accuracy.

TODO: future enhancement — prefer .article.json sections when present,
for richer structural chunking rather than flat sliding windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import zotero_sqlite

# ~400 tokens ≈ 1600 chars. 50-token overlap ≈ 200 chars.
_CHARS_PER_TOKEN = 4
_TARGET_CHUNK_TOKENS = 400
_OVERLAP_TOKENS = 50
_CHUNK_CHARS = _TARGET_CHUNK_TOKENS * _CHARS_PER_TOKEN   # 1600
_OVERLAP_CHARS = _OVERLAP_TOKENS * _CHARS_PER_TOKEN       # 200
_STRIDE_CHARS = _CHUNK_CHARS - _OVERLAP_CHARS              # 1400

# Upper bound on how much ft-cache to consider per item.
# With Zotero's PDF indexing raised to 2M chars / 500 pages, this
# lets the chunker cover roughly the first ~125 pages of a long
# document (200k chars ÷ ~1600 chars/page) — enough to capture
# intro + body for almost every monograph in a humanities/law library,
# without letting one 2000-page reference volume dominate the index.
#
# Raise to None (no cap) if semantic recall on long books proves
# insufficient.  Storage impact at None is manageable: ~500 books
# at 1250 chunks each = ~625k extra chunks, roughly 2.5 GB in
# Chroma at 1024 dims.
_MAX_FT_CHARS = 200_000


@dataclass
class Chunk:
    """One embeddable unit.

    ``char_start`` / ``char_end`` are offsets into the source text —
    for ft-cache chunks, offsets into the ft-cache file; for
    title+abstract chunks, offsets into the concatenated string
    (useful mainly for debugging).
    """

    text: str
    char_start: int
    char_end: int
    source: str  # "ft_cache" | "abstract"


def _read_ft_cache(attachment_key: str) -> str:
    """Return up to _MAX_FT_CHARS from an attachment's .zotero-ft-cache, or ''."""
    if not attachment_key:
        return ""
    base = Path(zotero_sqlite.sqlite_config.storage_path or "")
    if not base or not str(base):
        return ""
    path = base / attachment_key / ".zotero-ft-cache"
    if not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_FT_CHARS)
    except OSError:
        return ""


def _sliding_chunks(text: str) -> list[Chunk]:
    """Yield overlapping windows over *text*."""
    if not text:
        return []
    chunks: list[Chunk] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + _CHUNK_CHARS, n)
        chunks.append(
            Chunk(
                text=text[i:end],
                char_start=i,
                char_end=end,
                source="ft_cache",
            )
        )
        if end >= n:
            break
        i += _STRIDE_CHARS
    return chunks


def _build_context_header(item: dict) -> str:
    """Compact paper-level context prepended to every chunk.

    ~50–150 chars that anchor 'what paper is this from' for embedding models
    that benefit from per-chunk context (Qwen3-Embedding, nomic, etc.).
    """
    title = (item.get("title") or "").strip()
    venue = (item.get("bookTitle") or item.get("publicationTitle") or "").strip()
    authors: list[str] = item.get("authors") or []
    pub = (item.get("publisher") or "").strip()

    parts = [title] if title else []
    if venue:
        parts.append(f"In: {venue}")
    if authors:
        # Cap at 3 to avoid bloat for large edited volumes.
        a = ", ".join(authors[:3])
        if len(authors) > 3:
            a += " et al."
        parts.append(f"Authors: {a}")
    if pub and not venue:
        # Only show publisher when there is no venue — avoids redundancy.
        parts.append(f"Publisher: {pub}")
    return "\n".join(parts)


def chunk_item(item: dict) -> list[Chunk]:
    """Chunk one item returned by list_items_for_semantic_index().

    Contract:
      * Returns at least one chunk when the item has title or abstract.
      * Returns [] when the item has neither (caller should skip).
      * For ft-cache chunks, prepends a rich context header (title + venue +
        authors) so every chunk is independently retrievable. Offsets refer
        to the ft-cache content, not the header.
    """
    header = _build_context_header(item)
    abstract = (item.get("abstract") or "").strip()
    attachment_key = item.get("attachment_key") or ""

    ft_text = _read_ft_cache(attachment_key) if attachment_key else ""

    if ft_text:
        raw_chunks = _sliding_chunks(ft_text)
        prefix = f"{header}\n\n" if header else ""
        return [
            Chunk(
                text=prefix + c.text,
                char_start=c.char_start,
                char_end=c.char_end,
                source="ft_cache",
            )
            for c in raw_chunks
        ]

    # Abstract-only path.
    if not header and not abstract:
        return []
    text = f"{header}\n\n{abstract}".strip() if abstract else header
    return [Chunk(text=text, char_start=0, char_end=len(text), source="abstract")]
