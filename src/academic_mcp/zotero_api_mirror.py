"""Maintain the server's copy of the Zotero libraries from the Zotero Web API.

The rest of the server reads Zotero's own SQLite layout (``items``,
``itemData``, ``itemAttachments`` ...) and the ``.zotero-ft-cache`` text files
under ``storage/``. This module builds and updates exactly that, from
the Web API, so no desktop machine has to push a copy:

* **Incremental sync** uses Zotero's library versions: the keys changed since
  the last synced version (``format=versions``), then those items in batches,
  then ``/deleted`` for removals, then ``/fulltext`` for extracted text.
* **Reconciliation** (daily, and on demand) compares every key and version in
  each library with the local copy and repairs any difference, so a missed
  update cannot leave the copy permanently wrong.
* **Seeding**: a first build can start from an existing desktop snapshot
  (``ZOTERO_API_MIRROR_SEED``); only what changed since it is downloaded,
  and unchanged rows stay byte-identical (no needless re-embedding).

Values are written as Zotero desktop writes them (``YYYY-MM-DD HH:MM:SS``
timestamps, multipart ``date`` values) so search, sorting and the index
signatures behave as they did with the desktop snapshot.

Enable with ``ZOTERO_API_MIRROR=true``; the database is written at
``ZOTERO_SQLITE_PATH`` and full text under ``ZOTERO_LOCAL_STORAGE``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API = "https://api.zotero.org"
MARKER = ".api-mirror"
_BATCH = 50
_RECONCILE_SECONDS = 24 * 3600
_lock: asyncio.Lock | None = None
_write_lock = threading.Lock()
_last_result: dict = {}

_LINK_MODES = {"imported_file": 0, "imported_url": 1, "linked_file": 2, "linked_url": 3, "embedded_image": 4}
# Item JSON keys that are not Zotero fields.
_NON_FIELDS = frozenset("""
    key version itemType creators tags collections relations dateAdded dateModified
    deleted parentItem linkMode contentType charset filename md5 mtime path note
    annotationType annotationText annotationComment annotationColor annotationPageLabel
    annotationSortIndex annotationPosition annotationAuthorName annotationIsExternal
    inPublications lastRead
