from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..core import search as core_search
from ..core import semantic as core_semantic
from ..core import paper as core_paper
from ..core.types import SemanticHit, PaperInfo
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
    scite: dict[str, Any] | None = None
    score: float | None = None


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
            title=r.get("title") or "",
            authors=r.get("authors") or [],
            year=str(r["year"]) if r.get("year") else None,
            doi=r.get("doi"),
            zotero_key=r.get("zotero_key"),
            abstract=r.get("abstract"),
            citations=r.get("citations"),
            venue=r.get("venue"),
            found_in=r.get("found_in") or [],
            in_zotero=bool(r.get("in_zotero")),
            has_oa_pdf=bool(r.get("has_oa_pdf")),
            s2_id=r.get("s2_id"),
            url=r.get("url"),
            scite=r.get("scite"),
            score=r.get("_semantic_similarity"),
        )
        for r in raw
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
