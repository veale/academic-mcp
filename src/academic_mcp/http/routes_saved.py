from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .app import AuthRequired
from .persistence import SavedSearch, delete_saved_search, list_saved_searches, save_search

router = APIRouter()


class SaveSearchRequest(BaseModel):
    query: str
    params: dict = {}


@router.get("/api/saved-searches", dependencies=[AuthRequired])
async def get_saved_searches() -> list[SavedSearch]:
    return await list_saved_searches()


@router.post("/api/saved-searches", dependencies=[AuthRequired])
async def create_saved_search(body: SaveSearchRequest) -> SavedSearch:
    return await save_search(body.query, body.params)


@router.delete("/api/saved-searches/{search_id}", dependencies=[AuthRequired])
async def remove_saved_search(search_id: int) -> dict:
    deleted = await delete_saved_search(search_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}
