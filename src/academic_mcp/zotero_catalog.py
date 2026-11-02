"""Incremental Web API metadata overlay for the read-only Zotero snapshot.

The Mac snapshot remains useful for historical metadata and extracted text.
This independent catalog follows server versions/deletions while that Mac is
asleep. It never writes Zotero's private SQLite schema or changes file sync.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import config

_lock = asyncio.Lock()


class CatalogUnavailable(RuntimeError):
    pass


def enabled():
    from .zotero import zot_config

    return (
        os.getenv("ZOTERO_CATALOG_ENABLED", "true").lower() not in ("false", "0")
        and zot_config.web_api_available
    )


def _connect():
    path = config.pdf_cache_dir.parent / "zotero-catalog.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS libraries (
            prefix TEXT PRIMARY KEY, version INTEGER NOT NULL, mirror_version INTEGER NOT NULL,
            updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS catalog_meta (name TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS items (
            prefix TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(prefix,key));
    """)
    return db


def _mirror_libraries():
    from .zotero import zot_config
    from .zotero_sqlite import sqlite_config

    versions = {}
    mapping = {}
    try:
        with sqlite3.connect(
            Path(sqlite_config.db_path).resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=3,
        ) as db:
            account = db.execute(
                "SELECT value FROM settings WHERE setting='account' AND key='userID'"
            ).fetchone()
            if not account or str(account[0]) != str(zot_config.user_id):
                return versions, mapping
            for lid, kind, version in db.execute(
                "SELECT libraryID,type,version FROM libraries"
            ):
                if kind == "user":
                    prefix = "/users/" + zot_config.user_id
                elif kind == "group":
                    row = db.execute(
                        "SELECT groupID FROM groups WHERE libraryID=?", (lid,)
                    ).fetchone()
                    if not row:
                        continue
                    prefix = "/groups/" + str(row[0])
                else:
                    continue
                versions[prefix] = max(0, int(version))
                mapping[lid] = prefix
    except (sqlite3.Error, OSError):
        pass
    return versions, mapping


async def _get(client, path, params=None):
    from .zotero import zot_config

    for attempt in range(3):
        try:
            response = await client.get(
                "https://api.zotero.org" + path,
                params=params or {},
                headers={
                    "Zotero-API-Key": zot_config.api_key,
                    "Zotero-API-Version": "3",
                },
            )
        except httpx.RequestError:
            if attempt == 2:
                raise CatalogUnavailable(
                    "Zotero metadata endpoint is unreachable"
                ) from None
            await asyncio.sleep(attempt + 1)
            continue
        if response.status_code == 200:
            return response
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
            raise CatalogUnavailable(
                f"Zotero metadata read failed ({response.status_code}) for {path}"
            )
        delay = min(60, max(1, int(response.headers.get("Retry-After", attempt + 1))))
        await asyncio.sleep(delay)
    raise CatalogUnavailable("Zotero metadata read exhausted retries")


