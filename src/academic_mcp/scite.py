"""Scite public API integration.

No API key required. Uses public endpoints for citation tallies and paper
metadata (including editorial notices such as retractions/corrections).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.scite.ai"
_TIMEOUT = 15.0
_CACHE_TTL_SECONDS = 3600
_BATCH_CONCURRENCY = 5

_tally_cache: dict[str, tuple[float, dict]] = {}


def _normalize_doi(doi: str) -> str:
    return doi.lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def _cache_get(doi: str) -> dict | None:
    key = _normalize_doi(doi)
    hit = _tally_cache.get(key)
    if not hit:
        return None
    expires_at, payload = hit
    if time.time() > expires_at:
        _tally_cache.pop(key, None)
        return None
    return payload


def _cache_set(doi: str, payload: dict) -> None:
    _tally_cache[_normalize_doi(doi)] = (time.time() + _CACHE_TTL_SECONDS, payload)


async def get_scite_tallies(doi: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """Return normalized Scite tally info for one DOI."""
    doi_norm = _normalize_doi(doi)
    if not doi_norm:
        return None

    cached = _cache_get(doi_norm)
    if cached is not None:
        return cached

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=_TIMEOUT)

    try:
        resp = await client.get(
            f"{_BASE}/tallies/{doi_norm}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return None

        data = resp.json() or {}
        supporting = int(data.get("supporting") or 0)
        contrasting = int(data.get("contradicting") or data.get("contrasting") or 0)
        mentioning = int(data.get("mentioning") or 0)
        citing = int(data.get("citingPublications") or 0)
        total = int(data.get("total") or (supporting + contrasting + mentioning))

        payload = {
            "doi": doi_norm,
            "supporting": supporting,
            "contrasting": contrasting,
            "mentioning": mentioning,
            "citing": citing,
            "total": total,
            "retracted": False,
        }
        _cache_set(doi_norm, payload)
        return payload
    except Exception as e:
        logger.debug("Scite tally request failed for %s: %s", doi_norm, e)
        return None
    finally:
        if owns_client:
            await client.aclose()


async def get_scite_tallies_batch(dois: list[str], concurrency: int = _BATCH_CONCURRENCY) -> dict[str, dict]:
    """Fetch tallies concurrently with semaphore throttling and in-memory TTL cache."""
    out: dict[str, dict] = {}
    if not dois:
        return out

    unique = [_normalize_doi(d) for d in dois if d]
    unique = [d for d in dict.fromkeys(unique) if d]
    sem = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async def _one(doi_norm: str) -> None:
            cached = _cache_get(doi_norm)
            if cached is not None:
                out[doi_norm] = cached
                return
            async with sem:
                payload = await get_scite_tallies(doi_norm, client=client)
                if payload:
                    out[doi_norm] = payload

        await asyncio.gather(*[_one(d) for d in unique])

    return out


async def get_scite_papers_batch(dois: list[str]) -> dict[str, dict]:
    """Fetch Scite paper metadata in one batch call."""
    if not dois:
        return {}

    unique = [_normalize_doi(d) for d in dois if d]
    unique = [d for d in dict.fromkeys(unique) if d][:500]
    if not unique:
        return {}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{_BASE}/papers",
                json={"dois": unique},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                return {}
            return (resp.json() or {}).get("papers") or {}
    except Exception as e:
        logger.debug("Scite paper batch request failed: %s", e)
        return {}


def paper_has_retraction_notice(paper: dict[str, Any] | None) -> bool:
    """Return True when Scite paper metadata contains retraction/correction signals."""
    if not paper:
        return False

    notices = paper.get("editorialNotices") or []
    text_parts: list[str] = []
    if isinstance(notices, list):
        for n in notices:
            if isinstance(n, dict):
                text_parts.extend([
                    str(n.get("type") or ""),
                    str(n.get("label") or ""),
                    str(n.get("title") or ""),
                    str(n.get("description") or ""),
                ])
            else:
                text_parts.append(str(n))

    combined = " ".join(text_parts).lower()
    if not combined:
        return False
    return any(token in combined for token in ("retract", "withdraw", "expression of concern", "correction"))
