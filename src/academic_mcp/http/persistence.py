"""Saved-search persistence layer backed by a single SQLite database.

The database lives at ``WEBAPP_DB`` (env) or ``/var/cache/academic-mcp/webapp.sqlite``
by default, with a fallback to ``~/.cache/academic-mcp/webapp.sqlite`` when the
production path is not writable (e.g. dev on macOS).

Public surface:
    init_db()                               — called once at app startup
    save_search(query, params) -> SavedSearch
    list_saved_searches()      -> list[SavedSearch]
    delete_saved_search(id)    -> bool
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

_PREFERRED_DB = Path("/var/cache/academic-mcp/webapp.sqlite")
_FALLBACK_DB = Path("~/.cache/academic-mcp/webapp.sqlite").expanduser()


def _db_path() -> Path:
    raw = os.getenv("WEBAPP_DB", "")
    if raw:
        return Path(raw).expanduser()
    # Try the production path first; fall back to user-local if not writable.
    try:
        _PREFERRED_DB.parent.mkdir(parents=True, exist_ok=True)
        # Touch to verify write access without clobbering an existing DB.
        if not _PREFERRED_DB.exists():
            _PREFERRED_DB.touch()
        return _PREFERRED_DB
    except (PermissionError, OSError):
        logger.info(
            "Cannot write to %s — using fallback DB at %s",
            _PREFERRED_DB,
            _FALLBACK_DB,
        )
        return _FALLBACK_DB


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class SavedSearch(BaseModel):
    id: int
    query: str
    params: dict
    created_at: str  # ISO-8601 UTC


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    query      TEXT    NOT NULL,
    params     TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create the database and apply schema.  Safe to call multiple times."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def save_search(query: str, params: dict | None = None) -> SavedSearch:
    """Persist a search and return the saved row."""
    params = params or {}
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            "INSERT INTO saved_searches (query, params, created_at) VALUES (?, ?, ?)",
            (query, json.dumps(params), now),
        )
        await db.commit()
        row_id = cursor.lastrowid
    return SavedSearch(id=row_id, query=query, params=params, created_at=now)


async def list_saved_searches() -> list[SavedSearch]:
    """Return all saved searches, newest first."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, query, params, created_at FROM saved_searches ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
    return [
        SavedSearch(
            id=row["id"],
            query=row["query"],
            params=json.loads(row["params"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


async def delete_saved_search(search_id: int) -> bool:
    """Delete a saved search by id.  Returns True if a row was deleted."""
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            "DELETE FROM saved_searches WHERE id = ?", (search_id,)
        )
        await db.commit()
        return cursor.rowcount > 0
