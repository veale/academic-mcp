"""Cross-encoder reranker for semantic_search_zotero.

Reranks a pool of bi-encoder candidate chunks using a configurable
two-stage provider chain (primary → fallback). Each provider may be:

  * ``"openrouter"`` — POST /v1/rerank to OpenRouter (cohere/rerank-v3.5
    by default). Hosted, GPU-fast, requires ``OPENROUTER_API_KEY``.
  * ``"local"``      — sentence-transformers CrossEncoder loaded in-process
    (``BAAI/bge-reranker-v2-m3`` by default).
  * ``"none"``       — no reranking; bi-encoder order returned unchanged.

Configuration (via :attr:`config`):
  * ``RERANKER_PRIMARY``   — primary provider (default: ``openrouter``)
  * ``RERANKER_FALLBACK``  — used only if primary fails (default: ``none``)
  * ``CROSS_RERANKER_MODEL`` — local model id
  * ``OPENROUTER_RERANK_MODEL`` — hosted model id
  * ``CROSS_RERANKER_FETCH`` — candidate pool size

If the entire chain fails or is disabled, the function returns the input
chunks sorted by their existing bi-encoder ``score``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import config

logger = logging.getLogger(__name__)

# Local CrossEncoder is loaded once per process.
_cross_encoder = None
_local_load_attempted: bool = False


def _get_local_cross_encoder():
    """Return a loaded sentence-transformers CrossEncoder, or None on failure."""
    global _cross_encoder, _local_load_attempted
    if _local_load_attempted:
        return _cross_encoder
    _local_load_attempted = True
    model_name = config.cross_reranker_model or "BAAI/bge-reranker-v2-m3"
    try:
        from sentence_transformers import CrossEncoder  # type: ignore

        logger.info("cross_reranker: loading local CrossEncoder %s", model_name)
        _cross_encoder = CrossEncoder(model_name)
        logger.info("cross_reranker: local CrossEncoder loaded")
    except Exception as exc:
        logger.warning(
            "cross_reranker: could not load local %s (%s)", model_name, exc
        )
        _cross_encoder = None
    return _cross_encoder


def _passage_text(chunk: dict[str, Any]) -> str:
    """Title-prefixed passage text matching what was embedded during sync."""
    title = (chunk.get("title") or "").strip()
    body = (chunk.get("snippet") or chunk.get("text") or "").strip()
    if title and not body.startswith(title):
        return f"{title}\n\n{body}" if body else title
    return body or title


def _bi_encoder_fallback(
    chunks: list[dict[str, Any]], top_k: int
) -> list[dict[str, Any]]:
    """Return top_k chunks sorted by their original bi-encoder score."""
    out = []
    for c in chunks:
        d = dict(c)
        d.setdefault("rerank_score", d.get("score", 0.0))
        out.append(d)
    out.sort(key=lambda x: x["rerank_score"], reverse=True)
    return out[:top_k]


def _rerank_local(
    query: str, chunks: list[dict[str, Any]], top_k: int
) -> list[dict[str, Any]] | None:
    """Score (query, passage) pairs locally. Returns None if unavailable."""
    enc = _get_local_cross_encoder()
    if enc is None:
        return None
    pairs = [(query, _passage_text(c)) for c in chunks]
    scores = enc.predict(pairs)
    ranked: list[dict[str, Any]] = []
    for chunk, score in zip(chunks, scores):
        out = dict(chunk)
        out["rerank_score"] = float(score)
        ranked.append(out)
    ranked.sort(key=lambda x: x["rerank_score"], reverse=True)
    return ranked[:top_k]


def _rerank_openrouter(
    query: str, chunks: list[dict[str, Any]], top_k: int
) -> list[dict[str, Any]] | None:
    """Score via OpenRouter /v1/rerank. Returns None if unavailable."""
    api_key = config.openrouter_api_key
    if not api_key:
        logger.warning("cross_reranker: OPENROUTER_API_KEY not set; skipping openrouter")
        return None

    try:
        import httpx
    except ImportError:
        logger.warning("cross_reranker: httpx not installed; cannot use openrouter")
        return None

    documents = [_passage_text(c) for c in chunks]
    payload = {
        "model": config.openrouter_rerank_model,
        "query": query,
        "documents": documents,
        "top_n": min(top_k, len(documents)),
    }
    url = config.openrouter_base_url.rstrip("/") + "/rerank"

    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("cross_reranker: openrouter call failed: %r", exc)
        return None

    results = data.get("results") or []
    if not results:
        logger.warning("cross_reranker: openrouter returned no results")
        return None

    ranked: list[dict[str, Any]] = []
    for r in results:
        idx = r.get("index")
        if idx is None or not (0 <= idx < len(chunks)):
            continue
        out = dict(chunks[idx])
        out["rerank_score"] = float(r.get("relevance_score", 0.0))
        ranked.append(out)
    if not ranked:
        return None
    return ranked[:top_k]


def _run_provider(
    name: str, query: str, chunks: list[dict[str, Any]], top_k: int
) -> list[dict[str, Any]] | None:
    """Dispatch to a single provider. Returns None on miss/failure."""
    n = (name or "").lower()
    if n == "openrouter":
        return _rerank_openrouter(query, chunks, top_k)
    if n == "local":
        return _rerank_local(query, chunks, top_k)
    if n in ("", "none", "off", "disabled"):
        return None
    logger.warning("cross_reranker: unknown provider %r — skipping", name)
    return None


async def rerank(
    query: str,
    chunks: list[dict[str, Any]],
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Rerank *chunks* against *query*.

    Tries ``reranker_primary``; if it returns None (unavailable, error, or
    "none"), tries ``reranker_fallback``. If both miss, returns the top_k
    by existing bi-encoder score.
    """
    if not chunks:
        return []

    def _blocking() -> list[dict[str, Any]]:
        for provider in (config.reranker_primary, config.reranker_fallback):
            result = _run_provider(provider, query, chunks, top_k)
            if result is not None:
                return result
        return _bi_encoder_fallback(chunks, top_k)

    return await asyncio.to_thread(_blocking)
