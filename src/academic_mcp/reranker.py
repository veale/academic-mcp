"""Re-ranking for cross-source academic search results.

Used by ``search_papers`` (Semantic Scholar / OpenAlex / Zotero / Primo)
to reorder a merged candidate list by relevance to the query. Distinct
from :mod:`cross_reranker`, which reranks chunks for ``semantic_search_zotero``.

Provider chain (shared with cross_reranker via ``RERANKER_PRIMARY`` /
``RERANKER_FALLBACK``):

  * ``openrouter`` — POST /v1/rerank to OpenRouter (cohere/rerank-v3.5).
    Cross-encoder quality, ~150 ms latency, requires OPENROUTER_API_KEY.
  * ``fastembed``  — local ONNX bi-encoder via fastembed (default
    ``sentence-transformers/all-MiniLM-L6-v2``). Tiny footprint, ~50 ms
    on CPU, no network. Bi-encoder quality (rougher than cross-encoder).
  * ``local``      — sentence-transformers SentenceTransformer. Same
    quality as fastembed but pulls in torch (~4 GB). Requires the
    ``local-models`` extra.
  * ``none``       — no semantic rerank; sort by composite score
    (zotero-bonus, OA, citations, recency, breadth).

Zotero items are always promoted to the top tier regardless of provider.

Configuration:
  * ``RERANKER_PRIMARY``           — default ``openrouter``
  * ``RERANKER_FALLBACK``          — default ``none``
  * ``OPENROUTER_API_KEY``         — required for openrouter
  * ``OPENROUTER_RERANK_MODEL``    — default ``cohere/rerank-v3.5``
  * ``FASTEMBED_RERANK_MODEL``     — default ``sentence-transformers/all-MiniLM-L6-v2``
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

from .config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Composite (no-rerank) fallback
# ---------------------------------------------------------------------------

_CURRENT_YEAR = 2026


def _composite_score(r: dict) -> tuple:
    zotero_bonus = 1 if r.get("in_zotero") else 0
    oa_bonus = 1 if r.get("has_oa_pdf") else 0
    cites = math.log1p(r.get("citations") or 0)
    year = int(r.get("year") or 0)
    recency = max(0, 3 - (_CURRENT_YEAR - year)) if year else 0
    breadth = len(r.get("found_in", []))
    return (zotero_bonus, oa_bonus, breadth, cites, recency)


def _result_text(r: dict) -> str:
    text = r.get("abstract") or r.get("title") or ""
    if text.startswith("[Preview from"):
        idx = text.find("]: ")
        if idx > 0:
            text = text[idx + 3:]
    return text


# ---------------------------------------------------------------------------
# Provider: local sentence-transformers (heavy)
# ---------------------------------------------------------------------------

_local_model = None
_local_load_attempted = False


def _load_local_model():
    global _local_model, _local_load_attempted
    if _local_load_attempted:
        return _local_model
    _local_load_attempted = True
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        logger.info(
            "reranker: 'local' provider requested but sentence-transformers "
            "is not installed. Install the optional extra "
            "(`uv sync --extra local-models`) or use 'fastembed' / 'openrouter' / 'none'."
        )
        return None
    try:
        _local_model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("reranker: loaded local SentenceTransformer all-MiniLM-L6-v2")
    except Exception as e:
        logger.warning("reranker: local model load failed: %s", e)
        _local_model = None
    return _local_model


def _score_local(query: str, texts: list[str]) -> list[float] | None:
    model = _load_local_model()
    if model is None:
        return None
    import numpy as np
    embs = model.encode([query] + list(texts), normalize_embeddings=True)
    return np.dot(embs[1:], embs[0]).tolist()


# ---------------------------------------------------------------------------
# Provider: fastembed (lightweight ONNX bi-encoder)
# ---------------------------------------------------------------------------

_fastembed_model = None
_fastembed_load_attempted = False


def _load_fastembed_model():
    global _fastembed_model, _fastembed_load_attempted
    if _fastembed_load_attempted:
        return _fastembed_model
    _fastembed_load_attempted = True
    try:
        from fastembed import TextEmbedding  # type: ignore
    except ImportError:
        logger.info(
            "reranker: 'fastembed' provider requested but fastembed is not "
            "installed. Install with `uv sync --extra fastembed` (or `pip "
            "install fastembed`). Falling through to fallback provider."
        )
        return None
    model_name = os.getenv(
        "FASTEMBED_RERANK_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    try:
        _fastembed_model = TextEmbedding(model_name=model_name)
        logger.info("reranker: loaded fastembed %s", model_name)
    except Exception as e:
        logger.warning("reranker: fastembed load failed: %s", e)
        _fastembed_model = None
    return _fastembed_model


def _score_fastembed(query: str, texts: list[str]) -> list[float] | None:
    model = _load_fastembed_model()
    if model is None:
        return None
    import numpy as np
    embs = list(model.embed([query] + list(texts)))
    arr = np.array(embs)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.clip(norms, 1e-12, None)
    return np.dot(arr[1:], arr[0]).tolist()


# ---------------------------------------------------------------------------
# Provider: OpenRouter rerank API (cross-encoder quality)
# ---------------------------------------------------------------------------

def _score_openrouter(query: str, texts: list[str]) -> list[float] | None:
    api_key = config.openrouter_api_key
    if not api_key:
        return None
    try:
        import httpx
    except ImportError:
        return None
    payload = {
        "model": config.openrouter_api_rerank_model,
        "query": query,
        "documents": texts,
        "top_n": len(texts),
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
    except Exception as e:
        logger.warning("reranker: openrouter call failed: %r", e)
        return None
    results = data.get("results") or []
    scores = [0.0] * len(texts)
    for r in results:
        idx = r.get("index")
        if idx is None or not (0 <= idx < len(texts)):
            continue
        scores[idx] = float(r.get("relevance_score", 0.0))
    return scores


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

def _score(provider: str, query: str, texts: list[str]) -> list[float] | None:
    p = (provider or "").lower()
    if p == "openrouter":
        return _score_openrouter(query, texts)
    if p == "fastembed":
        return _score_fastembed(query, texts)
    if p == "local":
        return _score_local(query, texts)
    if p in ("", "none", "off", "disabled"):
        return None
    logger.warning("reranker: unknown provider %r — skipping", provider)
    return None


def _compute_similarities(query: str, texts: list[str]) -> list[float]:
    """Try primary then fallback. Returns [] if neither produces scores."""
    for provider in (config.reranker_primary, config.reranker_fallback):
        scores = _score(provider, query, texts)
        if scores is not None:
            return scores
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def rerank_results(query: str, results: list[dict]) -> list[dict]:
    """Re-rank search results by relevance to the query.

    Zotero items are always promoted to the top tier. Within each tier,
    results are sorted by the configured rerank provider's score; if all
    providers miss, falls back to composite scoring.
    """
    if not results:
        return results

    texts = [_result_text(r) for r in results]

    try:
        similarities = await asyncio.to_thread(_compute_similarities, query, texts)
    except Exception as e:
        logger.warning("reranker: scoring failed, using composite: %s", e)
        similarities = []

    if similarities and len(similarities) == len(results):
        for r, sim in zip(results, similarities):
            r["_semantic_similarity"] = round(sim, 4)
        results.sort(
            key=lambda r: (
                1 if r.get("in_zotero") else 0,
                r.get("_semantic_similarity", 0.0),
            ),
            reverse=True,
        )
    else:
        results.sort(key=_composite_score, reverse=True)

    return results
