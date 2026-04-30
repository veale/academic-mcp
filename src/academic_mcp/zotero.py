"""Zotero integration — check your library before hitting the internet.

Connection modes (fallback chain):

  0. SQLITE   — Direct read-only access to zotero.sqlite
     FASTEST. No API needed, no running Zotero needed. Searches ALL
     libraries (user + groups). Supports DOI lookup, fulltext search,
     and reads .zotero-ft-cache files for instant fulltext retrieval.
     Set ZOTERO_SQLITE_PATH to enable (default: ~/Zotero/zotero.sqlite).

  1. LOCAL APP — Zotero 7/8 desktop, http://localhost:23119/api/users/0/...
     Fast. No auth. Read-only. Also reads PDFs from ~/Zotero/storage/.
     For remote servers: SSH tunnel port 23119 to localhost.

  2. WEB API  — https://api.zotero.org/users/<id>/...
     Needs API key. Supports /fulltext endpoint + /file download.

  3. WEBDAV   — https://your-server/zotero/<key>.zip
     Files stored as zipped PDFs. Needs WebDAV credentials.

IMPORTANT NOTES:
  - The SQLite backend solves the DOI search limitation of the API.
  - The SQLite backend automatically searches ALL group libraries.
  - Zotero fulltext can be TRUNCATED. Default indexing limits are ~100
    pages / ~500K chars.
  - Zotero does NOT support true headless mode for the local API.
"""

import asyncio
import json
import logging
import os
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import httpx

from . import zotero_sqlite
from .config import config as app_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ZoteroConfig:
    def __init__(self):
        self.api_key = os.getenv("ZOTERO_API_KEY", "")
        self.user_id = os.getenv("ZOTERO_USER_ID", "")
        self.library_type = os.getenv("ZOTERO_LIBRARY_TYPE", "user")
        self.group_id = os.getenv("ZOTERO_GROUP_ID", "")
        self.local_enabled = os.getenv("ZOTERO_LOCAL_ENABLED", "true").lower() in ("true", "1")
        self.local_port = int(os.getenv("ZOTERO_LOCAL_PORT", "23119"))
        self.local_host = os.getenv("ZOTERO_LOCAL_HOST", "localhost")
        self.webdav_url = os.getenv("ZOTERO_WEBDAV_URL", "")
        self.webdav_user = os.getenv("ZOTERO_WEBDAV_USER", "")
        self.webdav_pass = os.getenv("ZOTERO_WEBDAV_PASS", "")
        self.local_storage_path = os.getenv(
            "ZOTERO_LOCAL_STORAGE", str(Path.home() / "Zotero" / "storage")
        )
        # Where to persist the DOI index between restarts
        cache_dir = Path(os.getenv("PDF_CACHE_DIR", "~/.cache/academic-mcp/pdfs")).expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.doi_index_path = cache_dir / "zotero_doi_index.json"

    @property
    def library_prefix(self):
        if self.library_type == "group" and self.group_id:
            return f"/groups/{self.group_id}"
        return f"/users/{self.user_id}"

    @property
    def local_library_prefix(self):
        if self.library_type == "group" and self.group_id:
            return f"/groups/{self.group_id}"
        return "/users/0"

    @property
    def local_base(self):
        return f"http://{self.local_host}:{self.local_port}/api{self.local_library_prefix}"

    @property
    def web_api_available(self):
        return bool(self.api_key and self.user_id)

    @property
    def webdav_available(self):
        return bool(self.webdav_url)


zot_config = ZoteroConfig()


def _normalize_doi(doi: str) -> str:
    return doi.lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


# ---------------------------------------------------------------------------
# DOI index — enriched cache
# ---------------------------------------------------------------------------
# Each entry: {item_key, attachment_key, has_pdf, has_fulltext_synced}
# has_fulltext_synced is set True when we successfully retrieve fulltext once

_doi_index: dict[str, dict] | None = None


def _load_cached_index() -> dict[str, dict] | None:
    """Load index from disk if it exists."""
    try:
        if zot_config.doi_index_path.exists():
            data = json.loads(zot_config.doi_index_path.read_text())
            if isinstance(data, dict) and data.get("_version") == 2:
                logger.info("Loaded cached DOI index: %d entries", len(data) - 1)
                return {k: v for k, v in data.items() if k != "_version"}
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.debug("Could not load cached index: %s", e)
    return None


