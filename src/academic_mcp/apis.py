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


_OA_CITATION_SELECT = (
    "id,doi,title,publication_year,authorships,cited_by_count,"
    "primary_location,abstract_inverted_index"
)


async def _resolve_openalex_filter_id(
    doi: str,
    openalex_id: str | None = None,
) -> str:
    """Resolve a DOI to an OpenAlex identifier suitable for filter values.

    If *openalex_id* is already provided (e.g. 'W2741809807' or the full URL
    'https://openalex.org/W2741809807'), use it directly.  Otherwise, look up
    the DOI via ``openalex_work()`` and extract the Work ID from the response.

    Falls back to the DOI URL if resolution fails — the ``cites`` / ``cited_by``
    filters accept DOI URLs too, but Work IDs are more reliable.
    """
    if openalex_id:
        if openalex_id.startswith("https://openalex.org/"):
            return openalex_id.split("/")[-1]
        return openalex_id

    # Normalise DOI prefix variants before resolving
    clean_doi = doi.replace("DOI:", "").replace("doi:", "").strip()

    try:
        work = await openalex_work(clean_doi)
        if work and work.get("id"):
            # "https://openalex.org/W2741809807" → "W2741809807"
            return work["id"].split("/")[-1]
    except Exception:
        pass

    # Fallback: use DOI URL directly (works in most cases)
    return clean_doi if clean_doi.startswith("http") else f"https://doi.org/{clean_doi}"


def _openalex_year_filters(
    start_year: int | None, end_year: int | None
) -> list[str]:
    """Build publication_year filter fragments."""
    if start_year and end_year:
        return [f"publication_year:{start_year}-{end_year}"]
    if start_year:
        return [f"publication_year:{start_year}-"]
    if end_year:
        return [f"publication_year:-{end_year}"]
    return []


