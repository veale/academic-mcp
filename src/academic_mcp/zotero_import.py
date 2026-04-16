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
import logging
import random
import re
import shutil
import string
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import config
from .text_cache import CachedArticle
from .zotero import zot_config, _local_api_get
from . import zotero_sqlite

logger = logging.getLogger(__name__)


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

async def _create_zotero_item(item_data: dict) -> "str | None":
    """Create a new item in Zotero via the local API. Returns the item key."""
    if not zot_config.local_enabled:
        return None
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
                    return successful["0"].get("key")
                if isinstance(result, list) and result:
                    return result[0].get("key")
            else:
                logger.warning(
                    "Zotero item creation failed: %d %s",
                    resp.status_code, resp.text[:200],
                )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.debug("Zotero local API not reachable for item creation")
    except Exception as e:
        logger.warning("Unexpected error creating Zotero item: %s", e)
    return None


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
        return False

    if not doi:
        logger.debug("Skipping Zotero import: no DOI")
        return False

    if not _is_web_fetched(cached_article.source):
        logger.debug("Skipping Zotero import for %s: source is Zotero", doi)
        return False

    if not zot_config.local_enabled:
        logger.debug("Skipping Zotero import: local API disabled")
        return False

    if not pdf_path.exists():
        logger.debug("Skipping Zotero import for %s: PDF no longer on disk", doi)
        return False

    # Duplicate check
    existing = await _doi_exists_in_zotero(doi)
    if existing:
        item_key = existing.get("key")
        if item_key:
            if not await _item_has_pdf_attachment(item_key):
                logger.info("DOI %s in Zotero without PDF — attaching", doi)
                success = await _attach_pdf_to_item(item_key, pdf_path)
                if success:
                    _try_delete_cached_pdf(doi)
                return success
        logger.debug("DOI %s already in Zotero with PDF — skipping", doi)
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
    item_key = await _create_zotero_item(item_data)
    if not item_key:
        logger.warning("Failed to create Zotero item for %s", doi)
        return False

    # Attach the PDF
    filename = _generate_pdf_filename(item_data)
    success = await _attach_pdf_to_item(item_key, pdf_path, filename)

    if success:
        logger.info("Imported %s into Zotero as %s (%s)", doi, item_key, item_type)
        _try_delete_cached_pdf(doi)
    else:
        logger.warning(
            "Created Zotero item %s but PDF attachment failed for %s", item_key, doi
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


_import_queue: deque[_ImportJob] = deque()
_import_task: "asyncio.Task | None" = None
_IMPORT_DELAY_SECONDS = 5  # debounce: wait this long after last enqueue


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

    _import_queue.append(_ImportJob(doi=doi, pdf_path=pdf_path, cached_article=cached_article))
    _schedule_drain()


def _schedule_drain() -> None:
    """Schedule (or reschedule) the background drain task."""
    global _import_task

    if _import_task and not _import_task.done():
        _import_task.cancel()

    try:
        _import_task = asyncio.ensure_future(_drain_after_delay())
    except RuntimeError:
        # No running event loop (e.g. during testing) — skip
        pass


async def _drain_after_delay() -> None:
    """Wait for the debounce period, then process all queued imports."""
    await asyncio.sleep(_IMPORT_DELAY_SECONDS)

    while _import_queue:
        job = _import_queue.popleft()
        try:
            await import_to_zotero(job.doi, job.pdf_path, job.cached_article)
        except Exception as e:
            logger.warning("Zotero auto-import failed for %s: %s", job.doi, e)
