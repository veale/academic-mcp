"""API clients for academic data sources.

Includes exponential backoff for rate-limited APIs (HTTP 429, 5xx).
"""

import asyncio
import httpx
import logging
from typing import Any

from .config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with exponential backoff on 429 / 5xx errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else _BACKOFF_BASE * (2 ** attempt)
                wait = min(wait, 30.0)
                if attempt < max_retries:
                    logger.debug(
                        "Got %s from %s, retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, url, wait, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(wait)
                    continue
            return resp
        except httpx.RequestError as e:
            last_exc = e
            if attempt < max_retries:
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.debug(
                    "Request to %s failed: %s, retrying in %.1fs", url, e, wait,
                )
                await asyncio.sleep(wait)
            else:
                raise
    # Should not reach here, but satisfy type checker
    if last_exc:
        raise last_exc
    raise RuntimeError("Retry loop exhausted without response")

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------

def _client(**kwargs) -> httpx.AsyncClient:
    """Create an httpx client, optionally with proxy."""
    return httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        **kwargs,
    )


def _proxied_client(**kwargs) -> httpx.AsyncClient:
    """Create an httpx client routed through GOST proxy if configured."""
    if config.proxy_dict:
        kwargs["proxy"] = config.gost_proxy_url
    return _client(**kwargs)


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_PAPER_FIELDS = (
    "paperId,externalIds,title,abstract,year,authors,venue,"
    "referenceCount,citationCount,openAccessPdf,tldr,url"
)
S2_SEARCH_FIELDS = (
    "paperId,externalIds,title,abstract,year,authors,venue,"
    "citationCount,openAccessPdf"
)


