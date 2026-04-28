"""Chunking for the semantic index.

Turns Zotero items into one or more chunks suitable for embedding.

Strategy (in order of preference, per item):
  1. **Structural**: when a ``.article.json`` exists for the item's DOI
     (created by :mod:`pdf_extractor` during ``fetch_fulltext``), each
     detected section becomes one chunk, with sections that exceed
     ``_CHUNK_CHARS`` sliced by sliding windows *within* the section so
     boundaries respect headings.  Each chunk's text is prefixed with
     the section title — a strong retrieval signal for academic queries
     ("methodology of X", "results in Y").
  2. **Sliding (ft-cache)**: when only a Zotero ``.zotero-ft-cache``
     file is available, fall back to overlapping ~400-token windows.
     Character offsets are preserved so chunk-level hits can be fed to
     ``fetch_fulltext(mode="range", ...)``.
  3. **Abstract-only**: when the item has neither attachment nor
     extracted fulltext, emit a single chunk of title + venue +
     abstract.

Tokenization: we use a char-based approximation (~4 chars/token for
English). This is imprecise but (a) the target chunk size is a
soft preference, not a hard limit — even 512-context BERT-family
embedders truncate cleanly; (b) adding tiktoken would inflate the
dependency surface for something that doesn't benefit from
millisecond accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import text_cache, zotero_sqlite

# ~400 tokens ≈ 1600 chars. 50-token overlap ≈ 200 chars.
# 400 is the long-trodden sweet spot for academic retrieval (256–512 in
# the literature); we've measured no benefit from going smaller, only
# more chunks to manage.  Models with smaller context windows (bge-large
# at 512 tokens, gte-large v1.0 at 512) still cleanly accept 400+header.
_CHARS_PER_TOKEN = 4
_TARGET_CHUNK_TOKENS = 400
_OVERLAP_TOKENS = 50
_CHUNK_CHARS = _TARGET_CHUNK_TOKENS * _CHARS_PER_TOKEN   # 1600
_OVERLAP_CHARS = _OVERLAP_TOKENS * _CHARS_PER_TOKEN       # 200
_STRIDE_CHARS = _CHUNK_CHARS - _OVERLAP_CHARS              # 1400

# Upper bound on how much ft-cache to consider per item.
#
# ``None`` = no cap.  Long monographs are indexed in full, which matters
# for humanities/policy libraries where readers genuinely care about
# content past page 125.  Storage impact is bounded by the size of the
# Zotero ft-cache files themselves, which are themselves capped by
# Zotero's PDF indexing settings — typically a few MB per item even for
# 800-page volumes.
#
# Set to an integer (e.g. ``200_000`` for ~125 pages) if you need to
# bound the chunk count for cost/time reasons.
_MAX_FT_CHARS: int | None = None


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
    """Return up to ``_MAX_FT_CHARS`` from an attachment's .zotero-ft-cache, or ''.

    When ``_MAX_FT_CHARS`` is ``None``, the entire ft-cache file is read.
    """
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
            # f.read(None) reads the whole stream; f.read(N) reads up to N.
            return f.read(_MAX_FT_CHARS) if _MAX_FT_CHARS is not None else f.read()
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


# Maximum characters per prefix component, to keep the combined
# header + section-title + content under 512 tokens (the smallest
# context window we routinely target — bge-large-en-v1.5 / gte-v1.0).
# At ~4 chars/token: title 200c≈50t, venue 120c≈30t, section 120c≈30t
# → prefix ≤ ~110 tokens; +400-token content = ~510 → fits cleanly.
_HEADER_TITLE_MAX_CHARS = 200
_HEADER_VENUE_MAX_CHARS = 120
_SECTION_TITLE_MAX_CHARS = 120


def _truncate(s: str, n: int) -> str:
    """Truncate *s* to at most *n* characters, adding an ellipsis when cut."""
    s = s.strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


def _build_context_header(item: dict) -> str:
    """Compact paper-level context prepended to every chunk.

    Title plus the container title (book/journal/website) when present.
    Both fields are length-capped so the prefix never crowds out the
    embedder's context window — see ``_HEADER_*_MAX_CHARS`` above.
    """
    title = _truncate(item.get("title") or "", _HEADER_TITLE_MAX_CHARS)
    venue_raw = (item.get("bookTitle") or item.get("publicationTitle") or "")
    venue = _truncate(venue_raw, _HEADER_VENUE_MAX_CHARS)

    parts = [title] if title else []
    if venue:
        parts.append(f"In: {venue}")
    return "\n".join(parts)


def _section_chunks(
    article: "text_cache.CachedArticle", header: str
) -> list[Chunk]:
    """Build chunks from a structurally-extracted article.

    One chunk per section.  Sections longer than ``_CHUNK_CHARS`` are
    sliced via sliding windows *within* the section so chunk boundaries
    respect section boundaries — the most useful structural signal we
    have for academic retrieval.

    Each chunk's text is prefixed with ``{header}\\nSection: {title}``.
    Both fields are length-capped (see ``_HEADER_*`` / ``_SECTION_TITLE_MAX_CHARS``)
    so the combined prefix + 400-token content fits inside the smallest
    embedder context window we routinely target (512 tokens).
    """
    text = article.text or ""
    chunks: list[Chunk] = []
    for sec in article.sections or []:
        try:
            start = int(sec.get("start") or 0)
            end = int(sec.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start or start < 0 or end > len(text):
            continue
        section_text = text[start:end]
        if not section_text.strip():
            continue

        sec_title = _truncate(
            (sec.get("title") or "").strip(), _SECTION_TITLE_MAX_CHARS
        )
        prefix_parts = [header] if header else []
        if sec_title:
            prefix_parts.append(f"Section: {sec_title}")
        prefix = "\n".join(prefix_parts)
        prefix_block = f"{prefix}\n\n" if prefix else ""

        if len(section_text) <= _CHUNK_CHARS:
            chunks.append(
                Chunk(
                    text=prefix_block + section_text,
                    char_start=start,
                    char_end=end,
                    source="article_section",
                )
            )
        else:
            for sub in _sliding_chunks(section_text):
                # Sub-chunk offsets are relative to section_text; translate
                # back to offsets in the full article.text so callers can
                # still ask the article cache for the precise byte range.
                actual_start = start + sub.char_start
                actual_end = start + sub.char_end
                chunks.append(
                    Chunk(
                        text=prefix_block + sub.text,
                        char_start=actual_start,
                        char_end=actual_end,
                        source="article_section",
                    )
                )
    return chunks


def chunk_item(item: dict) -> list[Chunk]:
    """Chunk one item returned by list_items_for_semantic_index().

    Contract:
      * Returns at least one chunk when the item has title or abstract.
      * Returns [] when the item has neither (caller should skip).
      * Prefers structural chunking (one chunk per ``.article.json``
        section) when a cached extraction exists for the item's DOI;
        falls back to sliding windows over ``.zotero-ft-cache``; falls
        back to title+venue+abstract single chunk.
      * Every chunk text is prefixed with paper-level context (title,
        venue, and — for structural chunks — section title) so each
        chunk is independently retrievable.  Offsets refer to the
        underlying text source, not the prefix.
    """
    header = _build_context_header(item)
    abstract = (item.get("abstract") or "").strip()
    attachment_key = item.get("attachment_key") or ""
    doi = (item.get("doi") or "").strip()

    # 1. Structural path: use cached article sections when available.
    if doi:
        try:
            article = text_cache.get_cached(doi)
        except Exception:
            article = None
        if article is not None and article.sections and (article.text or "").strip():
            sec_chunks = _section_chunks(article, header)
            if sec_chunks:
                return sec_chunks

    # 2. Sliding-windows path: ft-cache full text.
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

    # 3. Abstract-only path.
    if not header and not abstract:
        return []
    text = f"{header}\n\n{abstract}".strip() if abstract else header
    return [Chunk(text=text, char_start=0, char_end=len(text), source="abstract")]
