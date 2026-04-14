"""Semantic re-ranking using sentence-transformers.

Computes cosine similarity between the user query and each result's
abstract (or title as fallback) using the lightweight all-MiniLM-L6-v2
model. Designed for the M4 Mac's CPU/Neural Engine — runs in milliseconds
on batches of 20-40 items.

Safety constraints:
  - All model inference runs via asyncio.to_thread to avoid blocking the
    event loop.
  - Embeddings are computed only for the current result batch (never
    cached across requests) to keep RAM usage minimal.
  - If the model fails to load (missing dependency, OOM, etc.), the
    module silently falls back to the original composite scoring.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy model loading with graceful fallback
# ---------------------------------------------------------------------------

_model = None
_model_load_failed = False


def _load_model():
    """Load the sentence-transformers model (once). Returns None on failure."""
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Loaded sentence-transformers model: all-MiniLM-L6-v2")
        return _model
    except Exception as e:
        _model_load_failed = True
        logger.warning(
            "sentence-transformers unavailable, falling back to composite scoring: %s", e
        )
        return None


# ---------------------------------------------------------------------------
# Cosine similarity computation (runs in thread via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _compute_similarities(query: str, texts: list[str]) -> list[float]:
    """Compute cosine similarities between query and each text.

    Returns a list of floats in [0, 1] (cosine similarity).
    Runs synchronously — caller wraps in asyncio.to_thread.
    """
    model = _load_model()
    if model is None:
        return []

    import numpy as np

    # Encode query + all texts in a single batch for efficiency
    all_inputs = [query] + texts
    embeddings = model.encode(all_inputs, normalize_embeddings=True)

    query_emb = embeddings[0]
    text_embs = embeddings[1:]

    # Cosine similarity with normalized vectors = dot product
    similarities = np.dot(text_embs, query_emb).tolist()
    return similarities


# ---------------------------------------------------------------------------
# Fallback composite scoring (original logic)
# ---------------------------------------------------------------------------

_CURRENT_YEAR = 2026


def _composite_score(r: dict) -> tuple:
    """Original composite scoring — used when semantic model is unavailable."""
    zotero_bonus = 1 if r.get("in_zotero") else 0
    oa_bonus = 1 if r.get("has_oa_pdf") else 0
    cites = math.log1p(r.get("citations") or 0)
    year = int(r.get("year") or 0)
    recency = max(0, 3 - (_CURRENT_YEAR - year)) if year else 0
    breadth = len(r.get("found_in", []))
    return (zotero_bonus, oa_bonus, breadth, cites, recency)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def rerank_results(query: str, results: list[dict]) -> list[dict]:
    """Re-rank search results by semantic similarity to the query.

    - Zotero items are always boosted to the top tier.
    - Within each tier (zotero / non-zotero), results are sorted by
      cosine similarity to the query embedding.
    - If the model is unavailable, falls back to composite scoring.

    This function is safe to call from the async event loop — all model
    inference is offloaded to a thread.
    """
    if not results:
        return results

    # Build text representations for each result (abstract preferred, title fallback)
    texts = []
    for r in results:
        text = r.get("abstract") or r.get("title") or ""
        # Strip preview prefixes from Zotero fulltext previews
        if text.startswith("[Preview from"):
            idx = text.find("]: ")
            if idx > 0:
                text = text[idx + 3:]
        texts.append(text)

    # Compute similarities in a thread (non-blocking)
    try:
        similarities = await asyncio.to_thread(_compute_similarities, query, texts)
    except Exception as e:
        logger.warning("Semantic re-ranking failed, using composite scoring: %s", e)
        similarities = []

    if similarities and len(similarities) == len(results):
        # Attach similarity scores to results
        for r, sim in zip(results, similarities):
            r["_semantic_similarity"] = round(sim, 4)

        # Sort: Zotero tier first, then by semantic similarity descending
        results.sort(
            key=lambda r: (
                1 if r.get("in_zotero") else 0,
                r.get("_semantic_similarity", 0.0),
            ),
            reverse=True,
        )
    else:
        # Fallback to composite scoring
        results.sort(key=_composite_score, reverse=True)

    return results
