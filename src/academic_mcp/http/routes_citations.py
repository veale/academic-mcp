from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core import citations as core_citations
from ..core import search as core_search
from ..core.types import CitationsResult, CitationTreeResult
from .app import AuthRequired
from .routes_search import SearchResponse, SearchResult

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
) -> SearchResponse:
    if direction == "in":
        citations_result = await core_citations.get_citations(doi, keywords=q, limit=limit)
    else:
        citations_result = await core_citations.get_references(doi, keywords=q, limit=limit)

    ranked = core_search.search_in_corpus(q, citations_result.items, limit=limit)
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
            scite=None,
            score=hit.semantic_similarity,
        )
        for hit in ranked
    ]
    return SearchResponse(results=results, query=q)