def _save_index(index: dict[str, dict]):
    """Persist index to disk."""
    try:
        data = dict(index)
        data["_version"] = 2
        zot_config.doi_index_path.write_text(json.dumps(data))
    except OSError as e:
        logger.debug("Could not save index: %s", e)


async def _local_api_get(path: str, params: dict | None = None) -> httpx.Response | None:
    """Make a GET to the local Zotero API. Returns response or None."""
    if not zot_config.local_enabled:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{zot_config.local_base}{path}",
                params=params or {},
                headers={"Zotero-Allowed-Request": "1"},
            )
            if resp.status_code == 200:
                return resp
    except (httpx.ConnectError, httpx.ConnectTimeout):
        pass
    return None


async def _web_api_get(path: str, params: dict | None = None) -> httpx.Response | None:
    """Make a GET to api.zotero.org. Returns response or None."""
    if not zot_config.web_api_available:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"https://api.zotero.org{zot_config.library_prefix}{path}",
                params=params or {},
                headers={"Zotero-API-Key": zot_config.api_key},
            )
            if resp.status_code == 200:
                return resp
    except httpx.RequestError:
        pass
    return None


async def _build_doi_index() -> dict[str, dict]:
    """Scan all library items and build DOI -> metadata index.

    Tries SQLite first (instant, covers ALL libraries including groups),
    then falls back to local API, then web API.

    Web API pagination is parallelised: the first request reads the
    ``Total-Results`` header, then all remaining pages are fetched
    concurrently with ``asyncio.gather()``.
    """
    # ── SQLite path (fastest, covers all libraries) ──────────────────
    if zotero_sqlite.sqlite_config.available:
        try:
            index = await zotero_sqlite.build_doi_index()
            if index:
                logger.info("Built DOI index from SQLite: %d entries (all libraries)", len(index))
                _save_index(index)
                return index
        except Exception as e:
            logger.warning("SQLite DOI index build failed, falling back to API: %s", e)

    # ── API fallback ─────────────────────────────────────────────────
    items_all: list[dict] = []

    # Try local API first (faster, no pagination limit)
    resp = await _local_api_get("/items", {"format": "json",
                                           "itemType": "-attachment -note -annotation",
                                           "limit": 0})
    if resp:
        items_all = resp.json()
        logger.info("Scanned %d items via local API", len(items_all))
    elif zot_config.web_api_available:
        items_all = await _fetch_all_items_parallel(
            f"https://api.zotero.org{zot_config.library_prefix}/items",
        )
        logger.info("Scanned %d items via web API", len(items_all))

    # Also scan group libraries via web API if configured
    if zot_config.web_api_available and zot_config.api_key:
        try:
            group_ids = await _fetch_user_group_ids()
            if group_ids:
                group_tasks = [
                    _fetch_all_items_parallel(
                        f"https://api.zotero.org/groups/{gid}/items",
                    )
                    for gid in group_ids
                ]
                group_results = await asyncio.gather(
                    *group_tasks, return_exceptions=True,
                )
                for gid, result in zip(group_ids, group_results):
                    if isinstance(result, Exception):
                        logger.warning("Group %s scan failed: %s", gid, result)
                    else:
                        items_all.extend(result)
                        logger.info("Scanned group %s via web API: %d items", gid, len(result))
        except httpx.RequestError as e:
            logger.warning("Group library scan failed: %s", e)

    # Build index
    index = {}
    for item in items_all:
        data = item.get("data", {})
        doi = data.get("DOI", "").strip()
        if not doi:
            continue
        doi_clean = _normalize_doi(doi)
        item_key = data["key"]

        index[doi_clean] = {
            "item_key": item_key,
            "attachment_key": None,
            "has_pdf": None,
            "has_fulltext": None,
        }

    logger.info("Built DOI index with %d entries", len(index))
    _save_index(index)
    return index


_ZOTERO_CONCURRENCY = 5          # max parallel requests to api.zotero.org
_ZOTERO_MAX_RETRIES = 3          # retries per page on 429 / transient errors
_ZOTERO_BACKOFF_BASE = 1.0       # seconds; doubles on each retry