""".split())
# Stored as SQL datetimes rather than multipart dates.
_DATETIME_FIELDS = frozenset({"accessDate"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL,
    editable INT NOT NULL, filesEditable INT NOT NULL, version INT NOT NULL DEFAULT 0,
    storageVersion INT NOT NULL DEFAULT 0, lastSync INT NOT NULL DEFAULT 0,
    archived INT NOT NULL DEFAULT 0, isAdmin INT NOT NULL DEFAULT 0, clientVersion INT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS groups (groupID INTEGER PRIMARY KEY, libraryID INT NOT NULL UNIQUE,
    name TEXT NOT NULL, description TEXT NOT NULL, version INT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (setting TEXT, key TEXT, value, PRIMARY KEY (setting, key));
CREATE TABLE IF NOT EXISTS items (itemID INTEGER PRIMARY KEY, itemTypeID INT NOT NULL,
    dateAdded TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    libraryID INT NOT NULL, key TEXT NOT NULL, version INT NOT NULL DEFAULT 0,
    synced INT NOT NULL DEFAULT 0, clientVersion INT NOT NULL DEFAULT 0, UNIQUE (libraryID, key));
CREATE TABLE IF NOT EXISTS itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT,
    templateItemTypeID INT, display INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT, fieldFormatID INT);
CREATE TABLE IF NOT EXISTS itemData (itemID INT, fieldID INT, valueID, PRIMARY KEY (itemID, fieldID));
CREATE TABLE IF NOT EXISTS itemDataValues (valueID INTEGER PRIMARY KEY, value UNIQUE, valueNormalized TEXT);
CREATE TABLE IF NOT EXISTS creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT,
    fieldMode INT, firstNameNormalized TEXT, lastNameNormalized TEXT, UNIQUE (lastName, firstName, fieldMode));
CREATE TABLE IF NOT EXISTS creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
CREATE TABLE IF NOT EXISTS itemCreators (itemID INT NOT NULL, creatorID INT NOT NULL,
    creatorTypeID INT NOT NULL DEFAULT 1, orderIndex INT NOT NULL DEFAULT 0,
    PRIMARY KEY (itemID, creatorID, creatorTypeID, orderIndex), UNIQUE (itemID, orderIndex));
CREATE TABLE IF NOT EXISTS tags (tagID INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, nameNormalized TEXT);
CREATE TABLE IF NOT EXISTS itemTags (itemID INT NOT NULL, tagID INT NOT NULL, type INT NOT NULL,
    PRIMARY KEY (itemID, tagID));
CREATE TABLE IF NOT EXISTS itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT,
    contentType TEXT, charsetID INT, path TEXT, syncState INT DEFAULT 0, storageModTime INT,
    storageHash TEXT, lastProcessedModificationTime INT, lastRead INT);
CREATE TABLE IF NOT EXISTS itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INT, note TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS deletedItems (itemID INTEGER PRIMARY KEY, dateDeleted DEFAULT CURRENT_TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT NOT NULL,
    parentCollectionID INT DEFAULT NULL, clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    libraryID INT NOT NULL, key TEXT NOT NULL, version INT NOT NULL DEFAULT 0, synced INT NOT NULL DEFAULT 0,
    clientVersion INT NOT NULL DEFAULT 0, UNIQUE (libraryID, key));
CREATE TABLE IF NOT EXISTS collectionItems (collectionID INT NOT NULL, itemID INT NOT NULL,
    orderIndex INT NOT NULL DEFAULT 0, PRIMARY KEY (collectionID, itemID));
CREATE TABLE IF NOT EXISTS fulltextItems (itemID INTEGER PRIMARY KEY, indexedPages INT, totalPages INT,
    indexedChars INT, totalChars INT, version INT NOT NULL DEFAULT 0, synced INT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fulltextWords (wordID INTEGER PRIMARY KEY, word TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS fulltextItemWords (wordID INT, itemID INT, PRIMARY KEY (wordID, itemID));
CREATE INDEX IF NOT EXISTS itemData_fieldID ON itemData(fieldID);
CREATE INDEX IF NOT EXISTS itemAttachments_parentItemID ON itemAttachments(parentItemID);
CREATE INDEX IF NOT EXISTS itemCreators_creatorID ON itemCreators(creatorID);
CREATE INDEX IF NOT EXISTS itemTags_tagID ON itemTags(tagID);
-- Sync bookkeeping (not part of Zotero's schema).
CREATE TABLE IF NOT EXISTS api_libraries (prefix TEXT PRIMARY KEY, libraryID INT NOT NULL UNIQUE,
    version INT NOT NULL DEFAULT 0, fulltext_version INT NOT NULL DEFAULT 0,
    reconciled_at REAL NOT NULL DEFAULT 0, synced_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS api_pending_parents (itemID INTEGER PRIMARY KEY, parentKey TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def enabled() -> bool:
    from .zotero import zot_config

    return (
        os.getenv("ZOTERO_API_MIRROR", "false").lower() in ("1", "true", "yes")
        and bool(zot_config.api_key and zot_config.user_id)
    )


def _paths() -> tuple[Path, Path]:
    from .zotero_sqlite import sqlite_config

    return Path(sqlite_config.db_path), Path(sqlite_config.storage_path)


def is_api_mirror(db_path: str | Path) -> bool:
    return (Path(db_path).parent / MARKER).is_file()


# ---------------------------------------------------------------------------
# Value conversion (as Zotero desktop stores them)
# ---------------------------------------------------------------------------

def sql_datetime(value: str) -> str:
    """'2025-04-15T13:32:25Z' -> '2025-04-15 13:32:25'."""
    value = (value or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", value)
    return f"{match.group(1)} {match.group(2)}" if match else value


def multipart_date(value: str, parsed: str = "") -> str:
    """Zotero's stored form of a date field: 'YYYY-MM-DD original'.

    *parsed* is the API's ``meta.parsedDate`` ('2012', '2012-03', '2012-03-04').
    Unknown parts are '00'; an unparseable date gets '0000-00-00'.
    """
    value = value or ""
    if not value:
        return ""
    numeric = re.fullmatch(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\s*", value)
    if numeric and os.getenv("ZOTERO_DATE_ORDER", "mdy").lower() == "dmy":
        # The API parses '06/12/2019' month-first; a desktop in a day-first
        # locale (UK) stores 6 December. Match the desktop.
        day, month, year = numeric.groups()
        if int(month) <= 12 and int(day) <= 31:
            return f"{year}-{month.zfill(2)}-{day.zfill(2)} {value}"
    parts = (parsed or "").split("-")
    if not parts or not re.fullmatch(r"\d{4}", parts[0] or ""):
        match = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", value)
        parts = [match.group(1)] if match else []
    if not parts:
        return f"0000-00-00 {value}"
    year = parts[0]
    month = parts[1] if len(parts) > 1 and parts[1] else "00"
    day = parts[2] if len(parts) > 2 and parts[2] else "00"
    return f"{year}-{month.zfill(2)}-{day.zfill(2)} {value}"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _connect(path: Path) -> sqlite3.Connection:
    # Used from worker threads one at a time (writes are serialised by
    # _write_lock), never concurrently.
    db = sqlite3.connect(path, timeout=60, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=OFF")
    return db


def _prepare(path: Path, seed: Path | None, user_id: str) -> sqlite3.Connection:
    """Open the mirror database, creating it (from *seed* when given)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / MARKER).write_text("Built from the Zotero Web API by academic-mcp.\n")
    # A desktop-snapshot watermark would make readers open this live
    # database as immutable.
    (path.parent / ".last-sync").unlink(missing_ok=True)
    fresh = not path.exists()
    if fresh and seed and seed.is_file():
        logger.info("Seeding Zotero API mirror from snapshot %s", seed)
        source = sqlite3.connect(f"file:{seed}?mode=ro&immutable=1", uri=True)
        target = sqlite3.connect(path)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
    db = _connect(path)
    # Desktop-only consistency triggers; this copy is written only by sync.
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        db.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    db.executescript(SCHEMA)
    db.execute("INSERT OR REPLACE INTO settings(setting, key, value) VALUES('account', 'userID', ?)", (int(user_id),))
    if not db.execute("SELECT 1 FROM api_libraries LIMIT 1").fetchone():
        # Adopt the libraries a seed snapshot already holds, at its versions.
        for row in db.execute(
            "SELECT l.libraryID, l.type, l.version, g.groupID FROM libraries l "
            "LEFT JOIN groups g ON g.libraryID = l.libraryID"
        ).fetchall():
            prefix = f"/users/{user_id}" if row["type"] == "user" else (
                f"/groups/{row['groupID']}" if row["groupID"] else None)
            if prefix:
                db.execute(
                    "INSERT OR IGNORE INTO api_libraries(prefix, libraryID, version, fulltext_version) VALUES(?,?,?,?)",
                    (prefix, row["libraryID"], row["version"], row["version"]),
                )
    return db


