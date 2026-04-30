from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..core import libraries as core_libraries
from ..core import search as core_search
from ..core.types import ZoteroIndexRefresh
from .app import AuthRequired

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class ZoteroSearchResult(BaseModel):
    title: str | None = None
    key: str | None = None
    doi: str | None = None
    authors: list[str] = []
    year: str | None = None
    venue: str | None = None
    abstract: str | None = None
    item_type: str | None = None
    url: str | None = None


class DeeplinkResponse(BaseModel):
    zotero_key: str
    select_url: str
    open_pdf_url: str


# ---------------------------------------------------------------------------
# GET /api/zotero/libraries
# ---------------------------------------------------------------------------

@router.get("/api/zotero/libraries", dependencies=[AuthRequired])
async def list_libraries():
    return await core_libraries.list_libraries()


# ---------------------------------------------------------------------------
# GET /api/zotero/search
# ---------------------------------------------------------------------------

@router.get("/api/zotero/search", dependencies=[AuthRequired])
async def search_zotero(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
) -> list[ZoteroSearchResult]:
    raw = await core_search.search_zotero(query=q, limit=limit)
    results: list[ZoteroSearchResult] = []
    for item in raw:
        creators = item.get("creators") or []
        authors = []
        for c in creators if isinstance(creators, list) else []:
            if isinstance(c, dict):
                name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                if name:
                    authors.append(name)
        results.append(ZoteroSearchResult(
            title=item.get("title"),
            key=item.get("key"),
            doi=(item.get("DOI") or "").strip() or None,
            authors=authors,
            year=(item.get("date") or "")[:4] or None,
            venue=item.get("publicationTitle"),
            abstract=(item.get("abstractNote") or "").strip() or None,
            item_type=item.get("itemType"),
            url=(item.get("url") or "").strip() or None,
        ))
    return results


# ---------------------------------------------------------------------------
# GET /api/zotero/deeplink
# ---------------------------------------------------------------------------

@router.get("/api/zotero/deeplink", dependencies=[AuthRequired])
async def deeplink(zotero_key: str = Query(..., min_length=1)) -> DeeplinkResponse:
    return DeeplinkResponse(
        zotero_key=zotero_key,
        select_url=f"zotero://select/library/items/{zotero_key}",
        open_pdf_url=f"zotero://open-pdf/library/items/{zotero_key}",
    )
