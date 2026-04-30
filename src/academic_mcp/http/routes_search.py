from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..core import search as core_search
from ..core import semantic as core_semantic
from ..core import paper as core_paper
from ..core.types import SemanticHit, PaperInfo, ScitePayload
from .app import AuthRequired

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    title: str
    authors: list[str] = []
    year: str | None = None
    doi: str | None = None
    zotero_key: str | None = None
    abstract: str | None = None
    citations: int | None = None
    venue: str | None = None
    found_in: list[str] = []
    in_zotero: bool = False
    has_oa_pdf: bool = False
    s2_id: str | None = None
    url: str | None = None
    work_type: str | None = None
    container_title: str | None = None
    scite: ScitePayload | None = None
    score: float | None = None
    semantic_zotero_score: float | None = None
    scite_adjust: float | None = None
    primo_proxy_url: str | None = None
    primo_oa_url: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    query: str


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------

@router.get("/api/search", dependencies=[AuthRequired])
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=20),
    zotero_only: bool = False,
    semantic: bool | None = None,
    include_scite: bool = False,
    domain_hint: str = "general",
) -> SearchResponse:
    source = "zotero" if zotero_only else "all"
    raw = await core_search.search_papers(
        query=q,
        limit=limit,
        source=source,
        domain_hint=domain_hint,
        include_scite=include_scite,
        semantic=semantic,
    )
    results = [
        SearchResult(
            title=hit.title,
            authors=hit.authors,
            year=str(hit.year) if hit.year is not None else None,
            doi=hit.doi,
            zotero_key=hit.zotero_key,
            abstract=hit.abstract,
            citations=hit.citations,
            venue=hit.venue,
            found_in=hit.found_in,
            in_zotero=hit.in_zotero,
            has_oa_pdf=hit.has_oa_pdf,
            s2_id=hit.s2_id,
            url=hit.url,
            work_type=hit.work_type,
            container_title=hit.container_title,
            scite=hit.scite,
            score=hit.semantic_similarity,
            semantic_zotero_score=hit.semantic_zotero_score,
            scite_adjust=hit.scite_adjust,
            primo_proxy_url=hit.primo_proxy_url,
            primo_oa_url=hit.primo_oa_url,
        )
        for hit in raw
    ]
    return SearchResponse(results=results, query=q)


# ---------------------------------------------------------------------------
# GET /api/semantic
# ---------------------------------------------------------------------------

@router.get("/api/semantic", dependencies=[AuthRequired])
async def semantic_search(
    q: str = Query(..., min_length=1),
    k: int = Query(10, ge=1, le=50),
    library_id: str | None = None,
) -> list[SemanticHit]:
    from ..semantic_index import SemanticIndexUnavailable
    try:
        return await core_semantic.semantic_search_zotero(query=q, k=k)
    except SemanticIndexUnavailable as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/paper
# ---------------------------------------------------------------------------

@router.get("/api/paper", dependencies=[AuthRequired])
async def get_paper(doi: str = Query(..., min_length=1)) -> PaperInfo:
    return await core_paper.get_paper(doi)