def _seed_schema(db: sqlite3.Connection, schema: dict) -> None:
    """Item types, fields and creator types from the API's /schema."""
    for entry in schema.get("itemTypes") or []:
        _type_id(db, entry["itemType"])
        for field in entry.get("fields") or []:
            _field_id(db, field["field"])
        for creator in entry.get("creatorTypes") or []:
            _creator_type_id(db, creator["creatorType"])
    for name in ("attachment", "note", "annotation"):
        _type_id(db, name)


def _lookup_or_insert(db, table, id_col, name_col, name):
    row = db.execute(f"SELECT {id_col} FROM {table} WHERE {name_col}=?", (name,)).fetchone()
    if row:
        return row[0]
    return db.execute(f"INSERT INTO {table}({name_col}) VALUES(?)", (name,)).lastrowid


def _type_id(db, name):
    return _lookup_or_insert(db, "itemTypes", "itemTypeID", "typeName", name)


def _field_id(db, name):
    return _lookup_or_insert(db, "fields", "fieldID", "fieldName", name)


def _creator_type_id(db, name):
    return _lookup_or_insert(db, "creatorTypes", "creatorTypeID", "creatorType", name)


def _value_id(db, value):
    row = db.execute("SELECT valueID FROM itemDataValues WHERE value=?", (value,)).fetchone()
    if row:
        return row[0]
    return db.execute("INSERT INTO itemDataValues(value) VALUES(?)", (value,)).lastrowid


def _creator_id(db, first, last, mode):
    row = db.execute(
        "SELECT creatorID FROM creators WHERE lastName=? AND firstName=? AND fieldMode=?",
        (last, first, mode),
    ).fetchone()
    if row:
        return row[0]
    return db.execute(
        "INSERT INTO creators(firstName, lastName, fieldMode) VALUES(?,?,?)", (first, last, mode)
    ).lastrowid


def _tag_id(db, name):
    return _lookup_or_insert(db, "tags", "tagID", "name", name)


def _clear_item_rows(db, item_id):
    for table in ("itemData", "itemCreators", "itemTags", "collectionItems", "deletedItems",
                  "itemAttachments", "itemNotes", "api_pending_parents"):
        db.execute(f"DELETE FROM {table} WHERE itemID=?", (item_id,))