async def openalex_citations(
    doi: str,
    search: str | None = None,
    limit: int = 25,
    start_year: int | None = None,
    end_year: int | None = None,
    openalex_id: str | None = None,
) -> dict:
    """Find works that cite the given DOI (forward citations / children)."""
    resolved = await _resolve_openalex_filter_id(doi, openalex_id)
    filters = [f"cites:{resolved}"] + _openalex_year_filters(start_year, end_year)

    params: dict[str, Any] = {
        "filter": ",".join(filters),
        "select": _OA_CITATION_SELECT,
        "per_page": min(limit, 100),
        "sort": "cited_by_count:desc",
        **_openalex_mailto_param(),
    }
    if search:
        params["search"] = search

    async with _client() as client:
        resp = await _request_with_retry(
            client, "GET", f"{OA_BASE}/works",
            params=params, headers=_openalex_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def openalex_references(
    doi: str,
    search: str | None = None,
    limit: int = 25,
    start_year: int | None = None,
    end_year: int | None = None,
    openalex_id: str | None = None,
) -> dict:
    """Find works cited by the given DOI (backward references / parents)."""
    resolved = await _resolve_openalex_filter_id(doi, openalex_id)
    filters = [f"cited_by:{resolved}"] + _openalex_year_filters(start_year, end_year)

    params: dict[str, Any] = {
        "filter": ",".join(filters),
        "select": _OA_CITATION_SELECT,
        "per_page": min(limit, 100),
        "sort": "cited_by_count:desc",
        **_openalex_mailto_param(),
    }
    if search:
        params["search"] = search

    async with _client() as client:
        resp = await _request_with_retry(
            client, "GET", f"{OA_BASE}/works",
            params=params, headers=_openalex_headers(),
        )
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


def _normalize_isbn(isbn: str) -> str:
    """Strip dashes/spaces/URL prefixes so ISBN queries match Crossref's canonical form."""
    return "".join(c for c in (isbn or "") if c.isalnum())


async def crossref_book_chapters(
    isbn: str | None = None,
    container_title: str | None = None,
    keywords: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List book-chapter works that share an ISBN or container-title.

    Preferred keyed on ISBN (reliable); falls back to exact container-title
    search when only the book title is known.
    """
    if not isbn and not container_title:
        return []

    filters = ["type:book-chapter"]
    if isbn:
        filters.append(f"isbn:{_normalize_isbn(isbn)}")

    params: dict[str, Any] = {
        "filter": ",".join(filters),
        "rows": min(max(limit, 1), 100),
        "select": (
            "DOI,title,author,page,container-title,ISBN,published-print,"
            "published-online,type,publisher,editor,volume"
        ),
    }
    if container_title and not isbn:
        # container-title.search isn't a supported filter; use query.container-title
        params["query.container-title"] = container_title
    if keywords:
        params["query.bibliographic"] = keywords

    async with _client() as client:
        resp = await _request_with_retry(
            client, "GET", "https://api.crossref.org/works",
            params=params,
            headers={
                "User-Agent": f"AcademicMCP/0.1 (mailto:{config.unpaywall_email or 'user@example.com'})",
            },
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", []) or []

    # When only container-title was used (no ISBN), Crossref returns fuzzy
    # matches — keep only items whose container-title actually matches.
    if container_title and not isbn:
        want = container_title.strip().lower()
        items = [
            it for it in items
            if any(want == (ct or "").strip().lower()
                   for ct in (it.get("container-title") or []))
        ]
    return items


# ---------------------------------------------------------------------------
# Ex Libris Primo
# ---------------------------------------------------------------------------

import re as _re

_PRIMO_SUBFIELD_URL_RE = _re.compile(r'\$\$U(.*?)(?=\$\$|$)')


def _primo_extract_url(subfield_str: str) -> str | None:
    """Extract the URL from an Ex Libris $$U...$$X subfield string."""
    m = _PRIMO_SUBFIELD_URL_RE.search(subfield_str or "")
    return m.group(1).strip() if m else None


def _primo_build_q(query: str) -> str:
    """Translate a search query to Primo's field,precision,terms format.

    Supported prefixes: author:/creator:  → creator,contains
                        title:            → title,contains
                        subject:          → sub,contains
    Everything else    → any,contains
    """
    author_match = _re.match(r'^(?:author|creator):(.+)$', query.strip(), _re.I)
    title_match  = _re.match(r'^title:(.+)$',           query.strip(), _re.I)
    subject_match = _re.match(r'^subject:(.+)$',        query.strip(), _re.I)

    if author_match:
        return f"creator,contains,{author_match.group(1).strip()}"
    if title_match:
        return f"title,contains,{title_match.group(1).strip()}"
    if subject_match:
        return f"sub,contains,{subject_match.group(1).strip()}"
    # Strip any unknown field prefixes so Primo doesn't choke
    clean = _re.sub(r'\b\w+:', '', query).strip()
    return f"any,contains,{clean or query}"


async def primo_search(
    query: str, limit: int = 10, offset: int = 0,
    start_year: int | None = None, end_year: int | None = None,
) -> list[dict]:
    """Search an Ex Libris Primo instance.

    Returns a list of normalised result dicts (same schema as _handle_search).
    Returns [] if PRIMO_DOMAIN or PRIMO_VID are not configured.
    """
    if not config.primo_domain or not config.primo_vid:
        return []

    # Only the six parameters documented in the Primo REST guide.
    # Extra undocumented parameters (skipDelivery, conVoc, qExclude, etc.)
    # cause 400 responses on some Primo instances.
    params: dict[str, Any] = {
        "q": _primo_build_q(query),
        "vid": config.primo_vid,
        "tab": config.primo_tab,
        "search_scope": config.primo_search_scope,
        "limit": min(limit, 50),
        "offset": offset,
    }
    # Year filter — only add when actually filtering
    if start_year or end_year:
        lo = start_year or 1000
        hi = end_year or 9999
        params["qInclude"] = f"facet_searchcreationdate,exact,[{lo} TO {hi}]"

    url = f"https://{config.primo_domain}/primaws/rest/pub/pnxs"

    # Try institutional proxy first (gives access to full catalogue metadata);
    # fall back to a direct connection if the proxy is unavailable or fails.
    data = None
    for use_proxy in (True, False):
        try:
            client_factory = _proxied_client if use_proxy else _client
            async with client_factory() as client:
                resp = await _request_with_retry(client, "GET", url, params=params)
            if resp.status_code == 400:
                logger.warning(
                    "Primo returned 400 for query %r — params: %s", query, params
                )
                return []
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as exc:
            if use_proxy and config.gost_proxy_url:
                logger.debug("Primo via proxy failed (%s), retrying direct", exc)
                continue
            raise
    if data is None:
        return []

    results = []
    for doc in (data.get("docs") or []):
        addata  = (doc.get("pnx") or {}).get("addata")  or {}
        display = (doc.get("pnx") or {}).get("display") or {}
        links   = (doc.get("pnx") or {}).get("links")   or {}
        delivery = doc.get("delivery") or {}

        # ── Title ──────────────────────────────────────────────────────
        title = (
            (addata.get("atitle") or addata.get("btitle") or
             display.get("title") or [""])[0]
        )

        # ── Authors ────────────────────────────────────────────────────
        raw_authors = addata.get("au") or display.get("creator") or []
        # Names arrive as "Last, First" — keep as-is, they're already readable
        authors = [a for a in raw_authors if a][:10]

        # ── DOI ────────────────────────────────────────────────────────
        doi_raw = (addata.get("doi") or [""])[0]
        doi = doi_raw.strip() or None

        # ── Year ───────────────────────────────────────────────────────
        date_raw = (addata.get("date") or display.get("creationdate") or [""])[0]
        year = date_raw[:4] if date_raw else None

        # ── Venue ──────────────────────────────────────────────────────
        venue = (
            addata.get("jtitle") or addata.get("btitle") or
            display.get("ispartof") or [None]
        )[0]

        # ── Abstract ───────────────────────────────────────────────────
        abstract_parts = display.get("description") or addata.get("abstract") or []
        abstract = abstract_parts[0] if abstract_parts else None

        # ── Full-text URLs ──────────────────────────────────────────────
        oa_url = None
        for link_key in ("linkunpaywall", "linktopdf", "linktohtml"):
            raw = (links.get(link_key) or [""])[0]
            u = _primo_extract_url(raw)
            if u:
                oa_url = u
                break

        # Institutional proxy URL (always present when Primo is configured)
        proxy_url = delivery.get("almaOpenurl") or None

        results.append({
            "title": title or "Untitled",
            "authors": authors,
            "year": year,
            "doi": doi,
            "abstract": abstract,
            "citations": None,
            "venue": venue,
            "found_in": ["primo"],
            "in_zotero": False,       # enriched later in _handle_search
            "has_oa_pdf": bool(oa_url),
            "s2_id": None,
            "_primo_oa_url": oa_url,
            "_primo_proxy_url": proxy_url,
        })

    return results


async def primo_search_law_reviews(
    query: str,
    limit: int = 10,
    start_year: int | None = None,
    end_year: int | None = None,
) -> list[dict]:
    """Search Primo specifically for law review and legal journal articles.

    Runs TWO Primo queries to maximise coverage:
    1. jtitle contains "law" — catches "law review", "law journal",
       "criminal law", "international law", etc. (~90% of law reviews)
    2. jtitle contains "legal" — catches "legal studies", "legal theory",
       "Journal of Legal Analysis", etc.

    Results are deduplicated by DOI and merged.

    Returns the same normalised result dict schema as primo_search().
    """
    if not config.primo_domain or not config.primo_vid:
        return []

    all_results = []
    seen_dois: set[str] = set()

    # The two journal-title constraints that together cover the vast
    # majority of law reviews and legal journals.
    # "law" catches: Harvard Law Review, Yale L.J., Criminal Law Forum,
    #   International Law Quarterly, Law & Society Review, etc.
    # "legal" catches: Journal of Legal Studies, Legal Theory,
    #   Journal of Legal Analysis, Legal Affairs, etc.
    jtitle_constraints = ["law", "legal"]

    for jtitle_term in jtitle_constraints:
        # Build the multi-field query:
        #   any,contains,{user_query},AND;jtitle,contains,{law|legal}
        # This means: user's keywords in any field AND journal title
        # contains "law" (or "legal").
        clean_query = _re.sub(r'\b\w+:', '', query).strip() or query
        q_value = f"any,contains,{clean_query},AND;jtitle,contains,{jtitle_term}"

        params: dict[str, Any] = {
            "q": q_value,
            "vid": config.primo_vid,
            "tab": config.primo_tab,
            "search_scope": config.primo_search_scope,
            "limit": min(limit, 50),
            "offset": 0,
        }

        # Combine facets: restrict to articles + optional date range
        qInclude_parts = ["facet_rtype,exact,articles"]
        if start_year or end_year:
            lo = start_year or 1000
            hi = end_year or 9999
            qInclude_parts.append(
                f"facet_searchcreationdate,exact,[{lo} TO {hi}]"
            )
        params["qInclude"] = "|,|".join(qInclude_parts)

        url = f"https://{config.primo_domain}/primaws/rest/pub/pnxs"

        data = None
        for use_proxy in (True, False):
            try:
                client_factory = _proxied_client if use_proxy else _client
                async with client_factory() as client:
                    resp = await _request_with_retry(client, "GET", url, params=params)
                if resp.status_code == 400:
                    logger.warning(
                        "Primo law review search returned 400 for jtitle=%s", jtitle_term
                    )
                    break  # Don't retry direct on a 400
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                if use_proxy and config.gost_proxy_url:
                    logger.debug("Primo law search via proxy failed (%s), retrying direct", exc)
                    continue
                logger.debug("Primo law review search failed: %s", exc)
                break

        if not data:
            continue

        # Parse results using the same logic as primo_search()
        for doc in (data.get("docs") or []):
            addata  = (doc.get("pnx") or {}).get("addata")  or {}
            display = (doc.get("pnx") or {}).get("display") or {}
            links   = (doc.get("pnx") or {}).get("links")   or {}
            delivery = doc.get("delivery") or {}

            doi_raw = (addata.get("doi") or [""])[0].strip()

            # Deduplicate across the two queries
            if doi_raw:
                doi_lower = doi_raw.lower()
                if doi_lower in seen_dois:
                    continue
                seen_dois.add(doi_lower)

            title = (
                (addata.get("atitle") or addata.get("btitle") or
                 display.get("title") or [""])[0]
            )
            raw_authors = addata.get("au") or display.get("creator") or []
            authors = [a for a in raw_authors if a][:10]
            date_raw = (addata.get("date") or display.get("creationdate") or [""])[0]
            year = date_raw[:4] if date_raw else None
            venue = (
                addata.get("jtitle") or addata.get("btitle") or
                display.get("ispartof") or [None]
            )[0]

            abstract_parts = display.get("description") or addata.get("abstract") or []
            abstract = abstract_parts[0] if abstract_parts else None

            oa_url = None
            for link_key in ("linkunpaywall", "linktopdf", "linktohtml"):
                raw = (links.get(link_key) or [""])[0]
                u = _primo_extract_url(raw)
                if u:
                    oa_url = u
                    break

            proxy_url = delivery.get("almaOpenurl") or None

            all_results.append({
                "title": title or "Untitled",
                "authors": authors,
                "year": year,
                "doi": doi_raw or None,
                "abstract": abstract,
                "citations": None,
                "venue": venue,
                "found_in": ["primo_law"],
                "in_zotero": False,
                "has_oa_pdf": bool(oa_url),
                "s2_id": None,
                "_primo_oa_url": oa_url,
                "_primo_proxy_url": proxy_url,
            })

    return all_results


# ---------------------------------------------------------------------------
# SSRN DOI resolution
# ---------------------------------------------------------------------------


async def resolve_ssrn_doi(ssrn_doi: str, client: httpx.AsyncClient) -> dict:
    """Resolve an SSRN DOI to a published DOI and/or OA PDF URLs.

    Queries (in order): OpenAlex, Semantic Scholar, Crossref.

    Returns dict:
        published_doi: str | None — journal/conference DOI if found
        oa_pdf_urls: list[str] — direct OA PDF URLs from any location
        title: str | None — paper title (useful for fallback title search)
        all_dois: list[str] — all known DOIs for this work
    """
    result: dict = {
        "published_doi": None,
        "oa_pdf_urls": [],
        "title": None,
        "all_dois": [ssrn_doi],
    }
    ssrn_norm = ssrn_doi.lower()

    # Step 1 — OpenAlex (best source for version mapping)
    try:
        work = await openalex_work(ssrn_doi)
        if work:
            result["title"] = work.get("title")
            canonical_doi = (work.get("doi") or "").replace("https://doi.org/", "")
            if canonical_doi and canonical_doi.lower() != ssrn_norm:
                result["published_doi"] = canonical_doi
                if canonical_doi not in result["all_dois"]:
                    result["all_dois"].append(canonical_doi)
            # Collect OA PDF URLs from all locations
            for loc in work.get("locations") or []:
                if loc.get("is_oa") and loc.get("pdf_url"):
                    url = loc["pdf_url"]
                    if url not in result["oa_pdf_urls"]:
                        result["oa_pdf_urls"].append(url)
            primary = work.get("primary_location") or {}
            if primary.get("pdf_url") and primary["pdf_url"] not in result["oa_pdf_urls"]:
                result["oa_pdf_urls"].append(primary["pdf_url"])
    except Exception as e:
        logger.debug("OpenAlex SSRN lookup failed for %s: %s", ssrn_doi, e)

    # Step 2 — Semantic Scholar (backup)
    if not result["published_doi"]:
        try:
            headers: dict[str, str] = {}
            if config.semantic_scholar_api_key:
                headers["x-api-key"] = config.semantic_scholar_api_key
            resp = await _request_with_retry(
                client, "GET", f"{S2_BASE}/paper/DOI:{ssrn_doi}",
                params={"fields": "externalIds,title,openAccessPdf"},
                headers=headers,
            )
            if resp.status_code == 200:
                s2_data = resp.json()
                if not result["title"] and s2_data.get("title"):
                    result["title"] = s2_data["title"]
                ext_ids = s2_data.get("externalIds") or {}
                s2_doi = ext_ids.get("DOI")
                if s2_doi and s2_doi.lower() != ssrn_norm:
                    result["published_doi"] = s2_doi
                    if s2_doi not in result["all_dois"]:
                        result["all_dois"].append(s2_doi)
                oa_pdf_url = (s2_data.get("openAccessPdf") or {}).get("url")
                if oa_pdf_url and oa_pdf_url not in result["oa_pdf_urls"]:
                    result["oa_pdf_urls"].append(oa_pdf_url)
        except Exception as e:
            logger.debug("S2 SSRN lookup failed for %s: %s", ssrn_doi, e)

    # Step 3 — Crossref (for is-preprint-of relations)
    if not result["published_doi"]:
        try:
            crossref_data = await crossref_work(ssrn_doi)
            if crossref_data:
                if not result["title"]:
                    titles = crossref_data.get("title") or []
                    result["title"] = titles[0] if titles else None
                relations = crossref_data.get("relation") or {}
                for rel_type in ("is-preprint-of", "is-version-of"):
                    for rel in relations.get(rel_type) or []:
                        if rel.get("id-type") == "doi" and rel.get("id"):
                            published = rel["id"]
                            result["published_doi"] = published
                            if published not in result["all_dois"]:
                                result["all_dois"].append(published)
                            break
                    if result["published_doi"]:
                        break
        except Exception as e:
            logger.debug("Crossref SSRN lookup failed for %s: %s", ssrn_doi, e)

    # Step 4 — Primo law review search (if no published DOI found yet)
    # SSRN papers destined for law reviews often have no cross-linked DOI
    # in OpenAlex/S2/Crossref. Search Primo by title to find the published version.
    if not result["published_doi"] and result.get("title"):
        title = result["title"]

        # Detect whether this SSRN paper is likely a law review article.
        # Check venue field OR common law review citation patterns in the title
        # (e.g. "Article Title, 95 Harv. L. Rev. 123 (2023)").
        venue_str = (result.get("venue") or "").lower()
        title_lower = title.lower()
        _law_signals = [
            "law review", "law journal", "l. rev.", "l.j.",
            "legal stud", "jurisprudence", "law quarterly",
        ]
        is_law = any(sig in venue_str or sig in title_lower for sig in _law_signals)

        if is_law:
            try:
                primo_hits = await primo_search_law_reviews(
                    f"title:{title}",
                    limit=3,
                )
                for hit in primo_hits:
                    hit_doi = hit.get("doi")
                    if hit_doi and hit_doi.lower() != ssrn_doi.lower():
                        result["published_doi"] = hit_doi
                        if hit_doi not in result["all_dois"]:
                            result["all_dois"].append(hit_doi)
                        logger.info(
                            "SSRN law review %s → %s via Primo", ssrn_doi, hit_doi
                        )
                        break
            except Exception as e:
                logger.debug("Primo law review search for SSRN %s failed: %s", ssrn_doi, e)

    return result


async def search_by_title_for_published_version(
    title: str, ssrn_doi: str, client: httpx.AsyncClient
) -> dict | None:
    """Search OpenAlex and S2 by title to find a published version.

    This catches cases where the SSRN DOI isn't linked to the published
    version in metadata, but the same paper exists under a different DOI.
    """
    ssrn_norm = ssrn_doi.lower()

    # OpenAlex title search
    try:
        resp = await _request_with_retry(
            client, "GET", f"{OA_BASE}/works",
            params={"search": title, "per_page": 3, **_openalex_mailto_param()},
            headers=_openalex_headers(),
        )
        if resp.status_code == 200:
            for work in resp.json().get("results") or []:
                work_doi = (work.get("doi") or "").replace("https://doi.org/", "")
                if work_doi and work_doi.lower() != ssrn_norm and not work_doi.startswith("10.2139"):
                    oa_urls = [
                        loc["pdf_url"]
                        for loc in (work.get("locations") or [])
                        if loc.get("is_oa") and loc.get("pdf_url")
                    ]
                    return {
                        "published_doi": work_doi,
                        "oa_pdf_urls": oa_urls,
                        "title": work.get("title"),
                    }
    except Exception as e:
        logger.debug("OpenAlex title search for published version failed: %s", e)

    # S2 title search
    try:
        headers = {}
        if config.semantic_scholar_api_key:
            headers["x-api-key"] = config.semantic_scholar_api_key
        resp = await _request_with_retry(
            client, "GET", f"{S2_BASE}/paper/search",
            params={"query": title, "limit": 3, "fields": "externalIds,title,openAccessPdf"},
            headers=headers,
        )
        if resp.status_code == 200:
            for paper in resp.json().get("data") or []:
                ext_ids = paper.get("externalIds") or {}
                paper_doi = ext_ids.get("DOI")
                if paper_doi and paper_doi.lower() != ssrn_norm and not paper_doi.startswith("10.2139"):
                    oa_url = (paper.get("openAccessPdf") or {}).get("url")
                    return {
                        "published_doi": paper_doi,
                        "oa_pdf_urls": [oa_url] if oa_url else [],
                        "title": paper.get("title"),
                    }
    except Exception as e:
        logger.debug("S2 title search for published version failed: %s", e)

    return None


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
