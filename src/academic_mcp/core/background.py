"""Background task management: semantic sync, lexical sync, article prewarm."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_semantic_sync_task: asyncio.Task | None = None
_lexical_sync_task: asyncio.Task | None = None
_prewarm_tasks: set[asyncio.Task] = set()


def _ensure_semantic_background_sync(max_age_hours: int = 24) -> None:
    """Kick off a background semantic sync when stale; never blocks request paths."""
    global _semantic_sync_task

    if _semantic_sync_task and not _semantic_sync_task.done():
        return

    async def _runner() -> None:
        from ..semantic_index import SemanticIndexUnavailable, get_semantic_index

        _MAX_ATTEMPTS = 5
        _RETRY_DELAYS = [30, 60, 120, 300]

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                idx = get_semantic_index()
                status = await idx.status()
                last_sync = status.get("last_sync")
                stale = True
                if isinstance(last_sync, str) and last_sync:
                    try:
                        from datetime import datetime, timezone

                        ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                        age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
                        stale = age_hours > max_age_hours
                    except Exception:
                        stale = True
                interrupted = bool(status.get("in_progress"))
                if not stale and not interrupted:
                    return
                if interrupted and not stale:
                    logger.info(
                        "Background semantic sync: resuming interrupted sync "
                        "(in_progress=True in status)."
                    )
                await idx.sync(force_rebuild=False, include_fulltext=False)
                return
            except SemanticIndexUnavailable:
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt < _MAX_ATTEMPTS:
                    delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
                    logger.warning(
                        "Background semantic sync attempt %d/%d failed: %s — "
                        "retrying in %ds",
                        attempt, _MAX_ATTEMPTS, e, delay,
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Background semantic sync failed after %d attempts: %s",
                        _MAX_ATTEMPTS, e,
                        exc_info=True,
                    )

    try:
        _semantic_sync_task = asyncio.create_task(_runner())
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Lexical (FTS5) index
# ---------------------------------------------------------------------------

def ensure_lexical_background_sync(max_age_hours: int = 6) -> None:
    """Refresh the FTS5 mirror in the background when it's stale or missing.

    Cheap compared to the semantic sync — no embeddings — so it runs on a
    tighter staleness budget.
    """
    global _lexical_sync_task

    if _lexical_sync_task and not _lexical_sync_task.done():
        return

    async def _runner() -> None:
        from datetime import datetime, timezone
        from .. import lexical_index

        try:
            st = await lexical_index.status()
            if not st.get("enabled") or not st.get("fts5"):
                return
            last_sync = st.get("last_sync") or ""
            if st.get("count") and last_sync:
                try:
                    ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
                    if age_h <= max_age_hours:
                        return
                except ValueError:
                    pass
            await lexical_index.sync(force_rebuild=False)
        except lexical_index.LexicalIndexUnavailable as e:
            logger.debug("Lexical index unavailable: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Background lexical sync failed: %s", e, exc_info=True)

    try:
        _lexical_sync_task = asyncio.create_task(_runner())
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Article prewarm
# ---------------------------------------------------------------------------

# Prewarm is a bet on what the user asks for next. Don't pay for it twice in
# the same session for the same paper, whether or not the bet paid off.
_prewarmed: set[str] = set()
_PREWARM_TIMEOUT_SEC = 45.0


def prewarm_articles(hits: list) -> None:
    """Pull full text for the top local hits into the article cache.

    Fire-and-forget: the user's search has already returned by the time this
    runs, and a failure here costs nothing — the next ``fetch_fulltext`` simply
    takes the slow path it would have taken anyway.

    Only Zotero-held papers are prewarmed. Fetching external papers in the
    background would mean hitting publishers (and possibly the stealth browser)
    for material the user never asked for.
    """
    from ..config import config

    if not config.prewarm_enabled:
        return

    targets: list[dict] = []
    for hit in hits:
        if len(targets) >= config.prewarm_max_articles:
            break
        if not getattr(hit, "in_zotero", False):
            continue
        doi = getattr(hit, "doi", None)
        zotero_key = getattr(hit, "zotero_key", None)
        marker = doi or zotero_key
        if not marker or marker in _prewarmed:
            continue
        _prewarmed.add(marker)
        targets.append({"doi": doi, "zotero_key": zotero_key})

    if not targets:
        return

    async def _runner() -> None:
        from .. import text_cache
        from .fetch import fetch_article
        from .types import ArticleId

        for target in targets:
            doi = target["doi"]
            label = doi or target["zotero_key"]
            try:
                if doi and text_cache.get_cached(doi):
                    continue  # already warm
                async with asyncio.timeout(_PREWARM_TIMEOUT_SEC):
                    await fetch_article(
                        ArticleId(doi=doi, zotero_key=target["zotero_key"]),
                        mode="sections",
                    )
                logger.debug("Prewarmed article %s", label)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Prewarm failed for %s: %s", label, e)

    try:
        task = asyncio.create_task(_runner())
    except RuntimeError:
        return
    _prewarm_tasks.add(task)
    task.add_done_callback(_prewarm_tasks.discard)
