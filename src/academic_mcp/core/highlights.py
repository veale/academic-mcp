"""Convert character-offset ranges to PDF page rectangles for viewer highlighting.

The charmap binary stored at ``<cache_key>.charmap.bin`` has one 18-byte record
per character in the companion ``.article.json`` text.  Each record is a
big-endian uint16 (0-indexed page number) followed by 4 × float32 (x0, y0,
x1, y1) in PDF user-space coordinates.  Records for whitespace and synthetic
characters (page-header lines, spaces inserted between spans) are all-zeros.

Invariant: the charmap was produced by
``pdf_extractor.build_charmap_bytes(source, text)`` where *text* is exactly
the string stored in ``.article.json``.  Never call ``offsets_to_pdf_rects``
with offsets into a differently-derived text.
"""

from __future__ import annotations

import struct

from ..config import config
from ..text_cache import charmap_path
from .types import PageRects, Rect

_FMT = ">Hffff"
_RECORD_SIZE: int = struct.calcsize(_FMT)   # 18
# Tolerance (PDF points) for deciding two char rects share the same line.
# 2 pt covers typical inter-line spacing variation in academic PDFs.
_LINE_Y_TOLERANCE: float = 2.0


def offsets_to_pdf_rects(
    cache_key: str,
    ranges: list[tuple[int, int]],
) -> list[PageRects]:
    """Return PDF page rectangles for character-offset *ranges* in a cached article.

    *cache_key* is the SHA-256 hex digest of the normalised DOI, as returned by
    ``text_cache._cache_key(doi)``.  *ranges* is a list of ``(start, end)``
    half-open character intervals into the article text.

    Returns a list of :class:`PageRects`, one entry per page that contains at
    least one highlighted character, with contiguous characters on the same
    visual line merged into a single :class:`Rect`.  An empty list is returned
    when no charmap file exists for the given key or when all matched characters
    have null records (i.e. they fall in synthetic / whitespace regions).
    """
    cm_path = charmap_path(cache_key)
    if not cm_path.exists():
        return []

    data = cm_path.read_bytes()
    total_chars = len(data) // _RECORD_SIZE

    # Collect (page, x0, y0, x1, y1) for all chars in the requested ranges,
    # skipping null records (whitespace / synthetic chars).
    raw: list[tuple[int, float, float, float, float]] = []
    for start, end in ranges:
        s = max(0, start)
        e = min(end, total_chars)
        for i in range(s, e):
            page, x0, y0, x1, y1 = struct.unpack_from(_FMT, data, i * _RECORD_SIZE)
            if x0 == 0.0 and y0 == 0.0 and x1 == 0.0 and y1 == 0.0:
                continue
            raw.append((page, x0, y0, x1, y1))

    if not raw:
        return []

    return _merge_to_page_rects(raw)


def _merge_to_page_rects(
    raw: list[tuple[int, float, float, float, float]],
) -> list[PageRects]:
    """Merge per-character rects into line-level rects and group by page.

    Within a page, characters whose y-midpoints are within ``_LINE_Y_TOLERANCE``
    of the previous character's midpoint are considered to be on the same line
    and merged into a single bounding rect.  Characters on different lines
    (or on different pages) start a new rect.
    """
    # Sort: page asc, y0 asc (top of line), x0 asc (left to right).
    raw.sort(key=lambda r: (r[0], r[2], r[1]))

    merged: list[tuple[int, float, float, float, float]] = []
    for page, x0, y0, x1, y1 in raw:
        if not merged:
            merged.append((page, x0, y0, x1, y1))
            continue
        lp, lx0, ly0, lx1, ly1 = merged[-1]
        l_mid = (ly0 + ly1) / 2
        cur_mid = (y0 + y1) / 2
        if lp == page and abs(cur_mid - l_mid) <= _LINE_Y_TOLERANCE:
            # Same line — extend the existing rect to cover the new char.
            merged[-1] = (lp, min(lx0, x0), min(ly0, y0), max(lx1, x1), max(ly1, y1))
        else:
            merged.append((page, x0, y0, x1, y1))

    pages: dict[int, list[Rect]] = {}
    for page, x0, y0, x1, y1 in merged:
        pages.setdefault(page, []).append(Rect(x0=x0, y0=y0, x1=x1, y1=y1))

    return [
        PageRects(page=pg, rects=rects)
        for pg, rects in sorted(pages.items())
    ]
