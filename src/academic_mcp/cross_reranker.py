"""Cross-encoder reranker for semantic_search_zotero.

Uses sentence-transformers' CrossEncoder to rerank a pool of bi-encoder
candidate chunks. This is a separate module from :mod:`reranker`, which
uses a bi-encoder for the *search_papers* (Semantic Scholar) pipeline and
must not be modified.

Architecture:
  1. The server handler calls :func:`rerank` with a *query* string and a
     list of chunk dicts (as returned by :meth:`SemanticIndex.search`).
  2. Each chunk dict must have at least ``"item_key"`` and ``"snippet"``
     (or ``"text"`` for tests). The chunk's ``title`` is prepended if
     present, matching what was embedded during sync.
  3. The CrossEncoder scores ``(query, passage)`` pairs in one batch call
     and returns a re-ordered list.

Model:
  ``BAAI/bge-reranker-v2-m3`` — good multilingual coverage, ~650 MB,
  runs on MPS on Apple Silicon via sentence-transformers.

Configuration (via :attr:`config`):
  * ``CROSS_RERANKER_MODEL`` — model name/path (default: ``BAAI/bge-reranker-v2-m3``)
  * ``CROSS_RERANKER_FETCH`` — candidate pool size to feed the reranker
    (default: 50). The caller fetches this many from Chroma, the reranker
    returns the top-k.

If the sentence-transformers package is missing or the model cannot be
loaded, :func:`rerank` returns the input list unchanged (graceful
degradation) and logs a warning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import config

logger = logging.getLogger(__name__)

# Sentinel so we only attempt model load once per process.
_cross_encoder = None
_load_attempted: bool = False


def _get_cross_encoder():
    """Return a loaded CrossEncoder, or None on failure."""
    global _cross_encoder, _load_attempted
    if _load_attempted:
        return _cross_encoder
    _load_attempted = True
    model_name = config.cross_reranker_model or "BAAI/bge-reranker-v2-m3"
    try:
        from sentence_transformers import CrossEncoder  # type: ignore

        logger.info("semantic_index: loading cross-encoder %s", model_name)
        _cross_encoder = CrossEncoder(model_name)
        logger.info("semantic_index: cross-encoder loaded")
    except Exception as exc:
        logger.warning(
            "cross_reranker: could not load %s (%s). "
            "Semantic search will return bi-encoder ranking.",
            model_name,
            exc,
        )
        _cross_encoder = None
    return _cross_encoder


def _passage_text(chunk: dict[str, Any]) -> str:
    """Extract the passage text to score against the query.

    Prefer the stored ``snippet``; fall back to ``text``.  Prepend the
    title (if present) so the reranker sees the same title-prefixed text
    that was embedded during sync.
    """
    title = (chunk.get("title") or "").strip()
    body = (chunk.get("snippet") or chunk.get("text") or "").strip()
    if title and not body.startswith(title):
        return f"{title}\n\n{body}" if body else title
    return body or title


async def rerank(
    query: str,
    chunks: list[dict[str, Any]],
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Rerank *chunks* against *query* using a cross-encoder.

    Returns up to *top_k* chunks, each with an added ``rerank_score``
    field. The list is sorted by descending ``rerank_score``.

    If the cross-encoder is unavailable, returns the first *top_k* chunks
    with ``rerank_score`` set to their original bi-encoder ``score``.
    """
    if not chunks:
        return []

    def _blocking() -> list[dict[str, Any]]:
        enc = _get_cross_encoder()
        if enc is None:
            # Graceful degradation: return top_k by bi-encoder score.
            for c in chunks:
                c.setdefault("rerank_score", c.get("score", 0.0))
            return sorted(chunks, key=lambda x: x["rerank_score"], reverse=True)[:top_k]

        pairs = [(query, _passage_text(c)) for c in chunks]
        scores = enc.predict(pairs)

        ranked: list[dict[str, Any]] = []
        for chunk, score in zip(chunks, scores):
            out = dict(chunk)
            out["rerank_score"] = float(score)
            ranked.append(out)

        ranked.sort(key=lambda x: x["rerank_score"], reverse=True)
        return ranked[:top_k]

    return await asyncio.to_thread(_blocking)
