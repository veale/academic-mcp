"""Persistent text cache for extracted article content.

Stores extracted full text and detected sections as JSON files in
pdf_cache_dir, keyed by a SHA-256 hash of the normalised DOI.  Files use
the ``.article.json`` extension so they are distinguishable from PDF cache
files but still subject to LRU eviction.

Typical usage::

    cached = text_cache.get_cached(doi)
    if cached:
        # fast path — no network or PDF parsing needed
        ...

    article = text_cache.put_cached(doi, text, source, sections,
                                    section_detection="html_headings")
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_doi(doi: str) -> str:
    """Lowercase, strip whitespace and leading ``doi:`` prefix."""
    norm = doi.lower().strip()
    if norm.startswith("doi:"):
        norm = norm[4:]
    return norm


def _cache_key(doi: str) -> str:
    """Return SHA-256 hex digest of the normalised DOI."""
    return hashlib.sha256(_normalize_doi(doi).encode()).hexdigest()


def _cache_path(doi: str) -> Path:
    return config.pdf_cache_dir / f"{_cache_key(doi)}.article.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CachedArticle:
    """Full-text content cached for a single paper.

    ``sections`` is a list of dicts with keys:
      - ``title``      (str)  — heading text
      - ``level``      (int)  — 2 for h2/main, 3 for h3/sub
      - ``start``      (int)  — character offset in ``text``
      - ``end``        (int)  — character offset in ``text`` (exclusive)
      - ``word_count`` (int)  — words in this section

    ``section_detection`` describes how sections were derived:
      - ``"html_headings"``     — marker injection into HTML before trafilatura;
                                  high confidence, exact boundaries
      - ``"pdf_toc"``           — PDF bookmark/outline tree (hyperref); clean
                                  titles with exact hierarchy
      - ``"pdf_font_analysis"`` — font-size threshold on PyMuPDF span data;
                                  reliable for most academic PDFs
      - ``"pymupdf4llm_markdown"`` — pymupdf4llm Markdown extraction with
                                  AcademicHeaderDetector; also provides tables
      - ``"text_heuristic"``    — conservative regex on plain text (ft-cache);
                                  approximate, may miss subsections
      - ``"keyword_skeleton"``  — TF-IDF chunks; no structural headings found
      - ``"unknown"``           — migrated from pre-field cache entry

    ``metadata`` contains bibliographic info and extraction flags:
      - ``title``      (str)  — paper title
      - ``authors``    (list) — list of author names
      - ``year``       (str)  — publication year
      - ``venue``      (str)  — publication venue
      - ``is_ocr``     (bool) — True if the PDF was identified as OCR/scanned
    """
    doi: str
    text: str
    source: str
    sections: list = field(default_factory=list)
    section_detection: str = "unknown"
    word_count: int = 0
    metadata: dict = field(default_factory=dict)
    cached_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    pdf_path: Optional[str] = None
    html_path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CachedArticle":
        # Graceful upgrade: files written before these fields existed get
        # sensible defaults so we never crash on a stale cache entry.
        d.setdefault("section_detection", "unknown")
        d.setdefault("word_count", len(d.get("text", "").split()))
        d.setdefault("metadata", {})
        d.setdefault("pdf_path", None)
        d.setdefault("html_path", None)
        return cls(**d)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_cached(doi: str) -> Optional[CachedArticle]:
    """Return the cached article for *doi*, or ``None`` if not cached.

    Touching the file on read updates its mtime so LRU eviction keeps
    recently-accessed articles alive.
    """
    path = _cache_path(doi)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        path.touch()  # refresh mtime for LRU eviction
        return CachedArticle.from_dict(data)
    except Exception as exc:
        logger.debug("Failed to read article cache for %s: %s", doi, exc)
        return None


def put_cached(
    doi: str,
    text: str,
    source: str,
    sections: list,
    section_detection: str = "unknown",
    word_count: int = 0,
    metadata: dict | None = None,
    pdf_path: str | None = None,
    html_path: str | None = None,
) -> CachedArticle:
    """Write *text* and *sections* to the article cache for *doi*.

    Returns the ``CachedArticle`` so callers can use it immediately
    without a second ``get_cached`` round-trip.
    """
    wc = word_count or len(text.split())
    meta = metadata or {}
    article = CachedArticle(
        doi=doi,
        text=text,
        source=source,
        sections=sections,
        section_detection=section_detection,
        word_count=wc,
        metadata=meta,
        pdf_path=pdf_path,
        html_path=html_path,
    )
    path = _cache_path(doi)
    try:
        path.write_text(
            json.dumps(article.to_dict(), ensure_ascii=False, indent=None),
            encoding="utf-8",
        )
        logger.debug("Cached article for %s → %s", doi, path.name)
    except Exception as exc:
        logger.warning("Failed to write article cache for %s: %s", doi, exc)
    return article


def charmap_path(cache_key: str) -> Path:
    """Return the path to the charmap binary for a given *cache_key*.

    The charmap file is co-located with the ``.article.json`` and uses the
    same cache key so they are evicted together.
    """
    return config.pdf_cache_dir / f"{cache_key}.charmap.bin"


def load_by_cache_key(cache_key: str) -> Optional[CachedArticle]:
    """Return the cached article for a raw SHA-256 *cache_key*, or ``None``."""
    path = config.pdf_cache_dir / f"{cache_key}.article.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CachedArticle.from_dict(data)
    except Exception as exc:
        logger.debug("Failed to read article cache for key %s: %s", cache_key, exc)
        return None


def update_paths(
    cache_key: str,
    pdf_path: str | None = None,
    html_path: str | None = None,
) -> None:
    """Update pdf_path / html_path on an existing cached article in-place.

    No-op if the article isn't cached yet.  Cleans up any legacy
    ``.paths.json`` sidecar file that was written by the old article_store.
    """
    art = load_by_cache_key(cache_key)
    if not art:
        return
    if pdf_path is not None:
        art.pdf_path = pdf_path
    if html_path is not None:
        art.html_path = html_path
    path = config.pdf_cache_dir / f"{cache_key}.article.json"
    try:
        path.write_text(
            json.dumps(art.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Failed to update paths for %s: %s", cache_key, exc)
    # Lazy migration: remove the legacy sidecar if it exists
    sidecar = config.pdf_cache_dir / f"{cache_key}.paths.json"
    sidecar.unlink(missing_ok=True)
