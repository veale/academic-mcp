"""CORE.ac.uk API client — 300M+ paper aggregator with 40M+ full-text PDFs.

CORE is an OA aggregator that indexes papers from institutional repositories
worldwide. Its /outputs/{id}/download endpoint returns PDF bytes directly,
bypassing the repository landing page.

API docs: https://api.core.ac.uk/docs/v3
Rate limit (free tier): 5 single requests OR 1 batch request per 10 seconds.
Auth: Authorization: Bearer {CORE_API_KEY}
"""

import logging
from pathlib import Path

import httpx

from .config import config
from .pdf_fetcher import _save_bytes_to_cache, _is_pdf_header

logger = logging.getLogger(__name__)

CORE_BASE = "https://api.core.ac.uk/v3"


def _core_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.core_api_key}"}


async def search_core(
    doi: str | None = None,
    title: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Search CORE for a paper by DOI or title.

    Returns list of dicts:
        core_id: int — CORE internal ID
        doi: str | None
        title: str | None
        download_url: str | None — direct PDF link (may need auth or redirect)
        source_fulltext_urls: list[str] — repository URLs for full text
        has_fulltext: bool — whether CORE has indexed the full text
    """
    if not config.core_api_key:
        return []

    if doi:
        q = f"doi:{doi}"
    elif title:
        q = f'title:"{title}" AND _exists_:fullText'
    else:
        return []

    _owned = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        _owned = True

    try:
        resp = await client.get(
            f"{CORE_BASE}/search/works",
            headers=_core_headers(),
            params={"q": q, "limit": 3},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.debug("CORE search returned %s for q=%r", resp.status_code, q)
            return []

        results = []
        for item in resp.json().get("results") or []:
            results.append({
                "core_id": item.get("id"),
                "doi": item.get("doi"),
                "title": item.get("title"),
                "download_url": item.get("downloadUrl"),
                "source_fulltext_urls": item.get("sourceFulltextUrls") or [],
                "has_fulltext": bool(item.get("fullText")),
            })
        return results
    except Exception as e:
        logger.debug("CORE search failed: %s", e)
        return []
    finally:
        if _owned:
            await client.aclose()


async def download_from_core(
    core_id: int,
    download_url: str | None,
    client: httpx.AsyncClient,
) -> Path | None:
    """Download a PDF from CORE via their /outputs/{id}/download endpoint.

    Tries the CORE API download endpoint first (authenticated, direct bytes).
    Falls back to the item's downloadUrl if the API endpoint fails.
    """
    if not config.core_api_key:
        return None

    # Try the authenticated CORE API download endpoint
    api_url = f"{CORE_BASE}/outputs/{core_id}/download"
    try:
        resp = await client.get(
            api_url,
            headers=_core_headers(),
            timeout=30,
            follow_redirects=True,
        )
        if resp.status_code == 200 and _is_pdf_header(resp.content[:5] if resp.content else b""):
            path = _save_bytes_to_cache(api_url, resp.content)
            if path:
                logger.info("Downloaded PDF from CORE: %s", api_url)
                return path
    except Exception as e:
        logger.debug("CORE API download failed for id=%s: %s", core_id, e)

    # Fall back to the item's own downloadUrl (may point to a repository page)
    if download_url and download_url != api_url:
        try:
            resp = await client.get(download_url, timeout=30, follow_redirects=True)
            if resp.status_code == 200 and _is_pdf_header(resp.content[:5] if resp.content else b""):
                path = _save_bytes_to_cache(download_url, resp.content)
                if path:
                    logger.info("Downloaded PDF from CORE downloadUrl: %s", download_url)
                    return path
        except Exception as e:
            logger.debug("CORE downloadUrl fetch failed for %s: %s", download_url, e)

    return None