def _delete_item(db, library_id, key):
    row = db.execute("SELECT itemID FROM items WHERE libraryID=? AND key=?", (library_id, key)).fetchone()
    if not row:
        return
    item_id = row[0]
    _clear_item_rows(db, item_id)
    db.execute("DELETE FROM fulltextItems WHERE itemID=?", (item_id,))
    db.execute("DELETE FROM items WHERE itemID=?", (item_id,))


def apply_items(db: sqlite3.Connection, library_id: int, items: list[dict]) -> None:
    """Write API item JSON into the Zotero tables, replacing earlier rows."""
    for item in items:
        data = item.get("data") or {}
        key = item.get("key") or data.get("key")
        if not key:
            continue
        version = int(item.get("version") or data.get("version") or 0)
        type_id = _type_id(db, data.get("itemType") or "document")
        added = sql_datetime(data.get("dateAdded") or "")
        modified = sql_datetime(data.get("dateModified") or "") or added
        row = db.execute("SELECT itemID FROM items WHERE libraryID=? AND key=?", (library_id, key)).fetchone()
        if row:
            item_id = row[0]
            db.execute(
                "UPDATE items SET itemTypeID=?, dateAdded=?, dateModified=?, clientDateModified=?, "
                "version=?, synced=1 WHERE itemID=?",
                (type_id, added, modified, modified, version, item_id),
            )
            _clear_item_rows(db, item_id)
        else:
            item_id = db.execute(
                "INSERT INTO items(itemTypeID, dateAdded, dateModified, clientDateModified, libraryID, key, "
                "version, synced) VALUES(?,?,?,?,?,?,?,1)",
                (type_id, added, modified, modified, library_id, key, version),
            ).lastrowid
        parsed = (item.get("meta") or {}).get("parsedDate") or ""
        for name, value in data.items():
            if name in _NON_FIELDS or not isinstance(value, (str, int, float)) or value == "":
                continue
            value = str(value)
            if name in _DATETIME_FIELDS:
                value = sql_datetime(value)
            elif name in ("date", "dateDecided", "dateEnacted", "filingDate", "issueDate"):
                value = multipart_date(value, parsed)
            db.execute(
                "INSERT OR REPLACE INTO itemData(itemID, fieldID, valueID) VALUES(?,?,?)",
                (item_id, _field_id(db, name), _value_id(db, value)),
            )
        for index, creator in enumerate(data.get("creators") or []):
            if "name" in creator:
                first, last, mode = "", creator.get("name") or "", 1
            else:
                first, last, mode = creator.get("firstName") or "", creator.get("lastName") or "", 0
            if not first and not last:
                continue
            db.execute(
                "INSERT OR REPLACE INTO itemCreators(itemID, creatorID, creatorTypeID, orderIndex) VALUES(?,?,?,?)",
                (item_id, _creator_id(db, first, last, mode),
                 _creator_type_id(db, creator.get("creatorType") or "author"), index),
            )
        for tag in data.get("tags") or []:
            name = (tag.get("tag") or "").strip()
            if name:
                db.execute(
                    "INSERT OR IGNORE INTO itemTags(itemID, tagID, type) VALUES(?,?,?)",
                    (item_id, _tag_id(db, name), int(tag.get("type") or 0)),
                )
        for collection_key in data.get("collections") or []:
            found = db.execute(
                "SELECT collectionID FROM collections WHERE libraryID=? AND key=?", (library_id, collection_key)
            ).fetchone()
            if found:
                db.execute(
                    "INSERT OR IGNORE INTO collectionItems(collectionID, itemID) VALUES(?,?)", (found[0], item_id)
                )
        if data.get("deleted"):
            db.execute("INSERT OR REPLACE INTO deletedItems(itemID) VALUES(?)", (item_id,))
        parent_key = data.get("parentItem") or ""
        parent_id = None
        if parent_key:
            found = db.execute(
                "SELECT itemID FROM items WHERE libraryID=? AND key=?", (library_id, parent_key)
            ).fetchone()
            parent_id = found[0] if found else None
            if parent_id is None:
                db.execute(
                    "INSERT OR REPLACE INTO api_pending_parents(itemID, parentKey) VALUES(?,?)", (item_id, parent_key)
                )
        item_type = data.get("itemType")
        if item_type == "attachment":
            link_mode = _LINK_MODES.get(data.get("linkMode") or "", 0)
            if link_mode in (0, 1) and data.get("filename"):
                path = "storage:" + data["filename"]
            else:
                path = data.get("path") or None
            db.execute(
                "INSERT OR REPLACE INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path, "
                "storageModTime, storageHash, lastRead) VALUES(?,?,?,?,?,?,?,?)",
                (item_id, parent_id, link_mode, data.get("contentType") or "", path,
                 data.get("mtime"), data.get("md5"), data.get("lastRead")),
            )
        elif item_type == "note":
            db.execute(
                "INSERT OR REPLACE INTO itemNotes(itemID, parentItemID, note, title) VALUES(?,?,?,?)",
                (item_id, parent_id, data.get("note") or "", ""),
            )
    _resolve_parents(db, library_id)