async def refresh(force=False, max_age_seconds=300):
    if not enabled():
        return
    from .zotero import zot_config

    async with _lock:
        with _connect() as db:
            completed = db.execute(
                "SELECT value FROM catalog_meta WHERE name='last_complete'"
            ).fetchone()
            latest = completed[0] if completed else None
            if not force and latest:
                age = time.time() - datetime.fromisoformat(latest).timestamp()
                if age < max_age_seconds:
                    return
            mirror_versions, _ = await asyncio.to_thread(_mirror_libraries)
            async with httpx.AsyncClient(timeout=30) as client:
                if zot_config.library_type == "group":
                    prefixes = [zot_config.library_prefix]
                else:
                    prefixes = ["/users/" + zot_config.user_id]
                    start = 0
                    while True:
                        groups = (
                            await _get(
                                client,
                                "/users/" + zot_config.user_id + "/groups",
                                {"limit": 100, "start": start},
                            )
                        ).json()
                        prefixes.extend("/groups/" + str(g["id"]) for g in groups)
                        if len(groups) < 100:
                            break
                        start += len(groups)
                for prefix in prefixes:
                    row = db.execute(
                        "SELECT * FROM libraries WHERE prefix=?", (prefix,)
                    ).fetchone()
                    mirror = mirror_versions.get(prefix, 0)
                    since = int(row["version"]) if row else mirror
                    reset = bool(row and mirror > int(row["version"]))
                    if reset:
                        since = mirror
                    # Do not advance a version across a moving paginated view.
                    # A library changed during the download is retried whole.
                    for consistency_attempt in range(3):
                        changed = []
                        start = 0
                        version = None
                        consistent = True
                        while True:
                            response = await _get(
                                client,
                                prefix + "/items",
                                {
                                    "since": since,
                                    "limit": 100,
                                    "start": start,
                                    "includeTrashed": 1,
                                },
                            )
                            page_version = int(
                                response.headers["Last-Modified-Version"]
                            )
                            if version is not None and page_version != version:
                                consistent = False
                                break
                            version = page_version
                            page = response.json()
                            changed.extend(page)
                            if len(page) < 100:
                                break
                            start += len(page)
                        deleted_response = await _get(
                            client, prefix + "/deleted", {"since": since}
                        )
                        if (
                            int(deleted_response.headers["Last-Modified-Version"])
                            != version
                        ):
                            consistent = False
                        if consistent:
                            break
                    if not consistent:
                        raise CatalogUnavailable(
                            f"Zotero library kept changing during metadata sync: {prefix}"
                        )
                    if reset:
                        db.execute("DELETE FROM items WHERE prefix=?", (prefix,))
                    for item in changed:
                        data = item["data"]
                        db.execute(
                            "INSERT OR REPLACE INTO items VALUES(?,?,?)",
                            (prefix, item["key"], json.dumps(data)),
                        )
                    for key in deleted_response.json().get("items", []):
                        db.execute(
                            "INSERT OR REPLACE INTO items VALUES(?,?,?)",
                            (prefix, key, json.dumps({"key": key, "deleted": True})),
                        )
                    now = datetime.now(timezone.utc).isoformat()
                    db.execute(
                        "INSERT OR REPLACE INTO libraries VALUES(?,?,?,?,?)",
                        (prefix, version, mirror, now, ""),
                    )
                    db.commit()
                db.execute(
                    "INSERT OR REPLACE INTO catalog_meta VALUES('last_complete',?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                db.commit()


def _record(data, prefix):
    authors = []
    for creator in data.get("creators", []):
        name = creator.get("name") or " ".join(
            filter(None, [creator.get("firstName"), creator.get("lastName")])
        )
        if name:
            authors.append(name)
    return {
        "item_key": data["key"],
        "_catalog": True,
        "library_prefix": prefix,
        "title": data.get("title")
        or data.get("caseName")
        or data.get("nameOfAct")
        or "",
        "abstract": data.get("abstractNote", ""),
        "doi": data.get("DOI", ""),
        "dateModified": data.get("dateModified", ""),
        "item_type": data.get("itemType", ""),
        "publicationTitle": data.get("publicationTitle", ""),
        "bookTitle": data.get("bookTitle", ""),
        "publisher": data.get("publisher", ""),
        "venue": data.get("publicationTitle")
        or data.get("bookTitle")
        or data.get("publisher", ""),
        "authors": authors,
        "tags": [t["tag"] for t in data.get("tags", [])],
        "attachment_key": "",
    }


async def overlay(base):
    if not enabled():
        return base
    await refresh()

    def merge():
        with _connect() as db:
            rows = [
                (r["prefix"], json.loads(r["data"]))
                for r in db.execute("SELECT prefix,data FROM items")
            ]
        result = {item["item_key"]: dict(item) for item in base}
        attachments = []
        attachment_prefixes = {}
        removed = set()
        for prefix, data in rows:
            key = data["key"]
            if data.get("deleted"):
                removed.add(key)
                result.pop(key, None)
            elif data.get("itemType") == "attachment":
                attachments.append(data)
                attachment_prefixes[key] = prefix
            elif data.get("itemType") not in ("note", "annotation"):
                old = result.get(key, {})
                record = _record(data, prefix)
                record["attachment_key"] = old.get("attachment_key", "")
                result[key] = record
        for item in result.values():
            if item.get("attachment_key") in removed:
                item["attachment_key"] = ""
        for attachment in attachments:
            parent = result.get(attachment.get("parentItem"))
            if (
                parent
                and attachment.get("contentType") == "application/pdf"
                and (
                    not parent.get("attachment_key")
                    or parent.get("attachment_key") == attachment["key"]
                )
            ):
                parent["attachment_key"] = attachment["key"]
                parent["attachment_md5"] = attachment.get("md5", "")
                parent["library_prefix"] = attachment_prefixes[attachment["key"]]
                parent["_catalog"] = True
        return [
            item
            for item in result.values()
            if item.get("title") or item.get("abstract")
        ]

    return await asyncio.to_thread(merge)


def status():
    if not enabled():
        return {"enabled": False}
    with _connect() as db:
        rows = [
            dict(r)
            for r in db.execute("SELECT prefix,version,updated_at,error FROM libraries")
        ]
    last = min((r["updated_at"] for r in rows), default=None)
    return {
        "enabled": True,
        "updated_at": last,
        "age_seconds": round(time.time() - datetime.fromisoformat(last).timestamp())
        if last
        else None,
        "libraries": rows,
    }


async def update_doi_index(index):
    """Overlay recently synced records onto the discovery/fetch DOI index."""
    if not enabled():
        return index
    await refresh()
    with _connect() as db:
        rows = [
            (r["prefix"], json.loads(r["data"]))
            for r in db.execute("SELECT prefix,data FROM items")
        ]
    changed = {
        data["key"]
        for _, data in rows
        if data.get("itemType") not in ("attachment", "note", "annotation")
    }
    result = {
        doi: entry
        for doi, entry in index.items()
        if entry.get("item_key") not in changed
    }
    children = {}
    removed = {data["key"] for _, data in rows if data.get("deleted")}
    for _, data in rows:
        if (
            data.get("itemType") == "attachment"
            and data.get("contentType") == "application/pdf"
            and not data.get("deleted")
        ):
            children.setdefault(data.get("parentItem"), data["key"])
    from .zotero import _normalize_doi, zot_config

    for prefix, data in rows:
        doi = data.get("DOI", "")
        if (
            not doi
            or data.get("deleted")
            or data.get("itemType") in ("attachment", "note", "annotation")
        ):
            continue
        existing = result.get(_normalize_doi(doi))
        if (
            existing
            and existing.get("library_prefix") == zot_config.library_prefix
            and prefix != zot_config.library_prefix
        ):
            continue
        result[_normalize_doi(doi)] = {
            "item_key": data["key"],
            "_catalog": True,
            "attachment_key": children.get(data["key"]),
            "has_pdf": None,
            "has_fulltext": None,
            "library_prefix": prefix,
            "_catalog": True,
        }
    for entry in result.values():
        if entry.get("attachment_key") in removed:
            entry["attachment_key"] = None
        if entry.get("item_key") in children:
            entry["attachment_key"] = children[entry["item_key"]]
    return result


def library_for_key(key):
    if not enabled():
        return None
    with _connect() as db:
        row = db.execute(
            "SELECT prefix FROM items WHERE key=? LIMIT 1", (key,)
        ).fetchone()
    return row[0] if row else None


async def hydrate_keys(keys, baseline):
    """Render current catalog records for search hits absent from an old mirror."""
    if not enabled() or not keys:
        return baseline
    from .models import ZoteroItem, Creator

    result = dict(baseline)
    with _connect() as db:
        for start in range(0, len(keys), 500):
            batch = keys[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = db.execute(
                f"SELECT prefix,key,data FROM items WHERE key IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                data = json.loads(row["data"])
                key = row["key"]
                if data.get("deleted"):
                    result.pop(key, None)
                    continue
                if data.get("itemType") in ("attachment", "note", "annotation"):
                    continue
                prefix = row["prefix"]
                group = prefix.startswith("/groups/")
                creators = [
                    Creator(
                        firstName=c.get("firstName", ""),
                        lastName=c.get("lastName") or c.get("name", ""),
                        creatorType=c.get("creatorType", "author"),
                    )
                    for c in data.get("creators", [])
                ]
                result[key] = ZoteroItem(
                    key=key,
                    libraryType="group" if group else "user",
                    groupID=int(prefix.rsplit("/", 1)[1]) if group else None,
                    libraryName=("Group " + prefix.rsplit("/", 1)[1])
                    if group
                    else "My Library",
                    itemType=data.get("itemType", "document"),
                    title=data.get("title")
                    or data.get("caseName")
                    or data.get("nameOfAct", ""),
                    DOI=data.get("DOI", ""),
                    url=data.get("url", ""),
                    date=data.get("date", ""),
                    abstractNote=data.get("abstractNote", ""),
                    publicationTitle=data.get("publicationTitle")
                    or data.get("bookTitle", ""),
                    creators=creators,
                    tags=[t["tag"] for t in data.get("tags", [])],
                    extra=data.get("extra", ""),
                    dateModified=data.get("dateModified", ""),
                    fields={k: v for k, v in data.items() if isinstance(v, str)},
                )
    return result
