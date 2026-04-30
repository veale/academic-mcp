"""core.semantic — business logic for semantic Zotero search."""

from __future__ import annotations

import logging

from .types import SemanticHit

logger = logging.getLogger(__name__)


async def semantic_search_zotero(query: str, k: int = 10) -> list[SemanticHit]:
    """Run semantic search against the Zotero index and return top-*k* unique hits.

    Raises:
        SemanticIndexUnavailable: propagated from get_semantic_index() — callers
            should catch this and surface the message.
    Returns:
        list of hit dicts with keys: item_key, title, doi, score, rerank_score,
        chunk_source, char_start, char_end, chunk_idx, chunk_count, snippet.
    """
    from ..semantic_index import SemanticIndexUnavailable, get_semantic_index  # noqa: F401
    from ..cross_reranker import rerank
    from ..config import config as _config
    from .background import _ensure_semantic_background_sync

    _ensure_semantic_background_sync()

    idx = get_semantic_index()
    fetch_n = max(k, _config.cross_reranker_fetch or 50)
    chunks = await idx.search(query, k=fetch_n)

    if not chunks:
        return []

    # Rerank the candidate pool and deduplicate by item_key, keeping top-k.
    reranked = await rerank(query, chunks, top_k=len(chunks))

    seen_keys: set[str] = set()
    unique_hits: list[SemanticHit] = []
    for h in reranked:
        ik = h.get("item_key") or ""
        if ik not in seen_keys:
            seen_keys.add(ik)
            unique_hits.append(SemanticHit(
                item_key=h.get("item_key"),
                title=h.get("title"),
                doi=h.get("doi"),
                score=float(h.get("score") or 0.0),
                rerank_score=h.get("rerank_score"),
                chunk_source=h.get("chunk_source"),
                char_start=h.get("char_start"),
                char_end=h.get("char_end"),
                chunk_idx=h.get("chunk_idx"),
                chunk_count=h.get("chunk_count"),
                snippet=h.get("snippet"),
            ))
        if len(unique_hits) >= k:
            break

    return unique_hits