def _resolve_parents(db, library_id):
    for row in db.execute(
        "SELECT p.itemID, i.itemID AS parentID FROM api_pending_parents p "
        "JOIN items c ON c.itemID = p.itemID "
        "JOIN items i ON i.key = p.parentKey AND i.libraryID = c.libraryID "
        "WHERE c.libraryID = ?", (library_id,),
    ).fetchall():
        db.execute("UPDATE itemAttachments SET parentItemID=? WHERE itemID=?", (row["parentID"], row["itemID"]))
        db.execute("UPDATE itemNotes SET parentItemID=? WHERE itemID=?", (row["parentID"], row["itemID"]))
        db.execute("DELETE FROM api_pending_parents WHERE itemID=?", (row["itemID"],))


def apply_collections(db, library_id, collections):
    for collection in collections:
        data = collection.get("data") or {}
        key = collection.get("key") or data.get("key")
        db.execute(
            "INSERT INTO collections(collectionName, libraryID, key, version, synced) VALUES(?,?,?,?,1) "
            "ON CONFLICT(libraryID, key) DO UPDATE SET collectionName=excluded.collectionName, "
            "version=excluded.version",
            (data.get("name") or "", library_id, key, int(collection.get("version") or 0)),
        )
    for collection in collections:
        data = collection.get("data") or {}
        parent = data.get("parentCollection") or None
        parent_id = None
        if parent:
            found = db.execute(
                "SELECT collectionID FROM collections WHERE libraryID=? AND key=?", (library_id, parent)
            ).fetchone()
            parent_id = found[0] if found else None
        db.execute(
            "UPDATE collections SET parentCollectionID=? WHERE libraryID=? AND key=?",
            (parent_id, library_id, collection.get("key") or data.get("key")),
        )


def _delete_collection(db, library_id, key):
    row = db.execute("SELECT collectionID FROM collections WHERE libraryID=? AND key=?", (library_id, key)).fetchone()
    if row:
        db.execute("DELETE FROM collectionItems WHERE collectionID=?", (row[0],))
        db.execute("DELETE FROM collections WHERE collectionID=?", (row[0],))


def _ensure_library(db, prefix, user_id, group=None):
    row = db.execute("SELECT libraryID FROM api_libraries WHERE prefix=?", (prefix,)).fetchone()
    if row:
        library_id = row[0]
    else:
        library_id = 1 if prefix.startswith("/users/") else max(
            2, (db.execute("SELECT MAX(libraryID) FROM libraries").fetchone()[0] or 1) + 1)
        db.execute("INSERT INTO api_libraries(prefix, libraryID) VALUES(?,?)", (prefix, library_id))
    exists = db.execute("SELECT 1 FROM libraries WHERE libraryID=?", (library_id,)).fetchone()
    if not exists:
        db.execute(
            "INSERT INTO libraries(libraryID, type, editable, filesEditable) VALUES(?,?,1,1)",
            (library_id, "user" if prefix.startswith("/users/") else "group"),
        )
    if group:
        data = group.get("data") or {}
        db.execute(
            "INSERT INTO groups(groupID, libraryID, name, description, version) VALUES(?,?,?,?,?) "
            "ON CONFLICT(groupID) DO UPDATE SET name=excluded.name, description=excluded.description, "
            "version=excluded.version",
            (int(group["id"]), library_id, data.get("name") or "", data.get("description") or "",
             int(group.get("version") or 0)),
        )
    return library_id


