"""Core business logic for Zotero library management."""

from __future__ import annotations

import logging

from .types import ZoteroIndexRefresh

logger = logging.getLogger(__name__)


async def list_libraries():
    """Return all Zotero libraries. Caller must guard for sqlite availability."""
    from .. import zotero_sqlite
    return await zotero_sqlite.list_libraries()


async def refresh_zotero_index() -> ZoteroIndexRefresh:
    """Invalidate + rebuild the Zotero DOI index; probe all backends."""
    from .. import zotero

    zotero.invalidate_doi_index()
    doi_index = await zotero.get_doi_index()
    connections = await zotero.check_connections()

    sqlite_active = bool(
        connections.get("sqlite", {}).get("reachable", False)
    )

    return ZoteroIndexRefresh(
        doi_count=len(doi_index),
        index_path=str(zotero.zot_config.doi_index_path),
        connections=connections,
        local_enabled=zotero.zot_config.local_enabled,
        local_host=zotero.zot_config.local_host,
        local_port=zotero.zot_config.local_port,
        sqlite_active=sqlite_active,
    )
