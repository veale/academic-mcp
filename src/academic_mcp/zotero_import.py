"""Auto-import web-fetched PDFs into the local Zotero library.

Design principles:
- The .article.json text cache is always kept — pymupdf4llm extraction with
  section boundaries is far better than Zotero's .zotero-ft-cache.
- Only the PDF file moves to Zotero (copied to ~/Zotero/storage/).
- Only papers fetched from the web are eligible; Zotero-sourced papers are
  already in the library.
- Import is non-blocking — it never delays a response to the LLM.
- Feature is opt-in via AUTO_IMPORT_TO_ZOTERO=true.
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import shutil
import string
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import httpx

from .config import config
from .text_cache import CachedArticle
from .zotero import zot_config, _local_api_get
from . import zotero_sqlite

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


# ---------------------------------------------------------------------------
# Runtime diagnostics (last N import attempts)
# ---------------------------------------------------------------------------

_MAX_IMPORT_ATTEMPTS = 50
_import_attempts: deque[dict] = deque(maxlen=_MAX_IMPORT_ATTEMPTS)
_latest_attempt_by_doi: dict[str, dict] = {}
_latest_error_by_doi: dict[str, str] = {}


def _record_attempt(
    doi: str,
    stage: str,
    status: str,
    error: str = "",
    details: dict | None = None,
) -> None:
    entry = {
        "timestamp": _iso_utc(),
        "doi": doi,
        "stage": stage,
        "status": status,
        "error": error,
        "details": details or {},
    }
    _import_attempts.append(entry)
    _latest_attempt_by_doi[doi] = entry
    if status == "error" and error:
        _latest_error_by_doi[doi] = error


def _friendly_import_error(raw: str) -> str:
    lower = (raw or "").lower()
    if "403" in lower or "405" in lower:
        return (
            "403/405 write access denied - enable Zotero local API write access "
            "in Zotero Settings > Advanced > Config Editor: "
            "extensions.zotero.httpServer.localAPI.allowWriteAccess=true"
        )
    if "not reachable" in lower or "connect" in lower:
        return "Zotero local API not reachable - ensure Zotero desktop is running"
    if "local api disabled" in lower:
        return "Zotero local API disabled"
    return raw or "Unknown auto-import failure"


async def get_import_status(limit: int = 20) -> dict:
    """Return queue + diagnostics state for MCP status tooling."""
    recent = list(_import_attempts)[-max(1, min(limit, _MAX_IMPORT_ATTEMPTS)) :]
    queue_count = await _queue_count()
    return {
        "auto_import_enabled": config.auto_import_to_zotero,
        "local_api_enabled": zot_config.local_enabled,
        "write_probe": dict(_write_probe_result),
        "queue_count": queue_count,
        "recent_attempts": recent,
    }


def get_auto_import_hint(doi: str) -> str | None:
    """Return a one-line hint when the most recent import state warrants surfacing."""
    if not config.auto_import_to_zotero or not doi:
        return None

    if _write_probe_result.get("state") == "denied":
        msg = _write_probe_result.get("message") or "write access not authorized"
        return f"[auto-import blocked: {msg}]"

    last = _latest_attempt_by_doi.get(doi)
    if not last:
        return None
    if last.get("status") != "error":
        return None

    try:
        ts = datetime.fromisoformat(last.get("timestamp", ""))
        if _utc_now() - ts > timedelta(minutes=15):
            return None
    except Exception:
        pass

    return f"[auto-import failed: {_friendly_import_error(last.get('error', ''))}]"


# ---------------------------------------------------------------------------
# Startup write probe
# ---------------------------------------------------------------------------

# Probe outcome is tri-state:
#   state = "denied"       -> writes are definitely not authorised
#   state = "unreachable"  -> Zotero desktop / local API not running
#   state = "unknown"      -> endpoint responded but we can't prove writes work
#                             without creating an item (we refuse to do so)
# `ok` is True only when we know writes are denied-free (currently always None
# unless state == "unknown" and we treat it as don't-surface). Do not flag
# failure in the LLM-facing footer unless state == "denied".
_write_probe_result: dict = {
    "checked_at": None,
    "state": "unchecked",
    "status_code": None,
    "message": "not checked",
}
_write_probe_ran = False


async def ensure_auto_import_initialized() -> None:
    """Run startup checks once and ensure the persistent worker is running."""
    global _write_probe_ran
    if not config.auto_import_to_zotero:
        return

    if not _write_probe_ran:
        _write_probe_ran = True
        await _probe_local_write_access()

    _ensure_worker_running()


async def _probe_local_write_access() -> None:
    """Probe whether local API writes are authorized, without creating any item.

    We POST an intentionally empty batch (``[]``). Zotero returns:
      * 403 / 405 when writes are forbidden -> state="denied" (actionable)
      * any other status (400 / 200 / etc.) -> state="unknown" — the endpoint
        is reachable but an empty batch can't *prove* writes are authorised.
        We do not surface this as a failure; real POSTs will either succeed or
        fall through to the standard per-request error handling.
    """
    _write_probe_result.update({
        "checked_at": _iso_utc(),
        "state": "unchecked",
        "status_code": None,
        "message": "not checked",
    })

    if not zot_config.local_enabled:
        _write_probe_result.update({
            "state": "denied",
            "message": "local API disabled",
        })
        logger.warning("AUTO_IMPORT_TO_ZOTERO=true but local API is disabled")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{zot_config.local_base}/items",
                json=[],
                headers={
                    "Content-Type": "application/json",
                    "Zotero-Allowed-Request": "1",
                },
            )
        _write_probe_result["status_code"] = resp.status_code
        if resp.status_code in (403, 405):
            _write_probe_result.update({
                "state": "denied",
                "message": (
                    "write access denied - enable Zotero local API write access "
                    "(extensions.zotero.httpServer.localAPI.allowWriteAccess=true)"
                ),
            })
            logger.warning(
                "Zotero auto-import is enabled but local API writes are not authorized "
                "(status %s). Enable extensions.zotero.httpServer.localAPI.allowWriteAccess=true",
                resp.status_code,
            )
            return
        _write_probe_result.update({
            "state": "unknown",
            "message": (
                f"endpoint reachable (status {resp.status_code}) — authorisation state "
                "cannot be proved without creating an item; will surface on first real import"
            ),
        })
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _write_probe_result.update({
            "state": "unreachable",
            "message": "Zotero local API not reachable",
        })
    except Exception as e:
        _write_probe_result.update({
            "state": "unknown",
            "message": f"write probe error: {e}",
        })


# ---------------------------------------------------------------------------
# Source eligibility
# ---------------------------------------------------------------------------

def _is_web_fetched(source: str) -> bool:
    """Return True if this article was fetched from the web, not from Zotero."""
    source_lower = source.lower()
    return not any(source_lower.startswith(prefix) for prefix in (
        "zotero_", "sqlite_",
    ))


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

async def _doi_exists_in_zotero(doi: str) -> "dict | None":
    """Check if a DOI is already in Zotero. Returns the item if found, else None.

    Uses the SQLite backend first (instant, no running Zotero needed),
    then falls back to the local API search.
    """
    # SQLite (preferred — searches all libraries without requiring Zotero to run)
    if zotero_sqlite.sqlite_config.available:
        try:
            result = await zotero_sqlite.search_by_doi(doi)
            if result:
                return {"key": result.key, "type": result.itemType, "title": result.title}
        except Exception as e:
            logger.debug("SQLite DOI check failed for %s: %s", doi, e)

    # Local API fallback (requires Zotero desktop running)
    resp = await _local_api_get("/items", {
        "q": doi,
        "format": "json",
        "limit": "5",
    })
    if resp:
        try:
            items = resp.json()
            doi_clean = doi.lower().strip()
            for item in items:
                item_doi = (item.get("data") or {}).get("DOI", "").lower().strip()
                if item_doi and item_doi == doi_clean:
                    data = item.get("data", {})
                    return {"key": data.get("key"), "type": data.get("itemType"), "title": data.get("title")}
        except Exception as e:
            logger.debug("Local API DOI check failed for %s: %s", doi, e)

    return None


async def _item_has_pdf_attachment(item_key: str) -> bool:
    """Check if a Zotero item already has a PDF attachment."""
    resp = await _local_api_get(f"/items/{item_key}/children", {"format": "json"})
    if not resp:
        return False
    try:
        children = resp.json()
        return any(
            (child.get("data") or {}).get("contentType") == "application/pdf"
            for child in children
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Metadata enrichment from Crossref
# ---------------------------------------------------------------------------

async def _fetch_crossref_metadata(doi: str, client: httpx.AsyncClient) -> "dict | None":
    """Fetch rich bibliographic metadata from Crossref for a DOI."""
    try:
        resp = await client.get(
            f"https://api.crossref.org/works/{doi}",
            headers={
                "User-Agent": (
                    f"academic-mcp/1.0 "
                    f"(mailto:{config.unpaywall_email or 'user@example.com'})"
                ),
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None

        work = resp.json().get("message") or {}

        return {
            "crossref_type": work.get("type", ""),
            "title": (work.get("title") or [""])[0],
            "container_title": (work.get("container-title") or [""])[0],
            "authors": work.get("author") or [],
            "year": str(
                (work.get("published") or work.get("issued") or {})
                .get("date-parts", [[None]])[0][0] or ""
            ),
            "volume": work.get("volume", ""),
            "issue": work.get("issue", ""),
            "pages": work.get("page", ""),
            "publisher": work.get("publisher", ""),
            "issn": (work.get("ISSN") or [""])[0],
            "isbn": (work.get("ISBN") or [""])[0],
            "abstract": work.get("abstract", ""),
            "doi": doi,
            "url": work.get("URL", f"https://doi.org/{doi}"),
            "language": work.get("language", ""),
            "event_name": (work.get("event") or {}).get("name", ""),
            "edition": work.get("edition-number", ""),
        }
    except Exception as e:
        logger.warning("Crossref metadata fetch failed for %s: %s", doi, e)
        return None


# ---------------------------------------------------------------------------
# Item type mapping
# ---------------------------------------------------------------------------

_CROSSREF_TO_ZOTERO_TYPE: dict[str, str] = {
    "journal-article":     "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter":        "bookSection",
    "book":                "book",
    "monograph":           "book",
    "edited-book":         "book",
    "reference-book":      "book",
    "report":              "report",
    "dissertation":        "thesis",
    "posted-content":      "preprint",
    "dataset":             "document",
    "peer-review":         "journalArticle",
    "component":           "journalArticle",
    "reference-entry":     "encyclopediaArticle",
    "report-series":       "report",
    "other":               "document",
    "standard":            "standard",
}

_OPENALEX_TO_ZOTERO_TYPE: dict[str, str] = {
    "article":      "journalArticle",
    "book":         "book",
    "book-chapter": "bookSection",
    "dataset":      "document",
    "dissertation": "thesis",
    "editorial":    "journalArticle",
    "erratum":      "journalArticle",
    "letter":       "journalArticle",
    "paratext":     "document",
    "peer-review":  "journalArticle",
    "preprint":     "preprint",
    "review":       "journalArticle",
    "standard":     "standard",
    "other":        "document",
}


def _resolve_zotero_item_type(
    crossref_meta: "dict | None",
    openalex_type: "str | None" = None,
    source: str = "",
) -> str:
    """Determine the Zotero item type from available metadata."""
    if crossref_meta and crossref_meta.get("crossref_type"):
        ztype = _CROSSREF_TO_ZOTERO_TYPE.get(crossref_meta["crossref_type"], "document")
        # Refine: conference proceedings with an event name
        if ztype == "journalArticle" and crossref_meta.get("event_name"):
            ztype = "conferencePaper"
        return ztype

    if openalex_type:
        return _OPENALEX_TO_ZOTERO_TYPE.get(openalex_type, "document")

    # Heuristic fallback
    src = source.lower()
    if "arxiv" in src or "biorxiv" in src or "medrxiv" in src or "ssrn" in src:
        return "preprint"

    return "journalArticle"


# ---------------------------------------------------------------------------
# Build Zotero item JSON
# ---------------------------------------------------------------------------

def _build_zotero_item(
    doi: str,
    item_type: str,
    crossref_meta: "dict | None",
    cached_meta: "dict | None",
    crossref_incomplete: bool = False,
) -> dict:
    """Build a Zotero item JSON payload from available metadata."""
    cr = crossref_meta or {}
    cm = cached_meta or {}

    # Strip JATS XML tags from Crossref abstracts
    abstract = cr.get("abstract", "")
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()

    tags = [{"tag": "auto-imported"}]
    if crossref_incomplete:
        tags.append({"tag": "metadata-incomplete"})

    item: dict = {
        "itemType": item_type,
        "title": cr.get("title") or cm.get("title", ""),
        "DOI": doi,
        "url": cr.get("url") or f"https://doi.org/{doi}",
        "accessDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "abstractNote": abstract,
        "tags": tags,
        "creators": [],
    }

    # Authors
    if cr.get("authors"):
        for author in cr["authors"]:
            creator: dict = {"creatorType": "author"}
            if author.get("family"):
                creator["lastName"] = author["family"]
                creator["firstName"] = author.get("given", "")
            elif author.get("name"):
                creator["name"] = author["name"]
            else:
                continue
            item["creators"].append(creator)
    elif cm.get("authors"):
        for name in cm["authors"]:
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                item["creators"].append({
                    "creatorType": "author",
                    "firstName": parts[0],
                    "lastName": parts[1],
                })
            elif parts[0]:
                item["creators"].append({
                    "creatorType": "author",
                    "lastName": parts[0],
                    "firstName": "",
                })

    # Date
    item["date"] = cr.get("year") or cm.get("year", "")

    # Type-specific fields
    if item_type == "journalArticle":
        item["publicationTitle"] = cr.get("container_title") or cm.get("venue", "")
        item["volume"] = cr.get("volume", "")
        item["issue"] = cr.get("issue", "")
        item["pages"] = cr.get("pages", "")
        item["ISSN"] = cr.get("issn", "")
        item["language"] = cr.get("language", "")

    elif item_type == "conferencePaper":
        item["conferenceName"] = cr.get("event_name", "")
        item["proceedingsTitle"] = cr.get("container_title") or cm.get("venue", "")
        item["pages"] = cr.get("pages", "")
        item["publisher"] = cr.get("publisher", "")

    elif item_type == "bookSection":
        item["bookTitle"] = cr.get("container_title", "")
        item["pages"] = cr.get("pages", "")
        item["publisher"] = cr.get("publisher", "")
        item["ISBN"] = cr.get("isbn", "")
        item["edition"] = cr.get("edition", "")

    elif item_type == "book":
        item["publisher"] = cr.get("publisher", "")
        item["ISBN"] = cr.get("isbn", "")
        item["edition"] = cr.get("edition", "")
        item["numPages"] = cr.get("pages", "")

    elif item_type == "preprint":
        item["repository"] = cr.get("container_title") or cm.get("venue", "")

    elif item_type == "thesis":
        item["university"] = cr.get("publisher", "")
        item["thesisType"] = ""

    elif item_type == "report":
        item["institution"] = cr.get("publisher", "")
        item["reportType"] = ""

    else:
        item["publicationTitle"] = cr.get("container_title") or cm.get("venue", "")
        item["publisher"] = cr.get("publisher", "")

    # Drop empty string values to keep the payload clean
    return {k: v for k, v in item.items() if v != "" and v != []}


# ---------------------------------------------------------------------------
# Local API: create item and attach PDF
# ---------------------------------------------------------------------------

async def _create_zotero_item(item_data: dict) -> "tuple[str | None, str, int | None]":
    """Create a new item in Zotero via the local API.

    Returns ``(item_key, error_message, status_code)``.
    """
    if not zot_config.local_enabled:
        return None, "local API disabled", None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{zot_config.local_base}/items",
                json=[item_data],
                headers={
                    "Content-Type": "application/json",
                    "Zotero-Allowed-Request": "1",
                },
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                successful = result.get("successful") or {}
                if "0" in successful:
                    return successful["0"].get("key"), "", resp.status_code
                if isinstance(result, list) and result:
                    return result[0].get("key"), "", resp.status_code
            else:
                msg = f"{resp.status_code} {resp.text[:200]}"
                logger.warning("Zotero item creation failed: %s", msg)
                return None, msg, resp.status_code
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.debug("Zotero local API not reachable for item creation")
        return None, "Zotero local API not reachable", None
    except Exception as e:
        logger.warning("Unexpected error creating Zotero item: %s", e)
        return None, str(e), None
    return None, "unknown item creation failure", None


def _generate_zotero_key() -> str:
    """Generate an 8-character Zotero-style alphanumeric item key."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=8))


