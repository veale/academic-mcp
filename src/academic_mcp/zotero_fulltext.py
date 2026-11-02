"""Bounded extraction of newly synced Zotero PDFs without a desktop mirror."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from . import pdf_work, text_cache, zotero, zotero_catalog, zotero_sqlite
from .config import config

logger = logging.getLogger(__name__)
_jobs = {}
_semaphore = None


def _needs_text(item):
    key = item["item_key"]
    if not item.get("_catalog") or not item.get("attachment_key"):
        return False
    cached = text_cache.get_cached(item.get("doi") or "zotero:" + key)
    if cached and cached.text:
        old_hash = cached.metadata.get("zotero_attachment_md5")
        return bool(
            old_hash
            and item.get("attachment_md5")
            and old_hash != item["attachment_md5"]
        )
    storage = getattr(zotero_sqlite.sqlite_config, "storage_path", "")
    return (
        not storage
        or not (Path(storage) / item["attachment_key"] / ".zotero-ft-cache").exists()
    )


async def _extract(item):
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(2)
    async with _semaphore:
        async with asyncio.timeout(120):
            attachment = item["attachment_key"]
            prefix = item.get("library_prefix") or zotero.zot_config.library_prefix
            if prefix.startswith("/users/") and zotero.zot_config.webdav_available:
                path = await zotero.get_pdf_from_webdav(attachment)
                source = "zotero_webdav_pdf"
            else:
                path = await zotero.get_pdf_from_web_api(attachment, prefix)
                source = "zotero_web_pdf"
            if not path:
                raise RuntimeError("Synced attachment file is not yet available")
            # An already cached PDF can predate a desktop annotation/revision.
            # Only this server's disposable download cache is refreshed.
            expected = item.get("attachment_md5")
            if expected:
                import hashlib

                def digest():
                    result = hashlib.md5(usedforsecurity=False)
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            result.update(chunk)
                    return result.hexdigest()

                if await asyncio.to_thread(digest) != expected:
                    path.unlink(missing_ok=True)
                    path = (
                        await zotero.get_pdf_from_webdav(attachment)
                        if source == "zotero_webdav_pdf"
                        else await zotero.get_pdf_from_web_api(attachment, prefix)
                    )
                    if not path or await asyncio.to_thread(digest) != expected:
                        raise RuntimeError(
                            "Attachment checksum differs from synced metadata"
                        )
            result = await pdf_work.extract(path, config.use_pymupdf4llm)
            metadata = {
                "title": item.get("title", ""),
                "authors": item.get("authors", []),
                "venue": item.get("venue", ""),
                "zotero_attachment_key": attachment,
                "zotero_attachment_md5": expected or "",
            }
            text_cache.put_cached(
                item.get("doi") or "zotero:" + item["item_key"],
                result["text"],
                source,
                sections=result.get("sections", []),
                metadata=metadata,
                section_detection=result.get("section_detection", "pdf_font_analysis"),
                pdf_path=str(path),
            )
            return len(result["text"].split())


async def hydrate(items):
    """Start a bounded batch; don't hold metadata indexing for slow file sync."""
    if not zotero_catalog.enabled() or os.getenv(
        "ZOTERO_FULLTEXT_SYNC_ENABLED", "true"
    ).lower() in ("false", "0"):
        return
    limit = max(0, int(os.getenv("ZOTERO_FULLTEXT_SYNC_LIMIT", "20")))
    selected = []
    for item in sorted(items, key=lambda x: x.get("dateModified", ""), reverse=True):
        if len(selected) >= limit:
            break
        key = item["item_key"]
        existing = _jobs.get(key)
        if existing:
            task, finished = existing
            if not task.done():
                selected.append(task)
                continue
            if task.cancelled():
                _jobs.pop(key, None)
            elif isinstance(task.result(), Exception) and time.time() - finished < 60:
                continue
        if not await asyncio.to_thread(_needs_text, item):
            continue

        async def run(item=item):
            try:
                return await _extract(item)
            except Exception as exc:
                logger.warning(
                    "Zotero fulltext sync pending for %s: %s",
                    item["item_key"],
                    type(exc).__name__,
                )
                return exc

        task = asyncio.create_task(run())
        _jobs[key] = (task, time.time())

        def done(task, key=key):
            _jobs[key] = (task, time.time())

        task.add_done_callback(done)
        selected.append(task)
    if selected:
        await asyncio.wait(selected, timeout=5)


def status():
    return {
        "running": sum(not task.done() for task, _ in _jobs.values()),
        "completed": sum(
            task.done()
            and not task.cancelled()
            and not isinstance(task.result(), Exception)
            for task, _ in _jobs.values()
        ),
        "pending_retry": sum(
            task.done()
            and not task.cancelled()
            and isinstance(task.result(), Exception)
            for task, _ in _jobs.values()
        ),
    }