async def _fetch_all_items_parallel(base_url: str, page_size: int = 100) -> list[dict]:
    """Fetch all items from a Zotero Web API endpoint using parallel pagination.

    1. Send the first request to get the ``Total-Results`` header.
    2. Compute remaining page offsets.
    3. Fetch remaining pages concurrently, limited by a semaphore to avoid
       overwhelming Zotero's servers with a burst of 100 simultaneous
       requests (which would trigger ``429 Too Many Requests``).
    """
    common_params = {
        "format": "json",
        "itemType": "-attachment -note -annotation",
        "limit": page_size,
    }
    common_headers = {"Zotero-API-Key": zot_config.api_key}
    sem = asyncio.Semaphore(_ZOTERO_CONCURRENCY)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── First page (we need the Total-Results header) ───────────
        first_resp = await client.get(
            base_url,
            params={**common_params, "start": 0},
            headers=common_headers,
        )
        if first_resp.status_code != 200:
            return []
        items: list[dict] = first_resp.json()
        if not items:
            return []

        # ── How many pages remain? ──────────────────────────────────
        total_results = int(first_resp.headers.get("Total-Results", len(items)))
        if total_results <= page_size:
            return items

        offsets = list(range(page_size, total_results, page_size))

        async def _fetch_page(start: int) -> list[dict]:
            """Fetch one page with semaphore gating and retry on 429."""
            async with sem:
                for attempt in range(_ZOTERO_MAX_RETRIES):
                    try:
                        resp = await client.get(
                            base_url,
                            params={**common_params, "start": start},
                            headers=common_headers,
                        )
                        if resp.status_code == 200:
                            return resp.json()
                        if resp.status_code == 429:
                            # Respect Retry-After header if present
                            retry_after = float(
                                resp.headers.get("Retry-After", 0)
                            )
                            delay = max(
                                retry_after,
                                _ZOTERO_BACKOFF_BASE * (2 ** attempt),
                            )
                            logger.debug(
                                "Zotero 429 for start=%d, retrying in %.1fs",
                                start, delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        # Non-retryable HTTP error
                        logger.debug(
                            "Zotero API returned %d for start=%d",
                            resp.status_code, start,
                        )
                        return []
                    except httpx.RequestError as e:
                        delay = _ZOTERO_BACKOFF_BASE * (2 ** attempt)
                        logger.debug(
                            "Zotero request error for start=%d (attempt %d): %s",
                            start, attempt + 1, e,
                        )
                        await asyncio.sleep(delay)
                logger.warning(
                    "Zotero pagination exhausted retries for start=%d", start
                )
                return []

        # ── Fire remaining pages (semaphore-limited) ────────────────
        pages = await asyncio.gather(*[_fetch_page(s) for s in offsets])
        for page in pages:
            items.extend(page)

    return items


async def _fetch_user_group_ids() -> list[str]:
    """Fetch all group IDs the user has access to via the web API."""
    if not zot_config.web_api_available:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.zotero.org/users/{zot_config.user_id}/groups",
                params={"format": "json", "limit": 100},
                headers={"Zotero-API-Key": zot_config.api_key},
            )
            if resp.status_code == 200:
                groups = resp.json()
                return [str(g.get("id", "")) for g in groups if g.get("id")]
    except httpx.RequestError as e:
        logger.debug("Failed to fetch group IDs: %s", e)
    return []


async def get_doi_index() -> dict[str, dict]:
    global _doi_index
    if _doi_index is not None:
        return _doi_index
    # Try disk cache first
    _doi_index = _load_cached_index()
    if _doi_index is None:
        _doi_index = await _build_doi_index()
    return _doi_index


def invalidate_doi_index():
    global _doi_index
    _doi_index = None
    try:
        zot_config.doi_index_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Item + attachment lookup
# ---------------------------------------------------------------------------

async def _get_item(item_key: str) -> dict | None:
    resp = await _local_api_get(f"/items/{item_key}", {"format": "json"})
    if resp:
        return resp.json()
    resp = await _web_api_get(f"/items/{item_key}", {"format": "json"})
    return resp.json() if resp else None


async def _get_pdf_attachments(item_key: str) -> list[dict]:
    """Get PDF attachment children for an item."""
    children = None
    resp = await _local_api_get(f"/items/{item_key}/children", {"format": "json"})
    if resp:
        children = resp.json()
    if children is None:
        resp = await _web_api_get(f"/items/{item_key}/children", {"format": "json"})
        children = resp.json() if resp else []

    return [
        {"key": c["data"]["key"], "filename": c["data"].get("filename", ""),
         "linkMode": c["data"].get("linkMode", "")}
        for c in children
        if c.get("data", {}).get("itemType") == "attachment"
        and c.get("data", {}).get("contentType") == "application/pdf"
    ]