def _drop_library(db, prefix):
    row = db.execute("SELECT libraryID FROM api_libraries WHERE prefix=?", (prefix,)).fetchone()
    if not row:
        return
    library_id = row[0]
    for (key,) in db.execute("SELECT key FROM items WHERE libraryID=?", (library_id,)).fetchall():
        _delete_item(db, library_id, key)
    for (key,) in db.execute("SELECT key FROM collections WHERE libraryID=?", (library_id,)).fetchall():
        _delete_collection(db, library_id, key)
    db.execute("DELETE FROM groups WHERE libraryID=?", (library_id,))
    db.execute("DELETE FROM libraries WHERE libraryID=?", (library_id,))
    db.execute("DELETE FROM api_libraries WHERE prefix=?", (prefix,))


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------

class _Api:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self.client = client
        self.headers = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}

    async def get(self, path, params=None, *, allow=(200,)):
        for attempt in range(6):
            response = await self.client.get(API + path, params=params, headers=self.headers)
            if response.status_code in (429, 503):
                delay = float(response.headers.get("Retry-After") or 5 * (attempt + 1))
                logger.info("Zotero API asked to wait %.0fs (%s)", delay, response.status_code)
                await asyncio.sleep(min(delay, 120))
                continue
            backoff = response.headers.get("Backoff")
            if response.status_code not in allow:
                raise RuntimeError(f"Zotero API {path}: HTTP {response.status_code} {response.text[:200]}")
            if backoff:
                await asyncio.sleep(min(float(backoff), 120))
            return response
        raise RuntimeError(f"Zotero API {path}: still rate-limited")


async def _fetch_by_keys(api, prefix, endpoint, keys, key_param):
    """Objects for *keys*, 50 per request. Zotero takes ~3 s per request,
    so a few run at once (rate-limit responses are honoured in _Api.get)."""
    semaphore = asyncio.Semaphore(int(os.getenv("ZOTERO_API_MIRROR_CONCURRENCY", "4")))

    async def one(chunk):
        async with semaphore:
            response = await api.get(f"{prefix}/{endpoint}", {
                key_param: ",".join(chunk), "format": "json", "includeTrashed": "1", "limit": str(_BATCH),
            })
            return response.json()

    chunks = [keys[start:start + _BATCH] for start in range(0, len(keys), _BATCH)]
    out = []
    for page in await asyncio.gather(*(one(c) for c in chunks)):
        out.extend(page)
    return out


async def _sync_library(api, db_path, storage, prefix, user_id, group=None, reconcile=False, fulltext=True):
    """Bring one library up to date. Returns counts of what changed."""
    def open_db():
        return _connect(db_path)

    db = await asyncio.to_thread(open_db)
    try:
        library_id = await asyncio.to_thread(_ensure_library, db, prefix, user_id, group)
        state = db.execute("SELECT version, fulltext_version, reconciled_at FROM api_libraries WHERE prefix=?",
                           (prefix,)).fetchone()
        since = 0 if reconcile else int(state["version"])
        counts = {"items": 0, "collections": 0, "deleted": 0, "fulltext": 0}

        coll_resp = await api.get(f"{prefix}/collections", {"since": str(since), "format": "versions"})
        remote_colls = coll_resp.json()
        item_resp = await api.get(f"{prefix}/items", {"since": str(since), "format": "versions",
                                                      "includeTrashed": "1"})
        remote_items = item_resp.json()
        library_version = int(item_resp.headers.get("Last-Modified-Version") or since)

        local_items = {r["key"]: r["version"] for r in db.execute(
            "SELECT key, version FROM items WHERE libraryID=?", (library_id,)).fetchall()}
        local_colls = {r["key"]: r["version"] for r in db.execute(
            "SELECT key, version FROM collections WHERE libraryID=?", (library_id,)).fetchall()}
        want_colls = [k for k, v in remote_colls.items() if local_colls.get(k) != v]
        want_items = [k for k, v in remote_items.items() if local_items.get(k) != v]

        if want_colls:
            data = await _fetch_by_keys(api, prefix, "collections", want_colls, "collectionKey")
            await asyncio.to_thread(_in_transaction, db, apply_collections, library_id, data)
            counts["collections"] = len(data)
        for start in range(0, len(want_items), 1000):
            data = await _fetch_by_keys(api, prefix, "items", want_items[start:start + 1000], "itemKey")
            await asyncio.to_thread(_in_transaction, db, apply_items, library_id, data)
            counts["items"] += len(data)

        if reconcile:
            gone_items = [k for k in local_items if k not in remote_items]
            gone_colls = [k for k in local_colls if k not in remote_colls]
        else:
            deleted = (await api.get(f"{prefix}/deleted", {"since": str(since)})).json()
            gone_items = deleted.get("items") or []
            gone_colls = deleted.get("collections") or []

        def remove():
            for key in gone_items:
                _delete_item(db, library_id, key)
            for key in gone_colls:
                _delete_collection(db, library_id, key)
        if gone_items or gone_colls:
            await asyncio.to_thread(_in_transaction, db, lambda *_: remove())
            counts["deleted"] = len(gone_items) + len(gone_colls)

        if fulltext:
            counts["fulltext"] = await _sync_fulltext(
                api, db, storage, prefix, library_id,
                0 if reconcile else int(state["fulltext_version"]), library_version)

        now = time.time()
        db.execute(
            "UPDATE api_libraries SET version=?, fulltext_version=?, synced_at=?, error='' "
            + (", reconciled_at=?" if reconcile else "") + " WHERE prefix=?",
            (library_version, library_version if fulltext else int(state["fulltext_version"]), now,
             *([now] if reconcile else []), prefix),
        )
        db.execute("UPDATE libraries SET version=?, lastSync=? WHERE libraryID=?",
                   (library_version, int(now), library_id))
        return counts
    finally:
        await asyncio.to_thread(db.close)


