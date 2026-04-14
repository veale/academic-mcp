"""Direct read-only access to Zotero's local SQLite database.

This module provides the fastest possible search and retrieval path by
reading the synced zotero.sqlite file directly — no API calls, no running
Zotero instance required. It supports:

  - Full metadata search (title, creators, DOI, abstract, tags, fulltext)
  - DOI lookup (instant, via SQL — no index building needed)
  - Fulltext search using Zotero's word index (fulltextItemWords)
  - Group library support (all libraries in a single DB)
  - Attachment/PDF resolution via local storage, local WebDAV dir, or WebDAV HTTP

IMPORTANT: The database is opened READ-ONLY. This module never writes to
zotero.sqlite. Zotero's documentation explicitly warns against writing.

All database access uses ``aiosqlite`` for native async I/O, eliminating
the thread-pool overhead that ``asyncio.to_thread()`` + ``sqlite3`` incurs
on every query.

Typical Zotero data directory layout:
  ~/Zotero/
    zotero.sqlite          ← main database (metadata + fulltext word index)
    storage/
      ABCD1234/            ← attachment folders (8-char keys)
        paper.pdf
        .zotero-ft-cache   ← cached fulltext plain text

The .zotero-ft-cache files in storage/ contain the full extracted text
of PDFs, which is what Zotero uses for its fulltext search index. We can
read these directly for instant fulltext retrieval without PDF parsing.

WebDAV local path:
  If your WebDAV server (e.g. self-hosted Nextcloud) stores the zotero/
  directory locally, set ZOTERO_WEBDAV_LOCAL_PATH to that directory.
  This avoids HTTP round-trips entirely — reads <key>.zip directly from disk.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import aiosqlite
import httpx

from .config import config as app_config
from .models import (
    AttachmentInfo, Creator, FulltextInfo, LibraryInfo,
    PaperContent, ZoteroItem,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ZoteroSQLiteConfig:
    """Configuration for direct SQLite access."""

    def __init__(self):
        self.db_path = str(Path(os.getenv(
            "ZOTERO_SQLITE_PATH",
            str(Path.home() / "Zotero" / "zotero.sqlite"),
        )).expanduser())
        self.storage_path = str(Path(os.getenv(
            "ZOTERO_LOCAL_STORAGE",
            str(Path.home() / "Zotero" / "storage"),
        )).expanduser())
        self.webdav_url = os.getenv("ZOTERO_WEBDAV_URL", "")
        self.webdav_user = os.getenv("ZOTERO_WEBDAV_USER", "")
        self.webdav_pass = os.getenv("ZOTERO_WEBDAV_PASS", "")
        # Local WebDAV directory — if the WebDAV server (e.g. Nextcloud)
        # stores zotero/<key>.zip on this machine, point here to skip HTTP.
        self.webdav_local_path = os.getenv("ZOTERO_WEBDAV_LOCAL_PATH", "")

    @property
    def available(self) -> bool:
        p = Path(self.db_path)
        return p.exists() and p.is_file()

    @property
    def webdav_http_available(self) -> bool:
        return bool(self.webdav_url)

    @property
    def webdav_local_available(self) -> bool:
        if not self.webdav_local_path:
            return False
        return Path(self.webdav_local_path).is_dir()


sqlite_config = ZoteroSQLiteConfig()


# ---------------------------------------------------------------------------
# Database connection (read-only, WAL mode for concurrent access)
# ---------------------------------------------------------------------------

async def _get_connection() -> aiosqlite.Connection:
    """Open a read-only aiosqlite connection.

    If Zotero's database is in WAL mode (recommended — run
    ``sqlite3 ~/Zotero/zotero.sqlite "PRAGMA journal_mode=WAL;"`` with
    Zotero closed), readers and writers never block each other and timeout
    is irrelevant.

    If still in DELETE mode, SQLite holds an exclusive lock during writes.
    timeout=15 lets us wait out transient sync locks rather than failing
    immediately. Lock wait >1 s is logged as a warning.
    """
    t0 = asyncio.get_event_loop().time()
    uri = f"file:{sqlite_config.db_path}?mode=ro"
    conn = await aiosqlite.connect(uri, uri=True, timeout=15)
    elapsed = asyncio.get_event_loop().time() - t0
    if elapsed > 1.0:
        logger.warning(
            "SQLite lock wait: %.1fs — consider switching Zotero to WAL mode: "
            "sqlite3 ~/Zotero/zotero.sqlite \"PRAGMA journal_mode=WAL;\"",
            elapsed,
        )
    conn.row_factory = sqlite3.Row
    await conn.execute("PRAGMA query_only=ON")
    return conn


# ---------------------------------------------------------------------------
# Field ID resolution (cached in-process)
# ---------------------------------------------------------------------------

_field_ids: dict[str, int] = {}


async def _get_field_id(conn: aiosqlite.Connection, field_name: str) -> Optional[int]:
    if not _field_ids:
        for table in ("fields", "fieldsCombined"):
            try:
                cursor = await conn.execute(
                    f"SELECT fieldID, fieldName FROM {table}"
                )
                rows = await cursor.fetchall()
                for r in rows:
                    _field_ids.setdefault(r["fieldName"], r["fieldID"])
            except Exception:
                pass
    return _field_ids.get(field_name)


# ---------------------------------------------------------------------------
# Library enumeration
# ---------------------------------------------------------------------------

async def _get_all_libraries(conn: aiosqlite.Connection) -> list[LibraryInfo]:
    cursor = await conn.execute("""
        SELECT l.libraryID, l.type,
               COALESCE(g.name, 'My Library') AS name,
               g.groupID
        FROM libraries l
        LEFT JOIN groups g ON l.libraryID = g.libraryID
        ORDER BY l.libraryID
    """)
    rows = await cursor.fetchall()
    return [
        LibraryInfo(
            libraryID=r["libraryID"], type=r["type"],
            name=r["name"], groupID=r["groupID"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Item field value retrieval
# ---------------------------------------------------------------------------

async def _get_item_fields(conn: aiosqlite.Connection, item_id: int) -> dict[str, str]:
    cursor = await conn.execute("""
        SELECT f.fieldName, idv.value
        FROM itemData id
        JOIN fields f ON id.fieldID = f.fieldID
        JOIN itemDataValues idv ON id.valueID = idv.valueID
        WHERE id.itemID = ?
    """, (item_id,))
    rows = await cursor.fetchall()
    return {r["fieldName"]: r["value"] for r in rows}


async def _get_item_creators(conn: aiosqlite.Connection, item_id: int) -> list[Creator]:
    cursor = await conn.execute("""
        SELECT c.firstName, c.lastName, ct.creatorType
        FROM itemCreators ic
        JOIN creators c ON ic.creatorID = c.creatorID
        JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
        WHERE ic.itemID = ?
        ORDER BY ic.orderIndex
    """, (item_id,))
    rows = await cursor.fetchall()
    return [
        Creator(firstName=r["firstName"], lastName=r["lastName"],
                creatorType=r["creatorType"])
        for r in rows
    ]


async def _get_item_tags(conn: aiosqlite.Connection, item_id: int) -> list[str]:
    cursor = await conn.execute("""
        SELECT t.name FROM itemTags it
        JOIN tags t ON it.tagID = t.tagID
        WHERE it.itemID = ?
    """, (item_id,))
    rows = await cursor.fetchall()
    return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# PDF attachment resolution
# ---------------------------------------------------------------------------

async def _get_pdf_attachment(conn: aiosqlite.Connection, item_id: int) -> Optional[AttachmentInfo]:
    cursor = await conn.execute("""
        SELECT ia.itemID, i.key, ia.path, ia.linkMode
        FROM itemAttachments ia
        JOIN items i ON ia.itemID = i.itemID
        WHERE ia.parentItemID = ?
          AND ia.contentType = 'application/pdf'
        ORDER BY ia.itemID LIMIT 1
    """, (item_id,))
    row = await cursor.fetchone()
    if row:
        return AttachmentInfo(
            itemID=row["itemID"], key=row["key"],
            path=row["path"] or "", linkMode=row["linkMode"] or 0,
        )
    return None


# ---------------------------------------------------------------------------
# Fulltext retrieval — .zotero-ft-cache files
# ---------------------------------------------------------------------------

def _get_fulltext_from_cache(attachment_key: str) -> Optional[str]:
    """Read the .zotero-ft-cache file, capped at the context limit.

    Some OCR'd textbooks produce ft-cache files exceeding 50–100 MB.
    Reading the whole file into RAM only to truncate it later is wasteful,
    so we stop at ``max_context_length`` characters.
    """
    cache_path = Path(sqlite_config.storage_path) / attachment_key / ".zotero-ft-cache"
    try:
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(app_config.max_context_length)
    except OSError as e:
        logger.debug("Failed to read ft-cache for %s: %s", attachment_key, e)
    return None


# ---------------------------------------------------------------------------
# PDF retrieval — return Path (zero-copy), never bytes
# ---------------------------------------------------------------------------

def _get_pdf_path_from_local_storage(attachment_key: str) -> Optional[Path]:
    """Return the Path to a locally stored PDF (no bytes loaded)."""
    storage_dir = Path(sqlite_config.storage_path) / attachment_key
    try:
        if not storage_dir.exists():
            return None
        for f in storage_dir.iterdir():
            if f.suffix.lower() == ".pdf" and f.is_file():
                return f
    except OSError as e:
        logger.debug("Failed to find local PDF for %s: %s", attachment_key, e)
    return None


# Maximum bytes we'll extract from a single zip member.  Anything larger
# than this is almost certainly not a legitimate academic PDF (or is a
# zip bomb).  150 MB is generous — most papers are well under 50 MB.
_MAX_EXTRACTED_BYTES = 150 * 1024 * 1024


def _extract_pdf_from_zip_to_path(zip_path: Path, dest: Path) -> Optional[Path]:
    """Extract a PDF from a Zotero WebDAV zip to *dest*, returning the Path.

    Falls back to treating the file as a raw PDF if it is not a valid zip.
    Enforces a size cap (``_MAX_EXTRACTED_BYTES``) to guard against zip bombs.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                with zf.open(name) as member:
                    header = member.read(5)
                    if header == b"%PDF-":
                        written = 0
                        with open(dest, "wb") as out:
                            out.write(header)
                            written += len(header)
                            while True:
                                block = member.read(65536)
                                if not block:
                                    break
                                written += len(block)
                                if written > _MAX_EXTRACTED_BYTES:
                                    logger.warning(
                                        "Zip member %r in %s exceeds %d MB "
                                        "size limit — aborting extraction "
                                        "(possible zip bomb)",
                                        name, zip_path.name,
                                        _MAX_EXTRACTED_BYTES // (1024 * 1024),
                                    )
                                    # Clean up the partial file
                                    out.close()
                                    dest.unlink(missing_ok=True)
                                    return None
                                out.write(block)
                        return dest
    except zipfile.BadZipFile:
        # Might be a raw PDF rather than a zip
        try:
            with open(zip_path, "rb") as f:
                if f.read(5) == b"%PDF-":
                    zip_path.rename(dest)
                    return dest
        except OSError:
            pass
    return None