async def _resolve_attachment(index_entry: dict) -> dict:
    """Lazily fill in attachment_key and has_pdf for an index entry."""
    if index_entry["attachment_key"] is not None:
        return index_entry

    attachments = await _get_pdf_attachments(index_entry["item_key"])
    if attachments:
        index_entry["attachment_key"] = attachments[0]["key"]
        index_entry["has_pdf"] = True
    else:
        index_entry["attachment_key"] = ""
        index_entry["has_pdf"] = False
    return index_entry


async def find_item_by_doi(doi: str) -> dict | None:
    """Look up a paper in Zotero by DOI. Returns full item dict or None.

    SQLite path: instant DOI lookup via SQL (searches all libraries).
    API fallback: uses the DOI index built by scanning items.
    """
    # ── SQLite path (instant) ────────────────────────────────────────
    if zotero_sqlite.sqlite_config.available:
        try:
            result = await zotero_sqlite.search_by_doi(doi)
            if result:
                return {"data": result.to_search_result()}
        except Exception as e:
            logger.debug("SQLite DOI lookup failed: %s", e)

    # ── API fallback ─────────────────────────────────────────────────
    doi_clean = _normalize_doi(doi)
    index = await get_doi_index()
    entry = index.get(doi_clean)
    if not entry:
        return None
    return await _get_item(entry["item_key"])


# ---------------------------------------------------------------------------
# Fulltext retrieval
# ---------------------------------------------------------------------------

async def get_fulltext(attachment_key: str) -> dict | None:
    """Get Zotero's pre-extracted fulltext.

    Returns {content, indexedPages, totalPages} or None.
    The /fulltext endpoint does NOT work on the local API.
    Only works via web API with fulltext sync enabled.

    TRUNCATION: If indexedPages < totalPages (or indexedChars < totalChars),
    the content is partial. Zotero defaults: ~100 pages, ~500K chars.
    Users can increase in Zotero > Settings > Search > PDF Indexing.
    """
    if not zot_config.web_api_available:
        return None
    resp = await _web_api_get(f"/items/{attachment_key}/fulltext")
    if resp:
        data = resp.json()
        if data.get("content"):
            return data
    return None


# ---------------------------------------------------------------------------
# PDF retrieval strategies
# ---------------------------------------------------------------------------

async def get_pdf_from_local_storage(attachment_key: str) -> Path | None:
    """Return the Path to a local PDF (no bytes loaded into RAM)."""
    storage_dir = Path(zot_config.local_storage_path) / attachment_key
    if not storage_dir.exists():
        return None
    for f in storage_dir.iterdir():
        if f.suffix.lower() == ".pdf" and f.is_file():
            return f
    return None