def _in_transaction(db, fn, *args):
    with _write_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            fn(db, *args)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


async def _sync_fulltext(api, db, storage, prefix, library_id, since, library_version):
    """Download extracted text that changed since *since* into storage/<key>/."""
    response = await api.get(f"{prefix}/fulltext", {"since": str(since)})
    remote = response.json()
    local = {r["key"]: r["version"] for r in db.execute(
        "SELECT i.key, f.version FROM fulltextItems f JOIN items i ON i.itemID=f.itemID WHERE i.libraryID=?",
        (library_id,)).fetchall()}
    synced_through = int(db.execute("SELECT fulltext_version FROM api_libraries WHERE libraryID=?",
                                    (library_id,)).fetchone()[0])
    wanted, adopt = [], []
    for k, v in remote.items():
        present = (storage / k / ".zotero-ft-cache").is_file()
        if local.get(k) == v and present:
            continue
        if present and int(v) <= synced_through:
            # Text this copy already holds (or was seeded with) as of a later
            # library version: record the version, keep the file untouched
            # (rewriting it would make the search indexes re-embed the item).
            adopt.append((k, int(v)))
        else:
            wanted.append(k)
    if adopt:
        def record():
            for key, version in adopt:
                row = db.execute("SELECT itemID FROM items WHERE libraryID=? AND key=?", (library_id, key)).fetchone()
                if row:
                    db.execute("INSERT INTO fulltextItems(itemID, version, synced) VALUES(?,?,1) "
                               "ON CONFLICT(itemID) DO UPDATE SET version=excluded.version", (row[0], version))
        await asyncio.to_thread(_in_transaction, db, lambda *_: record())
    semaphore = asyncio.Semaphore(4)
    done = 0

    async def one(key):
        nonlocal done
        async with semaphore:
            resp = await api.get(f"{prefix}/items/{key}/fulltext", allow=(200, 404))
        if resp.status_code != 200:
            return
        body = resp.json()
        target = storage / key / ".zotero-ft-cache"

        def write():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(".zotero-ft-cache.tmp")
            tmp.write_text(body.get("content") or "", encoding="utf-8")
            tmp.replace(target)
            with _write_lock:
                row = db.execute("SELECT itemID FROM items WHERE libraryID=? AND key=?",
                                 (library_id, key)).fetchone()
                if not row:
                    return
                db.execute(
                    "INSERT OR REPLACE INTO fulltextItems(itemID, indexedPages, totalPages, indexedChars, "
                    "totalChars, version, synced) VALUES(?,?,?,?,?,?,1)",
                    (row[0], body.get("indexedPages"), body.get("totalPages"), body.get("indexedChars"),
                     body.get("totalChars"), int(remote[key])),
                )
        await asyncio.to_thread(write)
        done += 1

    for start in range(0, len(wanted), 200):
        await asyncio.gather(*(one(k) for k in wanted[start:start + 200]))
    return done


