from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import text_cache as tc
from ..core import fetch as core_fetch
from ..core import in_article as core_in_article
from ..core.highlights import offsets_to_pdf_rects
from ..core.types import ArticleId, FetchMode, InArticleResult, PageRects
from .app import AuthRequired

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class ArticleViewers(BaseModel):
    pdf: bool = False
    html: bool = False
    text: bool = False


class ArticleMetaResponse(BaseModel):
    doi: str
    cache_key: str
    source: str
    word_count: int
    section_count: int
    section_detection: str
    metadata: dict
    viewers: ArticleViewers
    error: str | None = None
    failure_hints: list[str] = []


class ArticleTextResponse(BaseModel):
    doi: str
    cache_key: str
    text: str
    sections: list[dict]
    section_detection: str
    word_count: int


class HighlightChunk(BaseModel):
    score: float
    char_start: int
    char_end: int
    snippet: str
    page_rects: list[PageRects] = []
    match_type: str = "lexical"  # "semantic" | "lexical"


class HighlightsResponse(BaseModel):
    cache_key: str
    chunks: list[HighlightChunk]
    page_dimensions: dict[int, list[float]] = {}  # page_index -> [width_pts, height_pts]


# ---------------------------------------------------------------------------
# GET /api/article
# ---------------------------------------------------------------------------

@router.get("/api/article", dependencies=[AuthRequired])
async def get_article(
    doi: str | None = None,
    zotero_key: str | None = None,
    url: str | None = None,
) -> ArticleMetaResponse:
    if not doi and not zotero_key and not url:
        raise HTTPException(status_code=422, detail="Provide doi, zotero_key, or url")

    identifier = ArticleId(doi=doi, zotero_key=zotero_key, url=url)
    fa = await core_fetch.fetch_article(identifier, mode="sections")

    if fa.error and not fa.text and not fa.cache_key:
        raise HTTPException(status_code=404, detail=fa.error)

    cache_key = fa.cache_key or tc._cache_key(fa.doi or doi or "")

    if fa.pdf_path or fa.html_path:
        tc.update_paths(cache_key, fa.pdf_path, fa.html_path)

    art = tc.load_by_cache_key(cache_key)
    pdf_available = bool(art and art.pdf_path and Path(art.pdf_path).exists())
    html_available = bool(art and art.html_path and Path(art.html_path).exists())
    text_available = bool(fa.text or fa.word_count)

    return ArticleMetaResponse(
        doi=fa.doi,
        cache_key=cache_key,
        source=fa.source,
        word_count=fa.word_count,
        section_count=len(fa.available_sections),
        section_detection=fa.section_detection,
        metadata=fa.metadata,
        viewers=ArticleViewers(
            pdf=pdf_available,
            html=html_available,
            text=text_available,
        ),
        error=fa.error,
        failure_hints=fa.failure_hints,
    )


# ---------------------------------------------------------------------------
# GET /api/article/text
# ---------------------------------------------------------------------------

@router.get("/api/article/text", dependencies=[AuthRequired])
async def get_article_text(cache_key: str = Query(...)) -> ArticleTextResponse:
    article = tc.load_by_cache_key(cache_key)
    if not article:
        raise HTTPException(status_code=404, detail="Article not in cache")
    # Cached sections use start/end; the wire schema uses char_start/char_end.
    sections = [
        {
            "title": s.get("title", ""),
            "char_start": s.get("char_start", s.get("start", 0)),
            "char_end": s.get("char_end", s.get("end", 0)),
            "level": s.get("level", 2),
            "keywords": s.get("keywords", []),
            "word_count": s.get("word_count", 0),
            "is_infill": s.get("is_infill", False),
        }
        for s in (article.sections or [])
    ]
    return ArticleTextResponse(
        doi=article.doi,
        cache_key=cache_key,
        text=article.text,
        sections=sections,
        section_detection=article.section_detection,
        word_count=article.word_count,
    )


# ---------------------------------------------------------------------------
# GET /api/article/html
# ---------------------------------------------------------------------------

@router.get("/api/article/html", dependencies=[AuthRequired])
async def get_article_html(cache_key: str = Query(...)):
    art = tc.load_by_cache_key(cache_key)
    html_path = art.html_path if art else None
    if not html_path or not Path(html_path).exists():
        raise HTTPException(status_code=404, detail="HTML not available for this article")
    return FileResponse(html_path, media_type="text/html")


# ---------------------------------------------------------------------------
# GET /api/article/pdf
# ---------------------------------------------------------------------------

@router.get("/api/article/pdf", dependencies=[AuthRequired])
async def get_article_pdf(request: Request, cache_key: str = Query(...)):
    art = tc.load_by_cache_key(cache_key)
    pdf_path = art.pdf_path if art else None
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF not available for this article")
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={"Accept-Ranges": "bytes"},
    )


# ---------------------------------------------------------------------------
# GET /api/article/highlights
# ---------------------------------------------------------------------------

async def _resolve_item_key(article, explicit: str | None) -> str | None:
    """Resolve a Zotero item_key for a cached article (best-effort)."""
    if explicit:
        return explicit
    md_key = article.metadata.get("key") if article.metadata else None
    if md_key:
        return str(md_key)
    doi = article.doi or ""
    if doi.startswith(("zotero:", "url:")):
        if doi.startswith("zotero:"):
            return doi.split(":", 1)[1] or None
        return None
    if not doi:
        return None
    try:
        from .. import zotero as zotero_mod
        idx = await zotero_mod.get_doi_index()
        entry = idx.get(zotero_mod._normalize_doi(doi))
        if entry:
            return entry.get("item_key")
    except Exception:
        return None
    return None