async def get_pdf_from_web_api(attachment_key: str) -> Path | None:
    """Download PDF from Zotero web API, streaming to disk."""
    if not zot_config.web_api_available:
        return None
    dest = app_config.pdf_cache_dir / f"zotero_web_{attachment_key}.pdf"
    if dest.exists():
        return dest
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream(
                "GET",
                f"https://api.zotero.org{zot_config.library_prefix}/items/{attachment_key}/file",
                headers={"Zotero-API-Key": zot_config.api_key},
            ) as resp:
                if resp.status_code != 200:
                    return None
                fd, tmp_name = tempfile.mkstemp(
                    dir=app_config.pdf_cache_dir, suffix=".tmp",
                )
                tmp_path = Path(tmp_name)
                try:
                    header_checked = False
                    with open(fd, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            if not header_checked:
                                if chunk[:5] != b"%PDF-":
                                    return None
                                header_checked = True
                            f.write(chunk)
                    tmp_path.rename(dest)
                    return dest
                finally:
                    tmp_path.unlink(missing_ok=True)
    except httpx.RequestError as e:
        logger.debug("Web API file download failed: %s", e)
    return None


async def get_pdf_from_webdav(attachment_key: str) -> Path | None:
    """Fetch PDF from WebDAV, streaming the zip to a temp file.

    Enforces a 150 MB extraction cap to guard against zip bombs.
    """
    if not zot_config.webdav_available:
        return None
    dest = app_config.pdf_cache_dir / f"zotero_webdav_{attachment_key}.pdf"
    if dest.exists():
        return dest
    zip_url = urljoin(zot_config.webdav_url.rstrip("/") + "/", f"{attachment_key}.zip")
    max_extract = 150 * 1024 * 1024  # 150 MB safety cap
    try:
        auth = (zot_config.webdav_user, zot_config.webdav_pass) if zot_config.webdav_user else None
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", zip_url, auth=auth) as resp:
                if resp.status_code != 200:
                    return None
                # Stream the zip to a temp file
                fd, tmp_name = tempfile.mkstemp(
                    dir=app_config.pdf_cache_dir, suffix=".zip.tmp",
                )
                tmp_zip = Path(tmp_name)
                try:
                    with open(fd, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                    # Extract PDF from the zip on disk
                    try:
                        with zipfile.ZipFile(tmp_zip) as zf:
                            for name in zf.namelist():
                                with zf.open(name) as member:
                                    header = member.read(5)
                                    if header == b"%PDF-":
                                        written = len(header)
                                        with open(dest, "wb") as out:
                                            out.write(header)
                                            while True:
                                                block = member.read(65536)
                                                if not block:
                                                    break
                                                written += len(block)
                                                if written > max_extract:
                                                    logger.warning(
                                                        "WebDAV zip member %r exceeds "
                                                        "150 MB — aborting (possible zip bomb)",
                                                        name,
                                                    )
                                                    out.close()
                                                    dest.unlink(missing_ok=True)
                                                    return None
                                                out.write(block)
                                        return dest
                    except zipfile.BadZipFile:
                        # Might be a raw PDF, not zipped
                        with open(tmp_zip, "rb") as f:
                            if f.read(5) == b"%PDF-":
                                tmp_zip.rename(dest)
                                return dest
                finally:
                    tmp_zip.unlink(missing_ok=True)
    except (httpx.RequestError, OSError) as e:
        logger.debug("WebDAV fetch failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def get_paper_from_zotero(doi: str) -> dict | None:
    """Try to get paper content from Zotero.

    Returns dict with metadata + text/pdf_path, or None if not in library.
    Tries SQLite path first (ft-cache files, local PDFs), then API path.
    """
    # ── SQLite path (fastest — reads .zotero-ft-cache directly) ──────
    if zotero_sqlite.sqlite_config.available:
        try:
            sqlite_item = await zotero_sqlite.search_by_doi(doi)
            if sqlite_item:
                content = await zotero_sqlite.get_paper_content(sqlite_item)
                if content.has_content:
                    return content.to_zotero_result()
                else:
                    # Found but no content — still return metadata
                    content.source = content.source or "sqlite_metadata_only"
                    return content.to_zotero_result()
        except Exception as e:
            logger.debug("SQLite paper retrieval failed, falling back to API: %s", e)

    # ── API fallback ─────────────────────────────────────────────────
    doi_clean = _normalize_doi(doi)
    index = await get_doi_index()
    entry = index.get(doi_clean)
    if not entry:
        return None

    item = await _get_item(entry["item_key"])
    if not item:
        return None

    item_data = item.get("data", {})
    result = {
        "found": True, "item_key": entry["item_key"],
        "metadata": {k: item_data.get(k, "") for k in
                     ("title", "creators", "DOI", "date", "abstractNote",
                      "publicationTitle", "itemType", "url")},
        "text": None, "pdf_path": None, "source": None,
        "truncated": False, "indexed_pages": None, "total_pages": None,
    }

    # Resolve attachment
    await _resolve_attachment(entry)
    att_key = entry.get("attachment_key")
    if not att_key:
        result["source"] = "zotero_metadata_only"
        return result

    # 1. Fulltext (web API only, best path)
    ft = await get_fulltext(att_key)
    if ft and ft.get("content"):
        result["text"] = ft["content"]
        result["source"] = "zotero_fulltext"
        result["indexed_pages"] = ft.get("indexedPages")
        result["total_pages"] = ft.get("totalPages")
        # Detect truncation
        ip = ft.get("indexedPages")
        tp = ft.get("totalPages")
        ic = ft.get("indexedChars")
        tc = ft.get("totalChars")
        if (ip and tp and ip < tp) or (ic and tc and ic < tc):
            result["truncated"] = True
        # Also record local PDF path so the webapp can serve it for the
        # PDF viewer — without this, fulltext-indexed items lose access
        # to their original PDF.  Best-effort; ignore failures.
        try:
            local_pdf = await get_pdf_from_local_storage(att_key)
            if local_pdf:
                result["pdf_path"] = local_pdf
        except Exception as _e:
            logger.debug("Local PDF lookup failed for %s: %s", att_key, _e)
        # Update cache
        entry["has_fulltext"] = True
        _save_index(index)
        return result

    # 2. Local PDF (returns Path directly — no bytes loaded)
    pdf = await get_pdf_from_local_storage(att_key)
    if pdf:
        result["pdf_path"] = pdf
        result["source"] = "zotero_local_pdf"
        return result

    # 3. Web API file download (streams to disk, returns Path)
    pdf = await get_pdf_from_web_api(att_key)
    if pdf:
        result["pdf_path"] = pdf
        result["source"] = "zotero_web_pdf"
        return result

    # 4. WebDAV (streams to disk, returns Path)
    pdf = await get_pdf_from_webdav(att_key)
    if pdf:
        result["pdf_path"] = pdf
        result["source"] = "zotero_webdav_pdf"
        return result

    result["source"] = "zotero_metadata_only"
    return result


async def get_paper_from_zotero_by_key(key: str) -> dict | None:
    """Retrieve a paper by its Zotero item key (not DOI).

    Mirrors ``get_paper_from_zotero`` but skips the DOI index — used as a
    fallback for Zotero-only items that have no DOI.
    """
    if zotero_sqlite.sqlite_config.available:
        try:
            sqlite_item = await zotero_sqlite.search_by_key(key)
            if sqlite_item:
                content = await zotero_sqlite.get_paper_content(sqlite_item)
                if not content.has_content:
                    content.source = content.source or "sqlite_metadata_only"
                return content.to_zotero_result()
        except Exception as e:
            logger.debug("SQLite by-key retrieval failed, falling back to API: %s", e)

    # API fallback — synthesize an index entry for the existing helpers
    item = await _get_item(key)
    if not item:
        return None

    item_data = item.get("data", {})
    result = {
        "found": True, "item_key": key,
        "metadata": {k: item_data.get(k, "") for k in
                     ("title", "creators", "DOI", "date", "abstractNote",
                      "publicationTitle", "itemType", "url")},
        "text": None, "pdf_path": None, "source": None,
        "truncated": False, "indexed_pages": None, "total_pages": None,
    }

    entry = {"item_key": key, "attachment_key": None, "has_pdf": False}
    await _resolve_attachment(entry)
    att_key = entry.get("attachment_key")
    if not att_key:
        result["source"] = "zotero_metadata_only"
        return result

    ft = await get_fulltext(att_key)
    if ft and ft.get("content"):
        result["text"] = ft["content"]
        result["source"] = "zotero_fulltext"
        result["indexed_pages"] = ft.get("indexedPages")
        result["total_pages"] = ft.get("totalPages")
        ip = ft.get("indexedPages")
        tp = ft.get("totalPages")
        ic = ft.get("indexedChars")
        tc = ft.get("totalChars")
        if (ip and tp and ip < tp) or (ic and tc and ic < tc):
            result["truncated"] = True
        try:
            local_pdf = await get_pdf_from_local_storage(att_key)
            if local_pdf:
                result["pdf_path"] = local_pdf
        except Exception as _e:
            logger.debug("Local PDF lookup failed for %s: %s", att_key, _e)
        return result

    pdf = await get_pdf_from_local_storage(att_key)
    if pdf:
        result["pdf_path"] = pdf
        result["source"] = "zotero_local_pdf"
        return result

    pdf = await get_pdf_from_web_api(att_key)
    if pdf:
        result["pdf_path"] = pdf
        result["source"] = "zotero_web_pdf"
        return result

    pdf = await get_pdf_from_webdav(att_key)
    if pdf:
        result["pdf_path"] = pdf
        result["source"] = "zotero_webdav_pdf"
        return result

    result["source"] = "zotero_metadata_only"
    return result


# ---------------------------------------------------------------------------
# Library search — SQLite first (covers ALL libraries), then API fallback
# ---------------------------------------------------------------------------

async def search_zotero(
    query: str, limit: int = 10,
    start_year: int | None = None, end_year: int | None = None,
) -> list[dict]:
    """Search by keyword across ALL libraries (user + groups).

    SQLite backend (preferred): searches title, creators, abstract, tags,
    DOI, and the fulltext word index — across all libraries.

    API fallback: q param only searches title/creator/year (and fulltext
    if qmode=everything). Does NOT search DOI field. Only searches the
    configured library (user or single group).
    """
    # ── SQLite path (fastest, searches everything) ───────────────────
    if zotero_sqlite.sqlite_config.available:
        try:
            results = await zotero_sqlite.search_items(
                query, limit=limit, include_groups=True,
                start_year=start_year, end_year=end_year,
            )
            if results:
                return [r.to_search_result() for r in results]
        except Exception as e:
            logger.warning("SQLite search failed, falling back to API: %s", e)

    # ── API fallback ─────────────────────────────────────────────────
    items = []
    resp = await _local_api_get("/items", {
        "format": "json", "q": query, "qmode": "everything",
        "itemType": "-attachment -note -annotation", "limit": limit})
    if resp:
        items = resp.json()
    if not items:
        resp = await _web_api_get("/items", {
            "format": "json", "q": query, "qmode": "everything",
            "itemType": "-attachment -note -annotation", "limit": limit})
        if resp:
            items = resp.json()

    # Also search group libraries via web API (concurrent)
    if zot_config.web_api_available:
        try:
            group_ids = await _fetch_user_group_ids()
            # The web API does not support field-prefix syntax (e.g. "author:X").
            # Strip known prefixes so the query degrades gracefully.
            import re as _re
            api_query = _re.sub(r'\b\w+:', '', query).strip()

            async def _search_group(gid: int) -> list:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(
                            f"https://api.zotero.org/groups/{gid}/items",
                            params={
                                "format": "json", "q": api_query,
                                "qmode": "everything",
                                "itemType": "-attachment -note -annotation",
                                "limit": limit,
                            },
                            headers={"Zotero-API-Key": zot_config.api_key},
                        )
                        if resp.status_code == 200:
                            return resp.json()
                        logger.debug("Group %s search returned %s", gid, resp.status_code)
                except httpx.RequestError as e:
                    logger.debug("Group %s request error: %s", gid, e)
                return []

            group_results = await asyncio.gather(*[_search_group(gid) for gid in group_ids])
            for batch in group_results:
                items.extend(batch)
        except Exception as e:
            logger.debug("Group search failed: %s", e)
    return [
        {k: item.get("data", {}).get(k, "") for k in
         ("key", "title", "creators", "DOI", "url", "date", "abstractNote",
          "publicationTitle", "itemType")}
        for item in items
    ]


# ---------------------------------------------------------------------------
# Connection diagnostics
# ---------------------------------------------------------------------------

async def check_connections() -> dict:
    """Test which Zotero backends are reachable."""
    # SQLite status (preferred backend)
    sqlite_status = await zotero_sqlite.check_status()

    status = {
        "sqlite":    {"configured": bool(zotero_sqlite.sqlite_config.db_path),
                      "reachable": sqlite_status.get("available", False),
                      "path": sqlite_status.get("db_path", ""),
                      "total_items": sqlite_status.get("total_items", 0),
                      "libraries": sqlite_status.get("library_count", 0),
                      "groups": sqlite_status.get("group_count", 0)},
        "local_api": {"configured": zot_config.local_enabled, "reachable": False,
                      "host": f"{zot_config.local_host}:{zot_config.local_port}"},
        "web_api":   {"configured": zot_config.web_api_available, "reachable": False},
        "webdav":    {"configured": zot_config.webdav_available, "reachable": False},
    }

    if zot_config.local_enabled:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"http://{zot_config.local_host}:{zot_config.local_port}/connector/ping")
                status["local_api"]["reachable"] = resp.status_code == 200
        except httpx.RequestError:
            pass

    if zot_config.web_api_available:
        resp = await _web_api_get("/items", {"limit": 1, "format": "json"})
        status["web_api"]["reachable"] = resp is not None

    if zot_config.webdav_available:
        try:
            auth = (zot_config.webdav_user, zot_config.webdav_pass) if zot_config.webdav_user else None
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.request("OPTIONS", zot_config.webdav_url, auth=auth)
                status["webdav"]["reachable"] = resp.status_code in (200, 204, 207)
        except httpx.RequestError:
            pass

    return status
