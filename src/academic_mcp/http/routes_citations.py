from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core import citations as core_citations
from ..core.types import CitationsResult, CitationTreeResult
from .app import AuthRequired

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/citations   ?doi&direction=in|out|tree&limit
# ---------------------------------------------------------------------------

@router.get("/api/citations", dependencies=[AuthRequired])
async def get_citations(
    doi: str = Query(..., min_length=1),
    direction: str = Query("out", pattern="^(in|out|tree)$"),
    limit: int = Query(25, ge=1, le=50),
):
    if direction == "in":
        return await core_citations.get_citations(doi, limit=limit)
    if direction == "out":
        return await core_citations.get_references(doi, limit=limit)
    return await core_citations.get_citation_tree(doi, limit=limit)


# ---------------------------------------------------------------------------
# GET /api/citations/search   ?doi&q&direction=in|out
# ---------------------------------------------------------------------------

@router.get("/api/citations/search", dependencies=[AuthRequired])
async def search_citations(
    doi: str = Query(..., min_length=1),
    q: str = Query(..., min_length=1),
    direction: str = Query("out", pattern="^(in|out)$"),
    limit: int = Query(25, ge=1, le=50),
) -> CitationsResult:
    if direction == "in":
        return await core_citations.get_citations(doi, keywords=q, limit=limit)
    return await core_citations.get_references(doi, keywords=q, limit=limit)