async def s2_search(
    query: str, limit: int = 10, offset: int = 0,
    start_year: int | None = None, end_year: int | None = None,
) -> dict:
    """Search Semantic Scholar for papers."""
    headers = {}
    if config.semantic_scholar_api_key:
        headers["x-api-key"] = config.semantic_scholar_api_key

    params: dict[str, Any] = {
        "query": query, "limit": limit,
        "offset": offset, "fields": S2_SEARCH_FIELDS,
    }

    # S2 supports &year=start-end (or &year=start- or &year=-end)
    if start_year or end_year:
        year_str = f"{start_year or ''}-{end_year or ''}"
        params["year"] = year_str

    async with _client(headers=headers) as client:
        resp = await _request_with_retry(
            client, "GET", f"{S2_BASE}/paper/search",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


async def s2_paper(paper_id: str) -> dict:
    """Get a single paper by Semantic Scholar ID, DOI, ArXiv ID, etc.

    paper_id can be:
      - S2 paper ID
      - DOI:10.xxxx/yyyy
      - ARXIV:2301.xxxxx
      - CorpusId:xxxxxxx
    """
    headers = {}
    if config.semantic_scholar_api_key:
        headers["x-api-key"] = config.semantic_scholar_api_key

    async with _client(headers=headers) as client:
        resp = await _request_with_retry(
            client, "GET", f"{S2_BASE}/paper/{paper_id}",
            params={"fields": S2_PAPER_FIELDS},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

OA_BASE = "https://api.openalex.org"


def _openalex_headers() -> dict[str, str]:
    """Return auth headers for OpenAlex. Key takes priority over mailto."""
    if config.openalex_api_key:
        return {"Authorization": f"Bearer {config.openalex_api_key}"}
    return {}


def _openalex_mailto_param() -> dict[str, str]:
    """Return mailto param when no API key is configured (polite pool)."""
    if config.openalex_api_key:
        return {}
    return {"mailto": config.unpaywall_email or "academic-mcp@example.com"}


async def openalex_search(
    query: str, limit: int = 10, page: int = 1,
    start_year: int | None = None, end_year: int | None = None,
    venue: str | None = None,
) -> dict:
    """Search OpenAlex works."""
    params: dict[str, Any] = {
        "search": query, "per_page": limit, "page": page,
        **_openalex_mailto_param(),
    }

    # Build filter string for year range and/or venue
    filters: list[str] = []
    if start_year and end_year:
        filters.append(f"publication_year:{start_year}-{end_year}")
    elif start_year:
        filters.append(f"publication_year:{start_year}-")
    elif end_year:
        filters.append(f"publication_year:-{end_year}")
    if venue:
        filters.append(f"host_venue.display_name:{venue}")
    if filters:
        params["filter"] = ",".join(filters)

    async with _client() as client:
        resp = await _request_with_retry(
            client, "GET", f"{OA_BASE}/works",
            params=params, headers=_openalex_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def openalex_work(doi: str) -> dict | None:
    """Look up a single work in OpenAlex by DOI."""
    doi_url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
    async with _client() as client:
        resp = await _request_with_retry(
            client, "GET", f"{OA_BASE}/works/{doi_url}",
            params=_openalex_mailto_param(), headers=_openalex_headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Unpaywall
# ---------------------------------------------------------------------------


async def unpaywall_lookup(doi: str) -> dict | None:
    """Look up open access PDF via Unpaywall."""
    if not config.unpaywall_email:
        logger.warning("UNPAYWALL_EMAIL not set — skipping Unpaywall lookup")
        return None

    clean_doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    async with _client() as client:
        resp = await _request_with_retry(
            client, "GET", f"https://api.unpaywall.org/v2/{clean_doi}",
            params={"email": config.unpaywall_email},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Crossref (for metadata fallback)
# ---------------------------------------------------------------------------


async def crossref_work(doi: str) -> dict | None:
    """Get metadata from Crossref."""
    clean_doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    async with _client() as client:
        resp = await _request_with_retry(
            client, "GET", f"https://api.crossref.org/works/{clean_doi}",
            headers={
                "User-Agent": f"AcademicMCP/0.1 (mailto:{config.unpaywall_email or 'user@example.com'})",
            },
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("message")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_doi(paper: dict) -> str | None:
    """Try to extract a DOI from various API response formats."""
    # Semantic Scholar format
    ext_ids = paper.get("externalIds") or {}
    if ext_ids.get("DOI"):
        return ext_ids["DOI"]

    # OpenAlex format
    doi = paper.get("doi") or paper.get("ids", {}).get("doi")
    if doi:
        return doi.replace("https://doi.org/", "")

    return None


def collect_pdf_urls(paper_s2: dict | None, paper_oa: dict | None,
                     unpaywall: dict | None) -> list[dict[str, str]]:
    """Gather all candidate PDF URLs from the various API responses.

    Returns a list of {url, source} dicts, ordered by preference.
    """
    candidates = []

    # Unpaywall — usually the best source
    if unpaywall:
        best = unpaywall.get("best_oa_location") or {}
        if best.get("url_for_pdf"):
            candidates.append({"url": best["url_for_pdf"], "source": "unpaywall"})
        # Also check all OA locations
        for loc in unpaywall.get("oa_locations") or []:
            url = loc.get("url_for_pdf")
            if url and url not in [c["url"] for c in candidates]:
                candidates.append({"url": url, "source": "unpaywall"})

    # Semantic Scholar
    if paper_s2:
        oa_pdf = paper_s2.get("openAccessPdf") or {}
        if oa_pdf.get("url"):
            url = oa_pdf["url"]
            if url not in [c["url"] for c in candidates]:
                candidates.append({"url": url, "source": "semantic_scholar"})

    # OpenAlex
    if paper_oa:
        oa = paper_oa.get("open_access") or {}
        if oa.get("oa_url"):
            url = oa["oa_url"]
            if url not in [c["url"] for c in candidates]:
                candidates.append({"url": url, "source": "openalex"})

        # Check locations for pdf_url
        for loc in paper_oa.get("locations") or []:
            pdf_url = loc.get("pdf_url")
            if pdf_url and pdf_url not in [c["url"] for c in candidates]:
                candidates.append({"url": pdf_url, "source": "openalex"})

    return candidates
