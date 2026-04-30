"""Return types for academic_mcp core functions.

All types are Pydantic models so they can be serialised directly to JSON
by the webapp layer (Phase 1+).  Import them in core modules and handlers.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------

class ZoteroIndexRefresh(BaseModel):
    doi_count: int
    index_path: str
    connections: dict  # backend -> status dict from zotero.check_connections()
    local_enabled: bool = False
    local_host: str = "localhost"
    local_port: int = 23119
    sqlite_active: bool = False


# ---------------------------------------------------------------------------
# Search / Zotero lookup
# ---------------------------------------------------------------------------

class DoiSearchResult(BaseModel):
    found: bool
    source: str  # "sqlite" | "doi_index"
    title: str | None = None
    doi: str | None = None
    library_name: str | None = None
    library_type: str | None = None
    item_type: str | None = None
    date: str | None = None
    authors: list[str] = []
    abstract: str | None = None
    key: str | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

class SemanticHit(BaseModel):
    item_key: str | None = None
    title: str | None = None
    doi: str | None = None
    score: float = 0.0
    rerank_score: float | None = None
    chunk_source: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    chunk_idx: int | None = None
    chunk_count: int | None = None
    snippet: str | None = None


# ---------------------------------------------------------------------------
# Paper metadata
# ---------------------------------------------------------------------------

class PdfUrlInfo(BaseModel):
    url: str
    source: str


class PaperInfo(BaseModel):
    identifier: str
    title: str
    doi: str | None = None
    s2_id: str | None = None
    authors: list[str] = []
    year: int | str | None = None
    venue: str | None = None
    citation_count: int | None = None
    reference_count: int | None = None
    abstract: str | None = None
    tldr: str | None = None
    pdf_urls: list[PdfUrlInfo] = []
    is_oa: bool = False
    oa_type: str | None = None
    oa_container: str | None = None


# ---------------------------------------------------------------------------
# Citations / references
# ---------------------------------------------------------------------------

class CitationWorkItem(BaseModel):
    title: str
    doi: str | None = None
    openalex_id: str | None = None
    authors: list[str] = []
    year: int | None = None
    venue: str | None = None
    cited_by_count: int = 0
    abstract: str | None = None
    in_zotero: bool = False


class CitationsResult(BaseModel):
    doi: str
    direction: str  # "citations" | "references"
    total: int
    items: list[CitationWorkItem]
    dropped: int = 0
    error: str | None = None


class CitationTreeResult(BaseModel):
    doi: str
    citations: CitationsResult | None = None
    references: CitationsResult | None = None


# ---------------------------------------------------------------------------
# Search in article
# ---------------------------------------------------------------------------

class TermMatch(BaseModel):
    char_start: int
    char_end: int
    snippet: str
    match_start: int | None = None  # offset of match start within snippet
    match_end: int | None = None    # offset of match end within snippet
    section: str | None = None
    bm25_score: float | None = None
    is_bm25: bool = False


class TermResult(BaseModel):
    term: str
    total_hits: int
    matches: list[TermMatch]
    segment_counts: list[int]  # 10 equal-segment counts


class InArticleResult(BaseModel):
    doi: str
    segment_length: int
    sections: list[dict]
    term_results: list[TermResult]


# ---------------------------------------------------------------------------
# PDF highlight geometry
# ---------------------------------------------------------------------------

class Rect(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class PageRects(BaseModel):
    page: int        # 0-indexed page number
    rects: list[Rect]


# ---------------------------------------------------------------------------
# Fetch / article retrieval
# ---------------------------------------------------------------------------

class Section(BaseModel):
    title: str
    char_start: int
    char_end: int
    level: int = 2
    keywords: list[str] = []
    word_count: int = 0
    is_infill: bool = False


class PreviewChunk(BaseModel):
    section_title: str | None  # None for pre-first-heading preamble
    text: str                  # raw text, possibly truncated
    word_count_total: int      # total words in the source section
    word_count_shown: int      # words actually included in text


class FetchMode(str, Enum):
    full = "full"
    sections = "sections"
    section = "section"
    preview = "preview"
    range = "range"


class ArticleId(BaseModel):
    doi: str | None = None
    zotero_key: str | None = None
    url: str | None = None


class FetchedArticle(BaseModel):
    # Identity
    doi: str
    cache_key: str = ""

    # Source pointers
    pdf_path: str | None = None
    html_path: str | None = None
    source: str = ""

    # Body — raw extracted text only. No headers, no markers, no markdown.
    text: str = ""
    sections: list[dict] = []
    section_detection: str = "unknown"
    word_count: int = 0
    metadata: dict = {}
    truncated: bool = False

    # Mode result — structured, not pre-rendered
    mode: FetchMode = FetchMode.sections
    matched_section: Section | None = None
    available_sections: list[Section] = []
    range_chars: tuple[int, int] | None = None
    preview_chunks: list[PreviewChunk] = []

    # Operational
    auto_import_status: str | None = None
    attempted_sources: list[str] = []
    error: str | None = None
    failure_hints: list[str] = []


# ---------------------------------------------------------------------------
# Book chapters
# ---------------------------------------------------------------------------

class BookChaptersResult(BaseModel):
    items: list[dict] = []
    book_title: str = ""
    isbns: list[str] = []
    seed_type: str = ""
    seed_doi: str = ""
    error: str | None = None