def _strip_chunk_prefix(snippet: str) -> str:
    """Remove the chunking-prefix (title / section header) from a chunk snippet.

    `chunking._section_chunks` and the ft_cache path both prepend a
    ``"{header}\\n\\n{body}"`` block.  Strip everything up to the first ``\\n\\n``
    to get back to text that exists in cached.text.
    """
    if not snippet:
        return ""
    parts = snippet.split("\n\n", 1)
    return parts[1] if len(parts) == 2 else snippet


def _align_offsets_to_text(
    text: str, snippet: str, source: str, fallback: tuple[int, int],
) -> tuple[int, int] | None:
    """Map a chunk's offsets onto *text* (the cached.text body).

    - ``article_section`` chunks already use cached.text coordinates — trust
      the recorded ``char_start``/``char_end`` directly.
    - Everything else (ft_cache slices, abstract-only chunks) was indexed
      against a different text source, so we string-search the snippet body
      (sans prefix) inside cached.text and recover a real range.

    Returns ``None`` when no alignment can be found — caller drops the chunk.
    """
    cs, ce = fallback
    if source == "article_section" and 0 <= cs < ce <= len(text):
        return cs, ce
    body = _strip_chunk_prefix(snippet).strip()
    if not body:
        return None
    probe = body[:80]
    found = text.find(probe)
    if found < 0:
        return None
    span_len = max(ce - cs, len(probe))
    return found, min(found + span_len, len(text))


@router.get("/api/article/highlights", dependencies=[AuthRequired])
async def get_article_highlights(
    cache_key: str = Query(...),
    q: str = Query(..., min_length=1),
    k: int = Query(20, ge=1, le=50),
    zotero_key: str | None = Query(None),
) -> HighlightsResponse:
    article = tc.load_by_cache_key(cache_key)
    if not article:
        raise HTTPException(status_code=404, detail="Article not in cache")

    text = article.text or ""
    chunks: list[HighlightChunk] = []

    # ── Tier 1: semantic chunks from the Zotero index ──────────────────
    item_key = await _resolve_item_key(article, zotero_key)
    if item_key and text:
        try:
            from ..semantic_index import get_semantic_index, SemanticIndexUnavailable
            idx = get_semantic_index()
            sem = await idx.search_within_item(q, item_key, k=k)
            for c in sem:
                aligned = _align_offsets_to_text(
                    text, c.get("snippet", ""),
                    c.get("chunk_source", ""),
                    (c["char_start"], c["char_end"]),
                )
                if not aligned:
                    continue
                cs, ce = aligned
                # Show the body without the chunk prefix in the UI.
                snippet_body = _strip_chunk_prefix(c.get("snippet", "")) or text[cs:ce]
                snippet_for_ui = snippet_body[:300]
                rects = offsets_to_pdf_rects(cache_key, [(cs, ce)])
                chunks.append(HighlightChunk(
                    score=float(c.get("score") or 0.0),
                    char_start=cs,
                    char_end=ce,
                    snippet=snippet_for_ui,
                    page_rects=rects,
                    match_type="semantic",
                ))
        except SemanticIndexUnavailable:
            pass
        except Exception as exc:
            logger.debug("Semantic highlight tier failed for %s: %s", cache_key, exc)

    # ── Tier 2: BM25 fallback (when semantic tier returned nothing) ────
    if not chunks:
        try:
            result = await core_in_article.search_in_article(
                doi=article.doi,
                terms=[q],
                context_chars=300,
                max_matches=k,
            )
            for term_result in result.term_results:
                for match in term_result.matches:
                    rects = offsets_to_pdf_rects(
                        cache_key, [(match.char_start, match.char_end)]
                    )
                    chunks.append(HighlightChunk(
                        score=float(match.bm25_score or 1.0),
                        char_start=match.char_start,
                        char_end=match.char_end,
                        snippet=match.snippet,
                        page_rects=rects,
                        match_type="lexical",
                    ))
        except LookupError:
            pass

    chunks.sort(key=lambda c: c.score, reverse=True)
    top_chunks = chunks[:k]

    page_dimensions: dict[int, list[float]] = {}
    pdf_path = article.pdf_path
    if pdf_path and Path(pdf_path).exists():
        try:
            import fitz  # PyMuPDF
            referenced = {r.page for c in top_chunks for r in c.page_rects}
            doc = fitz.open(pdf_path)
            for p in referenced:
                if 0 <= p < len(doc):
                    r = doc[p].rect
                    page_dimensions[p] = [r.width, r.height]
            doc.close()
        except Exception:
            pass

    return HighlightsResponse(
        cache_key=cache_key,
        chunks=top_chunks,
        page_dimensions=page_dimensions,
    )


# ---------------------------------------------------------------------------
# GET /api/article/in_article
# ---------------------------------------------------------------------------

@router.get("/api/article/in_article", dependencies=[AuthRequired])
async def get_in_article(
    cache_key: str = Query(...),
    q: str = Query(..., min_length=1),
) -> InArticleResult:
    article = tc.load_by_cache_key(cache_key)
    if not article:
        raise HTTPException(status_code=404, detail="Article not in cache")

    try:
        return await core_in_article.search_in_article(
            doi=article.doi,
            terms=q.split(),
            context_chars=500,
            max_matches=5,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Article not in cache")
