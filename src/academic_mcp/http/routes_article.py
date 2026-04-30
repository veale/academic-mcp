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
from .article_store import load_paths, store_paths

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


class HighlightsResponse(BaseModel):
    cache_key: str
    chunks: list[HighlightChunk]


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
        store_paths(cache_key, fa.pdf_path, fa.html_path)

    paths = load_paths(cache_key)
    pdf_available = bool(paths.get("pdf_path") and Path(paths["pdf_path"]).exists())
    html_available = bool(paths.get("html_path") and Path(paths["html_path"]).exists())
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
    return ArticleTextResponse(
        doi=article.doi,
        cache_key=cache_key,
        text=article.text,
        sections=article.sections,
        section_detection=article.section_detection,
        word_count=article.word_count,
    )


# ---------------------------------------------------------------------------
# GET /api/article/html
# ---------------------------------------------------------------------------

@router.get("/api/article/html", dependencies=[AuthRequired])
async def get_article_html(cache_key: str = Query(...)):
    paths = load_paths(cache_key)
    html_path = paths.get("html_path")
    if not html_path or not Path(html_path).exists():
        raise HTTPException(status_code=404, detail="HTML not available for this article")
    return FileResponse(html_path, media_type="text/html")


# ---------------------------------------------------------------------------
# GET /api/article/pdf
# ---------------------------------------------------------------------------

@router.get("/api/article/pdf", dependencies=[AuthRequired])
async def get_article_pdf(request: Request, cache_key: str = Query(...)):
    paths = load_paths(cache_key)
    pdf_path = paths.get("pdf_path")
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

@router.get("/api/article/highlights", dependencies=[AuthRequired])
async def get_article_highlights(
    cache_key: str = Query(...),
    q: str = Query(..., min_length=1),
    k: int = Query(10, ge=1, le=50),
) -> HighlightsResponse:
    article = tc.load_by_cache_key(cache_key)
    if not article:
        raise HTTPException(status_code=404, detail="Article not in cache")

    try:
        result = await core_in_article.search_in_article(
            doi=article.doi,
            terms=[q],
            context_chars=300,
            max_matches=k,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Article not in cache")

    chunks: list[HighlightChunk] = []
    for term_result in result.term_results:
        for match in term_result.matches:
            page_rects = offsets_to_pdf_rects(
                cache_key, [(match.char_start, match.char_end)]
            )
            chunks.append(HighlightChunk(
                score=match.bm25_score or 1.0,
                char_start=match.char_start,
                char_end=match.char_end,
                snippet=match.snippet,
                page_rects=page_rects,
            ))

    chunks.sort(key=lambda c: c.score, reverse=True)
    return HighlightsResponse(cache_key=cache_key, chunks=chunks[:k])


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
