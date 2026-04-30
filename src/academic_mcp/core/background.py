"""Background task management for semantic index sync."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_semantic_sync_task: asyncio.Task | None = None


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