async def sync(reconcile: bool | None = None, fulltext: bool = True) -> dict:
    """Update the mirror from the Web API. Returns a summary per library.

    *reconcile* None means: reconcile libraries not reconciled in 24 hours.
    """
    global _lock, _last_result
    if not enabled():
        return {"enabled": False}
    if _lock is None:
        _lock = asyncio.Lock()
    from .zotero import zot_config

    db_path, storage = _paths()
    seed = os.getenv("ZOTERO_API_MIRROR_SEED", "")
    user_id = str(zot_config.user_id)
    started = time.time()
    async with _lock:
        db = await asyncio.to_thread(_prepare, db_path, Path(seed) if seed else None, user_id)
        try:
            new_db = not db.execute("SELECT 1 FROM itemTypes LIMIT 1").fetchone()
        finally:
            db.close()
        summary: dict = {"libraries": {}, "errors": {}}
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            api = _Api(client, zot_config.api_key)
            if new_db:
                schema = (await api.get("/schema")).json()
                db = await asyncio.to_thread(_connect, db_path)
                try:
                    await asyncio.to_thread(_in_transaction, db, _seed_schema, schema)
                finally:
                    db.close()
            libraries = [(f"/users/{user_id}", None)]
            groups = (await api.get(f"/users/{user_id}/groups", {"format": "json", "limit": "100"})).json()
            libraries += [(f"/groups/{g['id']}", g) for g in groups]
            db = await asyncio.to_thread(_connect, db_path)
            try:
                known = [r[0] for r in db.execute("SELECT prefix FROM api_libraries").fetchall()]
                reconciled = {r[0]: r[1] for r in db.execute(
                    "SELECT prefix, reconciled_at FROM api_libraries").fetchall()}
                current = {prefix for prefix, _ in libraries}
                for prefix in known:
                    if prefix not in current:
                        logger.info("Zotero library %s is no longer accessible; removing it", prefix)
                        await asyncio.to_thread(_in_transaction, db, lambda d, p=prefix: _drop_library(d, p))
            finally:
                db.close()
            for prefix, group in libraries:
                full = reconcile if reconcile is not None else (
                    time.time() - (reconciled.get(prefix) or 0) > _RECONCILE_SECONDS)
                try:
                    summary["libraries"][prefix] = {
                        **await _sync_library(api, db_path, storage, prefix, user_id, group,
                                              reconcile=full, fulltext=fulltext),
                        "reconciled": full,
                    }
                except Exception as exc:
                    logger.warning("Zotero API mirror sync of %s failed: %s", prefix, exc)
                    summary["errors"][prefix] = str(exc)
                    db = await asyncio.to_thread(_connect, db_path)
                    try:
                        db.execute("UPDATE api_libraries SET error=? WHERE prefix=?", (str(exc)[:500], prefix))
                    finally:
                        db.close()
        db = await asyncio.to_thread(_connect, db_path)
        try:
            # Fold the WAL into the main file: readers key caches on its mtime.
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("INSERT OR REPLACE INTO api_meta(name, value) VALUES('last_sync', ?)",
                       (datetime.now(timezone.utc).isoformat(),))
        finally:
            db.close()
    summary["seconds"] = round(time.time() - started, 1)
    changed = any(any(v for k, v in lib.items() if k != "reconciled") for lib in summary["libraries"].values())
    if changed:
        logger.info("Zotero API mirror updated: %s", json.dumps(summary)[:1000])
    _last_result = summary
    return summary


def status() -> dict:
    """Mirror freshness and per-library state, for status tools."""
    if not enabled():
        return {"enabled": False}
    db_path, _ = _paths()
    if not db_path.exists():
        return {"enabled": True, "built": False}
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            libraries = []
            for row in db.execute(
                "SELECT a.prefix, a.libraryID, a.version, a.synced_at, a.reconciled_at, a.error, "
                "COALESCE(g.name, 'My Library') AS name, "
                "(SELECT COUNT(*) FROM items i WHERE i.libraryID=a.libraryID) AS items "
                "FROM api_libraries a LEFT JOIN groups g ON g.libraryID=a.libraryID ORDER BY a.libraryID"
            ).fetchall():
                libraries.append({
                    "library": row["name"], "prefix": row["prefix"], "version": row["version"],
                    "items": row["items"],
                    "synced_at": datetime.fromtimestamp(row["synced_at"], timezone.utc).isoformat()
                    if row["synced_at"] else None,
                    "reconciled_at": datetime.fromtimestamp(row["reconciled_at"], timezone.utc).isoformat()
                    if row["reconciled_at"] else None,
                    "error": row["error"] or None,
                })
            last = db.execute("SELECT value FROM api_meta WHERE name='last_sync'").fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return {"enabled": True, "built": True, "error": str(exc)}
    age = None
    if last:
        age = int(time.time() - datetime.fromisoformat(last[0]).timestamp())
    return {"enabled": True, "built": True, "last_sync": last[0] if last else None,
            "age_seconds": age, "libraries": libraries, "last_run": _last_result}