def _get_pdf_path_from_webdav_local(attachment_key: str) -> Optional[Path]:
    """Read PDF from a locally-mounted WebDAV directory, extracting to cache.

    When the WebDAV server (e.g. Nextcloud) stores zotero/<key>.zip on the
    same machine, we skip HTTP entirely and read the zip from disk.
    """
    if not sqlite_config.webdav_local_available:
        return None
    zip_path = Path(sqlite_config.webdav_local_path) / f"{attachment_key}.zip"
    try:
        if not zip_path.exists():
            return None
        dest = app_config.pdf_cache_dir / f"sqlite_webdav_local_{attachment_key}.pdf"
        if dest.exists():
            return dest
        return _extract_pdf_from_zip_to_path(zip_path, dest)
    except OSError as e:
        logger.debug("Local WebDAV read failed for %s: %s", attachment_key, e)
    return None


async def _get_pdf_path_from_webdav_http(attachment_key: str) -> Optional[Path]:
    """Fetch PDF from WebDAV over HTTP, streaming zip to disk then extracting."""
    if not sqlite_config.webdav_http_available:
        return None
    dest = app_config.pdf_cache_dir / f"sqlite_webdav_http_{attachment_key}.pdf"
    if dest.exists():
        return dest
    zip_url = urljoin(
        sqlite_config.webdav_url.rstrip("/") + "/",
        f"{attachment_key}.zip",
    )
    auth = (
        (sqlite_config.webdav_user, sqlite_config.webdav_pass)
        if sqlite_config.webdav_user else None
    )
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", zip_url, auth=auth) as resp:
                if resp.status_code != 200:
                    return None
                # Stream to a temp zip file on disk
                fd, tmp_name = tempfile.mkstemp(
                    dir=app_config.pdf_cache_dir, suffix=".zip.tmp",
                )
                tmp_zip = Path(tmp_name)
                try:
                    with open(fd, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                    return _extract_pdf_from_zip_to_path(tmp_zip, dest)
                finally:
                    tmp_zip.unlink(missing_ok=True)
    except httpx.HTTPStatusError as e:
        logger.debug("WebDAV HTTP error for %s: %s", attachment_key, e)
    except httpx.RequestError as e:
        logger.debug("WebDAV request failed for %s: %s", attachment_key, e)
    return None


# ---------------------------------------------------------------------------
# Fulltext index info
# ---------------------------------------------------------------------------

async def _get_fulltext_info(
    conn: aiosqlite.Connection, attachment_item_id: int,
) -> Optional[FulltextInfo]:
    cursor = await conn.execute("""
        SELECT indexedPages, totalPages, indexedChars, totalChars, version
        FROM fulltextItems WHERE itemID = ?
    """, (attachment_item_id,))
    row = await cursor.fetchone()
    if row:
        return FulltextInfo(
            indexedPages=row["indexedPages"], totalPages=row["totalPages"],
            indexedChars=row["indexedChars"], totalChars=row["totalChars"],
            version=row["version"],
        )
    return None


# ---------------------------------------------------------------------------
# Core: Build a ZoteroItem from an itemID
# ---------------------------------------------------------------------------

async def _build_item(conn: aiosqlite.Connection, item_id: int) -> Optional[ZoteroItem]:
    cursor = await conn.execute("""
        SELECT i.itemID, i.key, i.libraryID, it.typeName,
               i.dateAdded, i.dateModified
        FROM items i
        JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
        WHERE i.itemID = ?
    """, (item_id,))
    item_row = await cursor.fetchone()
    if not item_row:
        return None

    fields = await _get_item_fields(conn, item_id)
    creators = await _get_item_creators(conn, item_id)
    tags = await _get_item_tags(conn, item_id)

    cursor = await conn.execute("""
        SELECT l.type, COALESCE(g.name, 'My Library') AS name
        FROM libraries l
        LEFT JOIN groups g ON l.libraryID = g.libraryID
        WHERE l.libraryID = ?
    """, (item_row["libraryID"],))
    lib_row = await cursor.fetchone()

    return ZoteroItem(
        itemID=item_id, key=item_row["key"],
        libraryID=item_row["libraryID"],
        libraryName=lib_row["name"] if lib_row else "Unknown",
        libraryType=lib_row["type"] if lib_row else "unknown",
        itemType=item_row["typeName"],
        title=fields.get("title", ""),
        DOI=fields.get("DOI", ""),
        url=fields.get("url", ""),
        date=fields.get("date", ""),
        abstractNote=fields.get("abstractNote", ""),
        publicationTitle=fields.get("publicationTitle", ""),
        creators=creators, tags=tags,
        extra=fields.get("extra", ""),
        dateAdded=item_row["dateAdded"],
        dateModified=item_row["dateModified"],
    )


async def _excluded_type_ids(conn: aiosqlite.Connection) -> list[int]:
    cursor = await conn.execute("""
        SELECT itemTypeID FROM itemTypes
        WHERE typeName IN ('attachment', 'note', 'annotation')
    """)
    rows = await cursor.fetchall()
    ids = [r["itemTypeID"] for r in rows]
    return ids if ids else [14, 1, 36]


# ---------------------------------------------------------------------------
# Search: by DOI
# ---------------------------------------------------------------------------

async def search_by_doi(doi: str) -> Optional[ZoteroItem]:
    """Look up an item by DOI — pure async, no thread pool."""
    if not sqlite_config.available:
        return None
    doi_clean = (
        doi.lower()
        .replace("https://doi.org/", "")
        .replace("http://doi.org/", "")
        .strip()
    )
    conn = await _get_connection()
    try:
        doi_fid = await _get_field_id(conn, "DOI")
        if doi_fid is None:
            return None

        cursor = await conn.execute("""
            SELECT id.itemID FROM itemData id
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.fieldID = ? AND LOWER(TRIM(idv.value)) = ?
            LIMIT 1
        """, (doi_fid, doi_clean))
        row = await cursor.fetchone()

        if not row:
            extra_fid = await _get_field_id(conn, "extra")
            if extra_fid:
                cursor = await conn.execute("""
                    SELECT id.itemID FROM itemData id
                    JOIN itemDataValues idv ON id.valueID = idv.valueID
                    WHERE id.fieldID = ? AND LOWER(idv.value) LIKE ?
                    LIMIT 1
                """, (extra_fid, f"%{doi_clean}%"))
                row = await cursor.fetchone()

        if not row:
            return None
        return await _build_item(conn, row["itemID"])
    except Exception as e:
        logger.warning("SQLite DOI lookup failed: %s", e)
        return None
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Search: keyword
# ---------------------------------------------------------------------------

async def search_items(
    query: str, limit: int = 20,
    library_id: Optional[int] = None,
    include_fulltext: bool = True,
    include_groups: bool = True,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
) -> list[ZoteroItem]:
    """Full keyword search — pure async, no thread pool."""
    if not sqlite_config.available:
        return []
    conn = await _get_connection()
    try:
        terms = [t.strip().lower() for t in query.split() if t.strip()]
        if not terms:
            return []

        if library_id is not None:
            lib_filter = "AND i.libraryID = ?"
            lib_params: list = [library_id]
        elif not include_groups:
            lib_filter = "AND i.libraryID = 1"
            lib_params = []
        else:
            lib_filter = ""
            lib_params = []

        excl = await _excluded_type_ids(conn)
        type_ph = ",".join("?" * len(excl))

        # ── Year filter setup ──────────────────────────────────────
        # Zotero stores dates as free-form strings (e.g. "2023-04-15",
        # "March 2021", "2020"). We parse the first 4 characters as the
        # year for filtering via a subquery on the 'date' field.
        year_filter = ""
        year_params: list = []
        if start_year or end_year:
            date_fid = await _get_field_id(conn, "date")
            if date_fid:
                if start_year and end_year:
                    year_filter = (
                        "AND i.itemID IN ("
                        "  SELECT id2.itemID FROM itemData id2"
                        "  JOIN itemDataValues idv2 ON id2.valueID = idv2.valueID"
                        "  WHERE id2.fieldID = ?"
                        "    AND CAST(SUBSTR(idv2.value, 1, 4) AS INTEGER) BETWEEN ? AND ?"
                        ")"
                    )
                    year_params = [date_fid, start_year, end_year]
                elif start_year:
                    year_filter = (
                        "AND i.itemID IN ("
                        "  SELECT id2.itemID FROM itemData id2"
                        "  JOIN itemDataValues idv2 ON id2.valueID = idv2.valueID"
                        "  WHERE id2.fieldID = ?"
                        "    AND CAST(SUBSTR(idv2.value, 1, 4) AS INTEGER) >= ?"
                        ")"
                    )
                    year_params = [date_fid, start_year]
                elif end_year:
                    year_filter = (
                        "AND i.itemID IN ("
                        "  SELECT id2.itemID FROM itemData id2"
                        "  JOIN itemDataValues idv2 ON id2.valueID = idv2.valueID"
                        "  WHERE id2.fieldID = ?"
                        "    AND CAST(SUBSTR(idv2.value, 1, 4) AS INTEGER) <= ?"
                        ")"
                    )
                    year_params = [date_fid, end_year]

        meta: set[int] = set()
        ft: set[int] = set()

        # Phase 1: Title
        title_fid = await _get_field_id(conn, "title")
        if title_fid:
            like_parts = " AND ".join(["LOWER(idv.value) LIKE ?"] * len(terms))
            cursor = await conn.execute(f"""
                SELECT DISTINCT id.itemID FROM itemData id
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                JOIN items i ON id.itemID = i.itemID
                WHERE id.fieldID = ? AND {like_parts}
                  AND i.itemTypeID NOT IN ({type_ph}) {lib_filter}
                  {year_filter}
                LIMIT ?
            """, [title_fid] + [f"%{t}%" for t in terms] + excl + lib_params + year_params + [limit * 3])
            rows = await cursor.fetchall()
            meta.update(r["itemID"] for r in rows)

        # Phase 2: DOI exact
        doi_fid = await _get_field_id(conn, "DOI")
        if doi_fid and len(terms) == 1:
            cursor = await conn.execute(f"""
                SELECT DISTINCT id.itemID FROM itemData id
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                JOIN items i ON id.itemID = i.itemID
                WHERE id.fieldID = ? AND LOWER(TRIM(idv.value)) = ?
                  AND i.itemTypeID NOT IN ({type_ph}) {lib_filter}
                  {year_filter}
                LIMIT 5
            """, [doi_fid, terms[0]] + excl + lib_params + year_params)
            rows = await cursor.fetchall()
            meta.update(r["itemID"] for r in rows)

        # Phase 3: Creator
        for term in terms:
            cursor = await conn.execute(f"""
                SELECT DISTINCT ic.itemID FROM itemCreators ic
                JOIN creators c ON ic.creatorID = c.creatorID
                JOIN items i ON ic.itemID = i.itemID
                WHERE (LOWER(c.lastName) LIKE ? OR LOWER(c.firstName) LIKE ?)
                  AND i.itemTypeID NOT IN ({type_ph}) {lib_filter}
                  {year_filter}
                LIMIT ?
            """, [f"%{term}%", f"%{term}%"] + excl + lib_params + year_params + [limit * 2])
            rows = await cursor.fetchall()
            meta.update(r["itemID"] for r in rows)

        # Phase 4: Abstract
        abs_fid = await _get_field_id(conn, "abstractNote")
        if abs_fid and len(meta) < limit:
            like_parts = " AND ".join(["LOWER(idv.value) LIKE ?"] * len(terms))
            cursor = await conn.execute(f"""
                SELECT DISTINCT id.itemID FROM itemData id
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                JOIN items i ON id.itemID = i.itemID
                WHERE id.fieldID = ? AND {like_parts}
                  AND i.itemTypeID NOT IN ({type_ph}) {lib_filter}
                  {year_filter}
                LIMIT ?
            """, [abs_fid] + [f"%{t}%" for t in terms] + excl + lib_params + year_params + [limit * 2])
            rows = await cursor.fetchall()
            meta.update(r["itemID"] for r in rows)

        # Phase 5: Tags
        for term in terms:
            cursor = await conn.execute(f"""
                SELECT DISTINCT it.itemID FROM itemTags it
                JOIN tags t ON it.tagID = t.tagID
                JOIN items i ON it.itemID = i.itemID
                WHERE LOWER(t.name) LIKE ?
                  AND i.itemTypeID NOT IN ({type_ph}) {lib_filter}
                  {year_filter}
                LIMIT ?
            """, [f"%{term}%"] + excl + lib_params + year_params + [limit])
            rows = await cursor.fetchall()
            meta.update(r["itemID"] for r in rows)

        # Phase 6: Fulltext word index
        if include_fulltext and len(meta) < limit:
            for term in terms:
                cursor = await conn.execute(f"""
                    SELECT DISTINCT ia.parentItemID AS itemID
                    FROM fulltextItemWords fiw
                    JOIN fulltextWords fw ON fiw.wordID = fw.wordID
                    JOIN itemAttachments ia ON fiw.itemID = ia.itemID
                    JOIN items i ON ia.parentItemID = i.itemID
                    WHERE LOWER(fw.word) LIKE ?
                      AND ia.parentItemID IS NOT NULL
                      AND i.itemTypeID NOT IN ({type_ph}) {lib_filter}
                      {year_filter}
                    LIMIT ?
                """, [f"%{term}%"] + excl + lib_params + year_params + [limit * 2])
                rows = await cursor.fetchall()
                for r in rows:
                    if r["itemID"] not in meta:
                        ft.add(r["itemID"])

        all_ids = list(meta)[:limit]
        remaining = limit - len(all_ids)
        if remaining > 0:
            all_ids.extend(list(ft)[:remaining])

        results = []
        for item_id in all_ids:
            item = await _build_item(conn, item_id)
            if item:
                item._match_type = "metadata" if item_id in meta else "fulltext"
                results.append(item)
        return results
    except Exception:
        logger.exception("SQLite search failed")
        return []
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Get paper content
# ---------------------------------------------------------------------------

async def get_paper_content(item: ZoteroItem) -> PaperContent:
    """Get full content for a Zotero item — pure async, returns Path not bytes."""
    result = PaperContent(
        found=True, item_key=item.key,
        title=item.title, DOI=item.DOI, creators=item.creators,
        date=item.date, abstractNote=item.abstractNote,
        publicationTitle=item.publicationTitle, itemType=item.itemType,
        url=item.url, libraryName=item.libraryName,
    )
    if not sqlite_config.available:
        return result

    conn = await _get_connection()
    try:
        att = await _get_pdf_attachment(conn, item.itemID)
        if not att:
            result.source = "sqlite_metadata_only"
            return result

        # 1. ft-cache (fastest — plain-text file, no PDF parsing)
        cached = _get_fulltext_from_cache(att.key)
        if cached:
            result.text = cached
            result.source = "sqlite_ft_cache"
            ft = await _get_fulltext_info(conn, att.itemID)
            if ft:
                result.indexed_pages = ft.indexedPages
                result.total_pages = ft.totalPages
                result.truncated = ft.is_truncated
            return result

        # 2. Local PDF — return Path directly (zero RAM)
        pdf_path = _get_pdf_path_from_local_storage(att.key)
        if pdf_path:
            result.pdf_path = pdf_path
            result.source = "sqlite_local_pdf"
            return result

        # 3. Local WebDAV dir (no HTTP!) — extract zip to cache, return Path
        pdf_path = _get_pdf_path_from_webdav_local(att.key)
        if pdf_path:
            result.pdf_path = pdf_path
            result.source = "sqlite_webdav_local_pdf"
            return result

        # 4. WebDAV over HTTP — stream zip to disk, extract, return Path
        pdf_path = await _get_pdf_path_from_webdav_http(att.key)
        if pdf_path:
            result.pdf_path = pdf_path
            result.source = "sqlite_webdav_http_pdf"
            return result

        result.source = "sqlite_metadata_only"
        return result
    except Exception as e:
        logger.warning("SQLite content retrieval error: %s", e)
        result.source = "sqlite_error"
        return result
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# List libraries
# ---------------------------------------------------------------------------

async def list_libraries() -> list[LibraryInfo]:
    """List all Zotero libraries — pure async."""
    if not sqlite_config.available:
        return []
    conn = await _get_connection()
    try:
        libs = await _get_all_libraries(conn)
        for lib in libs:
            cursor = await conn.execute("""
                SELECT COUNT(*) AS cnt FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE i.libraryID = ?
                  AND it.typeName NOT IN ('attachment', 'note', 'annotation')
            """, (lib.libraryID,))
            row = await cursor.fetchone()
            lib.itemCount = row["cnt"] if row else 0
        return libs
    except Exception as e:
        logger.warning("SQLite list libraries failed: %s", e)
        return []
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# DOI index
# ---------------------------------------------------------------------------

async def build_doi_index() -> dict[str, dict]:
    """Build DOI → item metadata index from SQLite — pure async."""
    if not sqlite_config.available:
        return {}
    conn = await _get_connection()
    try:
        doi_fid = await _get_field_id(conn, "DOI")
        if doi_fid is None:
            return {}
        cursor = await conn.execute("""
            SELECT id.itemID, i.key AS item_key, idv.value AS doi, i.libraryID
            FROM itemData id
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            JOIN items i ON id.itemID = i.itemID
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE id.fieldID = ?
              AND TRIM(idv.value) != ''
              AND it.typeName NOT IN ('attachment', 'note', 'annotation')
        """, (doi_fid,))
        rows = await cursor.fetchall()

        index: dict[str, dict] = {}
        for r in rows:
            dc = (r["doi"].lower()
                  .replace("https://doi.org/", "")
                  .replace("http://doi.org/", "").strip())
            if dc:
                index[dc] = {
                    "item_key": r["item_key"], "itemID": r["itemID"],
                    "libraryID": r["libraryID"],
                    "attachment_key": None, "has_pdf": None, "has_fulltext": None,
                }
        logger.info("Built SQLite DOI index: %d entries", len(index))
        return index
    except Exception as e:
        logger.warning("SQLite DOI index build failed: %s", e)
        return {}
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

async def check_status() -> dict:
    """Report connection and index stats — pure async."""
    status: dict = {
        "configured": bool(sqlite_config.db_path),
        "available": sqlite_config.available,
        "db_path": sqlite_config.db_path,
        "storage_path": sqlite_config.storage_path,
        "webdav_local_configured": sqlite_config.webdav_local_available,
        "webdav_local_path": sqlite_config.webdav_local_path,
        "webdav_http_configured": sqlite_config.webdav_http_available,
    }
    if sqlite_config.available:
        try:
            conn = await _get_connection()
            libs = await _get_all_libraries(conn)
            status["libraries"] = [lib.to_dict() for lib in libs]
            status["library_count"] = len(libs)
            status["group_count"] = sum(1 for lib in libs if lib.type == "group")

            cursor = await conn.execute("""
                SELECT COUNT(*) AS cnt FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
            """)
            row = await cursor.fetchone()
            status["total_items"] = row["cnt"] if row else 0

            doi_fid = await _get_field_id(conn, "DOI")
            if doi_fid:
                cursor = await conn.execute("""
                    SELECT COUNT(*) AS cnt FROM itemData id
                    JOIN itemDataValues idv ON id.valueID = idv.valueID
                    WHERE id.fieldID = ? AND TRIM(idv.value) != ''
                """, (doi_fid,))
                row = await cursor.fetchone()
                status["items_with_doi"] = row["cnt"] if row else 0

            cursor = await conn.execute(
                "SELECT COUNT(*) AS cnt FROM fulltextItems"
            )
            row = await cursor.fetchone()
            status["fulltext_indexed"] = row["cnt"] if row else 0
            await conn.close()
        except Exception as e:
            status["error"] = str(e)
    return status
