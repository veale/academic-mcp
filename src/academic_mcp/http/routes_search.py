from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..core import search as core_search
from ..core import semantic as core_semantic
from ..core import paper as core_paper
from ..core.types import SearchHit, SemanticHit, PaperInfo
from .app import AuthRequired

router = APIRouter()


class SearchResponse(BaseModel):
    results: list[SearchHit]
    query: str


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------

@router.get("/api/search", dependencies=[AuthRequired])
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=20),
    zotero_only: bool = False,
    exclude_local: bool = False,
    semantic: bool | None = None,
    include_scite: bool = False,
    domain_hint: str = "general",
    start_year: int | None = Query(None, ge=1000),
    end_year: int | None = Query(None, ge=1000),
) -> SearchResponse:
    source = "zotero" if zotero_only else "all"
    results = await core_search.search_papers(
        query=q,
        limit=limit,
        source=source,
        domain_hint=domain_hint,
        include_scite=include_scite,
        semantic=semantic,
        start_year=start_year,
        end_year=end_year,
        exclude_local=exclude_local,
    )
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
