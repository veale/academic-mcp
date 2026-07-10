"""core.zotero_save — add external search results to the Zotero library.

``AUTO_IMPORT_TO_ZOTERO`` covers PDFs the server fetched for you. It does not
cover the far more common case: you ran a search, three of the external hits
look worth keeping, and you want them in your library without leaving the
conversation.

This writes metadata-only items (no PDF) through the Zotero local API, which
is the same write path :mod:`zotero_import` already uses and which requires
Zotero desktop to be running. Metadata is enriched from Crossref when the DOI
resolves; otherwise whatever the caller supplies is used as-is.

Items already in the library are reported as skipped rather than duplicated —
this is a write to the user's own library, and a silent duplicate is worse
than a no-op.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Zotero's local API is a single-process HTTP server; hammering it with
# concurrent writes is a good way to get 500s back.
_WRITE_CONCURRENCY = 2

# Guard against a runaway loop dumping hundreds of items into a library.
MAX_ITEMS = 25


class SavedItem(BaseModel):
    doi: str | None = None
    title: str | None = None
    status: str  # "saved" | "skipped" | "failed"
    zotero_key: str | None = None
    reason: str | None = None


class SaveResult(BaseModel):
    items: list[SavedItem] = Field(default_factory=list)
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def _save_one(entry: dict) -> SavedItem:
    """Enrich, build, and create a single Zotero item."""
    from .. import zotero_import
    from ..http_client import get_client

    doi = (entry.get("doi") or "").strip() or None
    title = (entry.get("title") or "").strip() or None

    if not doi and not title:
        return SavedItem(status="failed", reason="needs a doi or a title")

    if doi:
        existing = await zotero_import._doi_exists_in_zotero(doi)
        if existing:
            return SavedItem(
                doi=doi,
                title=existing.get("title") or title,
                status="skipped",
                zotero_key=existing.get("key"),
                reason="already in library",
            )

    crossref_meta = None
    if doi:
        crossref_meta = await zotero_import._fetch_crossref_metadata(doi, get_client())

    # Fall back to the caller's metadata for anything Crossref didn't answer.
    cached_meta = {
        "title": title,
        "authors": entry.get("authors") or [],
        "year": str(entry.get("year") or ""),
        "venue": entry.get("venue") or "",
    }

    item_type = zotero_import._resolve_zotero_item_type(
        crossref_meta,
        openalex_type=entry.get("work_type"),
    )
    payload = zotero_import._build_zotero_item(
        doi=doi or "",
        item_type=item_type,
        crossref_meta=crossref_meta,
        cached_meta=cached_meta,
        crossref_incomplete=crossref_meta is None and bool(doi),
    )
    if not doi:
        payload.pop("DOI", None)
    if not payload.get("title"):
        return SavedItem(doi=doi, status="failed", reason="no title could be resolved")

    key, error, _status = await zotero_import._create_zotero_item(payload)
    if not key:
        return SavedItem(
            doi=doi,
            title=payload.get("title"),
            status="failed",
            reason=zotero_import._friendly_import_error(error or "unknown error"),
        )

    return SavedItem(
        doi=doi,
        title=payload.get("title"),
        status="saved",
        zotero_key=key,
    )


async def save_items(items: list[dict]) -> SaveResult:
    """Save *items* to the Zotero library.

    Each entry may carry ``doi``, ``title``, ``authors``, ``year``, ``venue``,
    and ``work_type``. A DOI alone is enough — everything else is pulled from
    Crossref.
    """
    from .. import zotero

    if not items:
        return SaveResult(error="No items supplied.")
    if len(items) > MAX_ITEMS:
        return SaveResult(
            error=f"Too many items ({len(items)}); {MAX_ITEMS} is the per-call limit."
        )

    if not zotero.zot_config.local_enabled:
        return SaveResult(
            error=(
                "Writing to Zotero needs the local API, which is disabled. "
                "Set ZOTERO_LOCAL_ENABLED=true and make sure Zotero desktop is "
                "running with 'Allow other applications on this computer to "
                "communicate with Zotero' enabled."
            )
        )

    sem = asyncio.Semaphore(_WRITE_CONCURRENCY)

    async def _guarded(entry: dict) -> SavedItem:
        async with sem:
            try:
                return await _save_one(entry)
            except Exception as e:
                logger.warning("Zotero save failed for %s: %s", entry.get("doi"), e)
                return SavedItem(
                    doi=entry.get("doi"),
                    title=entry.get("title"),
                    status="failed",
                    reason=str(e),
                )

    saved_items = await asyncio.gather(*(_guarded(e) for e in items))

    if any(i.status == "saved" for i in saved_items):
        # The DOI index and both indexes now disagree with the library.
        zotero.invalidate_doi_index()

    return SaveResult(
        items=list(saved_items),
        saved=sum(1 for i in saved_items if i.status == "saved"),
        skipped=sum(1 for i in saved_items if i.status == "skipped"),
        failed=sum(1 for i in saved_items if i.status == "failed"),
    )
