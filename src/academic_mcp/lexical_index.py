"""BM25-ranked lexical index over the Zotero library (SQLite FTS5).

Zotero's own schema stores field values in a generic ``itemDataValues`` table,
so keyword search there means ``LOWER(value) LIKE '%term%'`` — a full scan per
term, per field, with no relevance ranking. This module keeps an FTS5 mirror
in the cache directory, synced incrementally from ``zotero.sqlite`` in exactly
the way the semantic index is (skip anything whose ``dateModified`` is
unchanged), and answers queries with BM25 scores.

Layout::

    ~/.cache/academic-mcp/zotero-fts.sqlite
        items_fts   FTS5: title, authors, abstract, venue, tags, fulltext
        fts_meta    item_key -> (rowid in items_fts, dateModified)
        fts_state   sync bookkeeping

Full text comes from each item's ``.zotero-ft-cache``, capped at
``FTS_FULLTEXT_MAX_CHARS`` per item — the index is roughly as large as the
text it holds, so that setting is the disk-size knob. Set it to 0 to index
metadata only.

Query syntax: bare terms are AND-ed. ``author:``/``creator:`` map onto the
authors column, ``title:``, ``venue:``, ``tag:`` and ``fulltext:`` onto theirs.
Double-quoted spans are passed through to FTS5 as phrase queries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import zotero_sqlite
from .config import config

logger = logging.getLogger(__name__)


class LexicalIndexUnavailable(RuntimeError):
    """Raised when FTS5 is missing or the Zotero SQLite backend is absent."""


# Columns in declaration order — bm25() takes one weight per column, and the
# weights below encode "a title match beats an abstract match beats a body hit".
_COLUMNS = ("title", "authors", "abstract", "venue", "tags", "fulltext")
_BM25_WEIGHTS = (8.0, 3.0, 4.0, 2.0, 3.0, 1.0)

_FIELD_ALIASES = {
    "author": "authors",
    "creator": "authors",
    "authors": "authors",
    "title": "title",
    "abstract": "abstract",
    "venue": "venue",
    "journal": "venue",
    "publication": "venue",
    "tag": "tags",
    "tags": "tags",
    "fulltext": "fulltext",
    "text": "fulltext",
}

# FTS5 treats these as syntax. Bare user terms are quoted, so they only need
# stripping inside a term (e.g. a stray '*' or ':').
_FTS_SPECIALS = re.compile(r'[":()*^]')
_TOKEN_RE = re.compile(r'"[^"]*"|\S+')

_SYNC_BATCH = 200


def _db_path() -> Path:
    return config.pdf_cache_dir.parent / "zotero-fts.sqlite"


def _fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _connect(readonly: bool = False) -> sqlite3.Connection:
    path = _db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cols = ", ".join(_COLUMNS)
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
            {cols},
            tokenize = 'porter unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fts_meta (
            item_key      TEXT PRIMARY KEY,
            fts_rowid     INTEGER NOT NULL,
            date_modified TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # Search joins fts_meta on fts_rowid to recover item_key. Without this
    # index that join scans fts_meta once per matched row, and a common term
    # matching thousands of documents ends up slower than the LIKE scan it
    # replaced.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS fts_meta_rowid ON fts_meta(fts_rowid)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS fts_state (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()


# ---------------------------------------------------------------------------
# Full text
# ---------------------------------------------------------------------------

def _read_fulltext(attachment_key: str) -> str:
    """Up to ``config.fts_fulltext_max_chars`` of an attachment's ft-cache."""
    cap = config.fts_fulltext_max_chars
    if cap <= 0 or not attachment_key:
        return ""
    base = zotero_sqlite.sqlite_config.storage_path
    if not base:
        return ""
    path = Path(base) / attachment_key / ".zotero-ft-cache"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

def build_match_query(query: str) -> str:
    """Translate a user query into an FTS5 MATCH expression.

    Bare terms are quoted (so punctuation can't be read as FTS5 operators) and
    AND-ed together. ``field:value`` prefixes become column filters.
    """
    parts: list[str] = []
    for raw in _TOKEN_RE.findall(query or ""):
        token = raw.strip()
        if not token:
            continue

        if token.startswith('"') and token.endswith('"') and len(token) > 1:
            phrase = _FTS_SPECIALS.sub(" ", token.strip('"')).strip()
            if phrase:
                parts.append(f'"{phrase}"')
            continue

        column: str | None = None
        if ":" in token:
            prefix, _, rest = token.partition(":")
            mapped = _FIELD_ALIASES.get(prefix.lower())
            if mapped and rest.strip():
                column, token = mapped, rest

        term = _FTS_SPECIALS.sub(" ", token).strip()
        if not term:
            continue
        quoted = f'"{term}"'
        parts.append(f"{column}:{quoted}" if column else quoted)

    return " AND ".join(parts)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _search_blocking(query: str, limit: int) -> list[tuple[str, float]]:
    match = build_match_query(query)
    if not match:
        return []

    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    sql = f"""
        SELECT m.item_key AS item_key,
               bm25(items_fts, {weights}) AS score
        FROM items_fts
        JOIN fts_meta m ON m.fts_rowid = items_fts.rowid
        WHERE items_fts MATCH ?
        ORDER BY score
        LIMIT ?
    """

    conn = _connect(readonly=True)
    try:
        try:
            rows = conn.execute(sql, (match, limit)).fetchall()
        except sqlite3.OperationalError as e:
            # A malformed MATCH expression (unbalanced quote, lone operator).
            # Retry with every token quoted and AND-ed, which cannot be invalid.
            logger.debug("FTS match %r rejected (%s); retrying literally", match, e)
            fallback = " AND ".join(
                f'"{t}"'
                for t in (_FTS_SPECIALS.sub(" ", w).strip() for w in (query or "").split())
                if t
            )
            if not fallback:
                return []
            rows = conn.execute(sql, (fallback, limit)).fetchall()
        # bm25() is negative, most-relevant most-negative. Flip it so callers
        # get the usual "bigger is better" convention.
        return [(r["item_key"], -float(r["score"])) for r in rows]
    finally:
        conn.close()