async def _attach_pdf_to_item(
    parent_key: str,
    pdf_path: Path,
    filename: "str | None" = None,
) -> bool:
    """Copy a PDF into Zotero storage and create an attachment item.

    Steps:
    1. Generate a new 8-char key for the attachment.
    2. Create ~/Zotero/storage/{KEY}/ directory.
    3. Copy the PDF into that directory.
    4. POST an attachment item to the local API as a child of parent_key.
    """
    if not zot_config.local_enabled:
        return False

    storage_base = Path(zot_config.local_storage_path)
    if not storage_base.is_dir():
        logger.debug("Zotero storage dir not found: %s", storage_base)
        return False

    if not pdf_path.exists():
        logger.debug("PDF no longer on disk, cannot attach: %s", pdf_path)
        return False

    att_key = _generate_zotero_key()
    att_dir = storage_base / att_key

    # Build a safe filename
    raw_name = filename or pdf_path.name
    safe_name = "".join(c for c in raw_name if c.isalnum() or c in " .-_,").strip()
    if not safe_name:
        safe_name = "paper"
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"

    try:
        att_dir.mkdir(parents=True, exist_ok=True)
        dest = att_dir / safe_name
        shutil.copy2(pdf_path, dest)
    except OSError as e:
        logger.warning("Failed to copy PDF to Zotero storage: %s", e)
        return False

    attachment_data = {
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_file",
        "title": safe_name,
        "contentType": "application/pdf",
        "charset": "",
        "filename": safe_name,
        "tags": [],
        "relations": {},
        "path": f"storage:{att_key}/{safe_name}",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{zot_config.local_base}/items",
                json=[attachment_data],
                headers={
                    "Content-Type": "application/json",
                    "Zotero-Allowed-Request": "1",
                },
            )
            if resp.status_code in (200, 201):
                logger.info(
                    "Attached PDF to Zotero item %s → storage/%s/%s",
                    parent_key, att_key, safe_name,
                )
                return True
            else:
                logger.warning(
                    "Attachment creation failed: %d %s",
                    resp.status_code, resp.text[:200],
                )
                shutil.rmtree(att_dir, ignore_errors=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.debug("Zotero local API not reachable for PDF attachment")
        shutil.rmtree(att_dir, ignore_errors=True)
    except Exception as e:
        logger.warning("Unexpected error attaching PDF to Zotero: %s", e)
        shutil.rmtree(att_dir, ignore_errors=True)

    return False


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------

def _generate_pdf_filename(item_data: dict) -> str:
    """Generate 'AuthorLastName - Year - Title.pdf' from Zotero item data."""
    parts = []
    creators = item_data.get("creators") or []
    if creators:
        first = creators[0]
        name = first.get("lastName") or first.get("name", "Unknown")
        if len(creators) > 1:
            name += " et al."
        parts.append(name)

    year = (item_data.get("date") or "")[:4]
    if year:
        parts.append(year)

    title = (item_data.get("title") or "")[:80]
    if title:
        parts.append(title)

    filename = " - ".join(parts) if parts else "paper"
    filename = "".join(c for c in filename if c.isalnum() or c in " .-_,").strip()
    return f"{filename}.pdf"


# ---------------------------------------------------------------------------
# Cached PDF cleanup
# ---------------------------------------------------------------------------

def _try_delete_cached_pdf(doi: str) -> None:
    """Delete the cached PDF file (not the .article.json) to save space.

    The .article.json is always kept because our extraction pipeline
    (pymupdf4llm + section detection) is far better than Zotero's ft-cache.
    """
    doi_hash = hashlib.sha256(doi.lower().strip().encode()).hexdigest()[:16]
    pdf_path = config.pdf_cache_dir / f"{doi_hash}.pdf"
    try:
        if pdf_path.exists():
            pdf_path.unlink()
            logger.debug("Deleted cached PDF for %s (now in Zotero)", doi)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Import orchestrator
# ---------------------------------------------------------------------------

async def import_to_zotero(
    doi: str,
    pdf_path: Path,
    cached_article: CachedArticle,
) -> bool:
    """Import a web-fetched PDF into Zotero with full metadata.

    Returns True if the import succeeded or was skipped (already imported),
    False on failure.
    """
    if not config.auto_import_to_zotero:
        _record_attempt(doi, "guard", "skipped", "auto-import disabled")
        return False

    if not doi:
        logger.debug("Skipping Zotero import: no DOI")
        _record_attempt(doi, "guard", "skipped", "no DOI")
        return False

    if not _is_web_fetched(cached_article.source):
        logger.debug("Skipping Zotero import for %s: source is Zotero", doi)
        _record_attempt(doi, "guard", "skipped", "source is Zotero")
        return False

    if not zot_config.local_enabled:
        logger.debug("Skipping Zotero import: local API disabled")
        _record_attempt(doi, "guard", "error", "local API disabled")
        return False

    if not pdf_path.exists():
        logger.debug("Skipping Zotero import for %s: PDF no longer on disk", doi)
        _record_attempt(doi, "guard", "error", "PDF missing from cache")
        return False

    _record_attempt(doi, "start", "queued", details={"pdf_path": str(pdf_path)})

    # Duplicate check
    existing = await _doi_exists_in_zotero(doi)
    if existing:
        item_key = existing.get("key")
        if item_key:
            if not await _item_has_pdf_attachment(item_key):
                logger.info("DOI %s in Zotero without PDF — attaching", doi)
                success = await _attach_pdf_to_item(item_key, pdf_path)
                if success:
                    _record_attempt(doi, "attach_existing", "success")
                    _try_delete_cached_pdf(doi)
                return success
        logger.debug("DOI %s already in Zotero with PDF — skipping", doi)
        _record_attempt(doi, "duplicate", "skipped", "already in Zotero")
        return True

    # Fetch rich metadata from Crossref
    crossref_meta = None
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        crossref_meta = await _fetch_crossref_metadata(doi, client)

    crossref_incomplete = crossref_meta is None

    # Determine item type
    item_type = _resolve_zotero_item_type(
        crossref_meta,
        source=cached_article.source,
    )

    # Build the Zotero item payload
    item_data = _build_zotero_item(
        doi, item_type, crossref_meta, cached_article.metadata,
        crossref_incomplete=crossref_incomplete,
    )

    # Create the item via local API
    item_key, create_error, _ = await _create_zotero_item(item_data)
    if not item_key:
        logger.warning("Failed to create Zotero item for %s", doi)
        _record_attempt(doi, "create_item", "error", create_error or "create item failed")
        return False

    # Attach the PDF
    filename = _generate_pdf_filename(item_data)
    success = await _attach_pdf_to_item(item_key, pdf_path, filename)

    if success:
        logger.info("Imported %s into Zotero as %s (%s)", doi, item_key, item_type)
        _record_attempt(doi, "attach_pdf", "success", details={"item_key": item_key})
        _try_delete_cached_pdf(doi)
    else:
        logger.warning(
            "Created Zotero item %s but PDF attachment failed for %s", item_key, doi
        )
        _record_attempt(
            doi,
            "attach_pdf",
            "error",
            f"Created item {item_key} but attachment failed",
        )

    return success


# ---------------------------------------------------------------------------
# Background import queue
# ---------------------------------------------------------------------------

@dataclass
class _ImportJob:
    doi: str
    pdf_path: Path
    cached_article: CachedArticle


_IMPORT_DELAY_SECONDS = 5
_IMPORT_BACKOFF_BASE_SECONDS = 5
_IMPORT_BACKOFF_MAX_SECONDS = 300
_QUEUE_DB_PATH = config.pdf_cache_dir / "import_queue.sqlite"
_worker_task: "asyncio.Task | None" = None
_worker_wakeup: "asyncio.Event | None" = None


async def _ensure_queue_db() -> None:
    async with aiosqlite.connect(_QUEUE_DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS import_queue (
                doi TEXT PRIMARY KEY,
                pdf_path TEXT NOT NULL,
                source TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                queued_at TEXT NOT NULL,
                run_after TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.commit()


async def _queue_count() -> int:
    await _ensure_queue_db()
    async with aiosqlite.connect(_QUEUE_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) AS cnt FROM import_queue")
        row = await cur.fetchone()
        return int(row["cnt"] if row else 0)


async def _enqueue_job(job: _ImportJob) -> None:
    await _ensure_queue_db()
    run_after = _utc_now() + timedelta(seconds=_IMPORT_DELAY_SECONDS)
    async with aiosqlite.connect(_QUEUE_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO import_queue
                (doi, pdf_path, source, metadata_json, queued_at, run_after, attempts, last_error)
            VALUES (?, ?, ?, ?, ?, ?, 0, '')
            ON CONFLICT(doi) DO UPDATE SET
                pdf_path=excluded.pdf_path,
                source=excluded.source,
                metadata_json=excluded.metadata_json,
                queued_at=excluded.queued_at,
                run_after=excluded.run_after
            """,
            (
                job.doi,
                str(job.pdf_path),
                job.cached_article.source,
                json.dumps(job.cached_article.metadata or {}),
                _iso_utc(),
                _iso_utc(run_after),
            ),
        )
        await db.commit()


async def _get_due_job() -> dict | None:
    await _ensure_queue_db()
    async with aiosqlite.connect(_QUEUE_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT doi, pdf_path, source, metadata_json, queued_at, run_after, attempts, last_error
            FROM import_queue
            WHERE run_after <= ?
            ORDER BY run_after ASC
            LIMIT 1
            """,
            (_iso_utc(),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def _delete_job(doi: str) -> None:
    async with aiosqlite.connect(_QUEUE_DB_PATH) as db:
        await db.execute("DELETE FROM import_queue WHERE doi = ?", (doi,))
        await db.commit()


async def _reschedule_job(doi: str, attempts: int, last_error: str) -> None:
    backoff = min(
        _IMPORT_BACKOFF_MAX_SECONDS,
        _IMPORT_BACKOFF_BASE_SECONDS * (2 ** min(attempts, 6)),
    )
    run_after = _utc_now() + timedelta(seconds=backoff)
    async with aiosqlite.connect(_QUEUE_DB_PATH) as db:
        await db.execute(
            """
            UPDATE import_queue
            SET attempts = ?,
                last_error = ?,
                run_after = ?
            WHERE doi = ?
            """,
            (attempts, last_error, _iso_utc(run_after), doi),
        )
        await db.commit()


def enqueue_zotero_import(
    doi: str,
    pdf_path: Path,
    cached_article: CachedArticle,
) -> None:
    """Add a paper to the background import queue. Non-blocking.

    Call this from _cache_pdf_and_return after a successful web fetch.
    """
    if not config.auto_import_to_zotero:
        return
    if not doi:
        return
    if not _is_web_fetched(cached_article.source):
        return
    if not pdf_path.exists():
        return

    job = _ImportJob(doi=doi, pdf_path=pdf_path, cached_article=cached_article)
    _record_attempt(doi, "enqueue", "queued", details={"pdf_path": str(pdf_path)})

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_enqueue_job(job))
        _ensure_worker_running()
        if _worker_wakeup is not None:
            _worker_wakeup.set()
    except RuntimeError:
        # No running event loop (e.g. some unit tests)
        pass


def _ensure_worker_running() -> None:
    """Start the persistent queue worker once per process."""
    global _worker_task, _worker_wakeup

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return

    if _worker_wakeup is None:
        _worker_wakeup = asyncio.Event()

    if _worker_task and not _worker_task.done():
        return

    _worker_task = asyncio.create_task(_worker_loop())


async def _worker_loop() -> None:
    """Process persistent import jobs with retry/backoff."""
    await _ensure_queue_db()
    while True:
        try:
            job = await _get_due_job()
            if not job:
                if _worker_wakeup is None:
                    await asyncio.sleep(2)
                else:
                    _worker_wakeup.clear()
                    await asyncio.wait_for(_worker_wakeup.wait(), timeout=2.0)
                continue

            doi = job["doi"]
            pdf_path = Path(job["pdf_path"])
            metadata = json.loads(job.get("metadata_json") or "{}")
            cached_article = CachedArticle(
                doi=doi,
                text="",
                source=job.get("source") or "web",
                sections=[],
                section_detection="unknown",
                word_count=0,
                metadata=metadata,
            )

            ok = await import_to_zotero(doi, pdf_path, cached_article)
            if ok:
                await _delete_job(doi)
                continue

            attempts = int(job.get("attempts") or 0) + 1
            last_error = _latest_error_by_doi.get(doi, "auto-import failed")
            await _reschedule_job(doi, attempts, last_error)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.warning("Zotero auto-import worker error: %s", e)
            await asyncio.sleep(2)