async def search(query: str, limit: int = 20) -> list[tuple[str, float]]:
    """Return ``[(item_key, bm25_score)]``, best first. Empty if unavailable."""
    if not available():
        return []
    try:
        return await asyncio.to_thread(_search_blocking, query, limit)
    except Exception as e:
        logger.warning("Lexical index search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Status / availability
# ---------------------------------------------------------------------------

def available() -> bool:
    """True when the index exists, is enabled, and holds at least one item."""
    if not config.lexical_index_enabled or not _db_path().exists():
        return False
    try:
        conn = _connect(readonly=True)
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM fts_meta").fetchone()
            return bool(row and row["c"])
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _status_blocking() -> dict[str, Any]:
    status: dict[str, Any] = {
        "enabled": config.lexical_index_enabled,
        "fts5": _fts5_available(),
        "db_path": str(_db_path()),
        "exists": _db_path().exists(),
        "count": 0,
        "fulltext_max_chars": config.fts_fulltext_max_chars,
    }
    if not status["exists"]:
        return status
    try:
        conn = _connect(readonly=True)
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM fts_meta").fetchone()
            status["count"] = int(row["c"]) if row else 0
            for r in conn.execute("SELECT k, v FROM fts_state"):
                status[r["k"]] = r["v"]
        finally:
            conn.close()
    except sqlite3.Error as e:
        status["error"] = str(e)
    return status


async def status() -> dict[str, Any]:
    return await asyncio.to_thread(_status_blocking)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _document(item: dict) -> tuple[str, ...]:
    return (
        item.get("title") or "",
        " ".join(item.get("authors") or []),
        item.get("abstract") or "",
        item.get("venue") or "",
        " ".join(item.get("tags") or []),
        _read_fulltext(item.get("attachment_key") or ""),
    )


def _sync_blocking(items: list[dict], force_rebuild: bool) -> dict[str, Any]:
    started = time.monotonic()
    conn = _connect()
    try:
        _ensure_schema(conn)

        if force_rebuild:
            conn.execute("DELETE FROM items_fts")
            conn.execute("DELETE FROM fts_meta")
            conn.commit()
            prior: dict[str, tuple[int, str]] = {}
        else:
            prior = {
                r["item_key"]: (r["fts_rowid"], r["date_modified"])
                for r in conn.execute(
                    "SELECT item_key, fts_rowid, date_modified FROM fts_meta"
                )
            }

        seen: set[str] = set()
        indexed = 0
        deleted = 0
        placeholders = ", ".join("?" * len(_COLUMNS))
        insert_sql = f"INSERT INTO items_fts ({', '.join(_COLUMNS)}) VALUES ({placeholders})"

        for n, item in enumerate(items, 1):
            item_key = item.get("item_key") or ""
            if not item_key:
                continue
            seen.add(item_key)

            date_mod = item.get("dateModified") or ""
            existing = prior.get(item_key)
            if existing is not None and existing[1] == date_mod:
                continue
            if existing is not None:
                conn.execute("DELETE FROM items_fts WHERE rowid = ?", (existing[0],))
                deleted += 1

            cur = conn.execute(insert_sql, _document(item))
            conn.execute(
                "INSERT OR REPLACE INTO fts_meta (item_key, fts_rowid, date_modified) "
                "VALUES (?, ?, ?)",
                (item_key, cur.lastrowid, date_mod),
            )
            indexed += 1
            if n % _SYNC_BATCH == 0:
                conn.commit()

        # Items that vanished from the library.
        stale = [k for k in prior if k not in seen]
        for item_key in stale:
            conn.execute("DELETE FROM items_fts WHERE rowid = ?", (prior[item_key][0],))
            conn.execute("DELETE FROM fts_meta WHERE item_key = ?", (item_key,))
            deleted += 1

        conn.commit()

        count = conn.execute("SELECT COUNT(*) AS c FROM fts_meta").fetchone()["c"]
        result = {
            "count": int(count),
            "indexed": indexed,
            "deleted": deleted,
            "stale_removed": len(stale),
            "elapsed_sec": round(time.monotonic() - started, 2),
            "last_sync": datetime.now(timezone.utc).isoformat(),
        }
        conn.executemany(
            "INSERT OR REPLACE INTO fts_state (k, v) VALUES (?, ?)",
            [(k, json.dumps(v) if not isinstance(v, str) else v)
             for k, v in result.items()],
        )
        conn.commit()
        return result
    finally:
        conn.close()


async def sync(force_rebuild: bool = False) -> dict[str, Any]:
    """Bring the FTS index in line with the Zotero library.

    Incremental by default: an item is re-indexed only when its
    ``dateModified`` differs from what the mirror recorded.
    """
    if not config.lexical_index_enabled:
        raise LexicalIndexUnavailable("Lexical index disabled (LEXICAL_INDEX_ENABLED=false)")
    if not _fts5_available():
        raise LexicalIndexUnavailable("This SQLite build has no FTS5 support")
    if not zotero_sqlite.sqlite_config.available:
        raise LexicalIndexUnavailable("Zotero SQLite backend is not available")

    items = await zotero_sqlite.list_items_for_lexical_index()
    result = await asyncio.to_thread(_sync_blocking, items, force_rebuild)
    logger.info(
        "Lexical index sync: %d indexed, %d deleted, %d total (%.2fs)",
        result["indexed"], result["deleted"], result["count"], result["elapsed_sec"],
    )
    return result
