"""Web search fallback for PDF discovery (Serper / Brave).

Used as a retrieval tier after CORE.ac.uk fails but before the stealth browser.
Searches for direct PDF links and landing pages on trusted academic domains.

Trusted domain allowlist prevents following links to paywalls or unrelated sites.
"""

import logging
import re
from urllib.parse import urlparse

import httpx

from .config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trusted domain allowlist
# ---------------------------------------------------------------------------

TRUSTED_PDF_DOMAINS: frozenset[str] = frozenset({
    # ── Preprint servers & OA repositories ──────────────────────────────
    "arxiv.org", "biorxiv.org", "medrxiv.org", "osf.io", "zenodo.org",
    "hal.science", "hal.archives-ouvertes.fr",

    # ── Major publishers ─────────────────────────────────────────────────
    "dl.acm.org",
    "aclanthology.org", "aclweb.org",
    "ieee.org", "ieeexplore.ieee.org",
    "springer.com", "link.springer.com",
    "nature.com",
    "sciencedirect.com", "elsevier.com",
    "wiley.com", "onlinelibrary.wiley.com",
    "tandfonline.com", "taylorandfrancis.com",
    "sagepub.com", "journals.sagepub.com",
    "oup.com", "academic.oup.com",
    "cambridge.org",
    "jstor.org",
    "cell.com",
    "plos.org", "journals.plos.org",
    "mdpi.com",
    "frontiersin.org",
    "peerj.com",
    "degruyter.com",
    "emerald.com",
    "karger.com",
    "thieme-connect.com",
    "liebertpub.com",
    "worldscientific.com",
    "ingentaconnect.com",
    "brill.com",
    "nomos-elibrary.de",
    "muse.jhu.edu",
    "heinonline.org",
    "bloomsburycollections.com",

    # ── CS conferences & proceedings ─────────────────────────────────────
    "openreview.net",
    "proceedings.neurips.cc",
    "proceedings.mlr.press",
    "aaai.org",
    "ijcai.org",
    "usenix.org",
    "ecai.eu",

    # ── Government, medical, policy ──────────────────────────────────────
    "ncbi.nlm.nih.gov",
    "europepmc.org",
    "govinfo.gov",
    "who.int",

    # ── Law reviews with non-.edu domains ────────────────────────────────
    "californialawreview.org",
    "columbialawreview.org",
    "harvardlawreview.org",
    "michiganlawreview.org",
    "nyulawreview.org",
    "stanfordlawreview.org",
    "texaslawreview.org",
    "uclalawreview.org",
    "pennlawreview.com",
    "virginialawreview.org",
    "yalelawjournal.org",
    # Specialty / tech / comms law journals (non-.edu self-published)
    "fclj.org",           # Federal Communications Law Journal (Indiana U.)
    "jolt.law.harvard.edu",
    "jtl.columbia.edu",

    # ── Other OA / aggregators ────────────────────────────────────────────
    "ssrn.com", "papers.ssrn.com",
    "core.ac.uk",
    "lawreviewcommons.com",
    "legalscholarship.org",
    "philpapers.org",
    "repec.org",
    "researchgate.net",
    "academia.edu",
    "semanticscholar.org",
})

# Domain suffix patterns (institutional repositories)
TRUSTED_DOMAIN_SUFFIXES: tuple[str, ...] = (
    ".edu",       # US universities
    ".ac.uk",     # UK universities
    ".ac.jp",     # Japanese universities
    ".edu.au",    # Australian universities
    ".edu.cn",    # Chinese universities
    ".go.jp",     # Japanese government
)

# Infix patterns (German universities, research institutes)
TRUSTED_DOMAIN_INFIXES: tuple[str, ...] = (
    ".uni-",      # German universities (uni-mannheim.de, etc.)
    ".mpg.de",    # Max Planck
    ".cnrs.fr",   # CNRS
    ".inria.fr",  # INRIA
    ".csic.es",   # Spanish CSIC
    ".nii.ac.jp", # Japanese NII
)

# Law school repository patterns (bepress Digital Commons)
_LAW_REPO_RE = re.compile(
    r"(scholarship|digitalcommons|repository|lawdigitalcommons)"
    r"\.law\.\w+\.edu",
    re.IGNORECASE,
)

# Standalone law journal websites not covered by TRUSTED_PDF_DOMAINS.
# Catches:  *lawreview.org/com  *lawjournal.org/com  *legalforum.org
#           abbreviation sites ending in "lj" or "lr" (e.g. fclj.org, ualr.org → false
#           positive risk low since those .org domains are rare outside law)
_STANDALONE_LAW_ORG_RE = re.compile(
    r"^[a-z0-9-]*(lawreview|lawjournal|lawforum|lawquarterly"
    r"|legalstudies|legaltheory|legalforum)\.(org|com)$"
    r"|^[a-z]{1,6}(lj|lr)\.(org|com)$",
    re.IGNORECASE,
)


def _is_trusted_domain(url: str) -> bool:
    """Return True if the URL's hostname is on the trusted academic allowlist."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False

    # Strip leading www.
    host = host.lstrip("www.")

    if host in TRUSTED_PDF_DOMAINS:
        return True
    if any(host.endswith(sfx) for sfx in TRUSTED_DOMAIN_SUFFIXES):
        return True
    if any(infix in host for infix in TRUSTED_DOMAIN_INFIXES):
        return True
    if _LAW_REPO_RE.search(host):
        return True
    if _STANDALONE_LAW_ORG_RE.search(host):
        return True
    return False


# ---------------------------------------------------------------------------
# Law review detection
# ---------------------------------------------------------------------------

_LAW_KEYWORDS: list[str] = [
    # Full names
    "law review", "law journal", "legal studies", "jurisprudence",
    "law quarterly", "juridical", "law & ", "law and ",
    "law forum", "legal theory", "legal analysis",
    "legal ethics", "legal history", "law bulletin", "legal scholarship",
    "international law", "comparative law", "constitutional law",
    "administrative law", "criminal law", "environmental law",
    "intellectual property law", "corporate law", "tax law",
    # Bluebook abbreviations — CRITICAL, this is how most citations appear
    "l. rev.", "l.j.", "l. rev ", "l.j ",
    " l. ",          # Catches "Crim. L. Forum", "Int'l L. Q.", etc.
    "crim. l.", "int'l l.", "const. l.", "envtl. l.",
    "j.l. &", "j.l. ", "j. legal", "j. legis.",
    # Specific well-known journals (Bluebook abbreviated)
    "yale l.j.", "harv. l. rev.", "stan. l. rev.", "colum. l. rev.",
    "mich. l. rev.", "u. chi. l. rev.", "nyu l. rev.",
    "geo. l.j.", "va. l. rev.", "tex. l. rev.",
    "cornell l. rev.", "duke l.j.", "nw. u. l. rev.",
    "calif. l. rev.", "u. pa. l. rev.", "minn. l. rev.",
    # HeinOnline collection markers
    "hein.journals",
]


def _looks_like_law_review(metadata: dict) -> bool:
    """Detect whether a paper is likely in a law review / legal journal.

    Checks venue, journal name, and publication title against known patterns.
    Includes Bluebook abbreviation patterns (e.g. "L. Rev.", "L.J.").
    """
    venue = (
        metadata.get("venue", "") or
        metadata.get("journal", "") or
        metadata.get("publicationTitle", "") or
        metadata.get("container_title", "") or
        ""
    ).lower()
    return any(kw in venue for kw in _LAW_KEYWORDS)


def _looks_like_law_review_by_venue(venue: str) -> bool:
    """Convenience wrapper that takes a venue string directly."""
    return _looks_like_law_review({"venue": venue})


# ---------------------------------------------------------------------------
# Search API calls
# ---------------------------------------------------------------------------

async def _search_serper(query: str, client: httpx.AsyncClient) -> list[dict]:
    """Search Google via Serper.dev.

    POST https://google.serper.dev/search
    Headers: X-API-KEY
    Body: {"q": query, "num": 5}
    """
    if not config.serper_api_key:
        return []
    try:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": config.serper_api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.debug("Serper returned %s", resp.status_code)
            return []
        return [
            {"url": r["link"], "title": r.get("title", ""), "source": "serper"}
            for r in resp.json().get("organic") or []
            if r.get("link")
        ]
    except Exception as e:
        logger.debug("Serper search failed: %s", e)
        return []


async def _search_brave(query: str, client: httpx.AsyncClient) -> list[dict]:
    """Search via Brave Search API.

    GET https://api.search.brave.com/res/v1/web/search
    Headers: X-Subscription-Token
    """
    if not config.brave_search_api_key:
        return []
    try:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": config.brave_search_api_key,
            },
            params={"q": query, "count": 5},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.debug("Brave Search returned %s", resp.status_code)
            return []
        return [
            {"url": r["url"], "title": r.get("title", ""), "source": "brave"}
            for r in (resp.json().get("web") or {}).get("results") or []
            if r.get("url")
        ]
    except Exception as e:
        logger.debug("Brave search failed: %s", e)
        return []


async def search_for_pdf(
    title: str,
    authors: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Search for a PDF via Serper and/or Brave.

    Issues a single query per provider:
      '"Exact Paper Title" {first_author_surname} filetype:pdf'

    The filetype:pdf constraint is mandatory — without it, Google/Brave return
    CVs, syllabi, reading lists, and bibliographies that mention the paper but
    are not the paper itself.  Landing pages are handled by the stealth browser
    tier (step 3) and do not belong in the web search results.

    Returns list of {url, title, source} dicts, filtered to trusted domains.
    """
    if not config.serper_api_key and not config.brave_search_api_key:
        return []

    _owned = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        _owned = True

    try:
        quoted_title = f'"{title}"'
        author_suffix = ""
        if authors:
            # Use surname only (last word of first author name)
            first_author = authors[0]
            surname = first_author.split()[-1] if first_author.split() else first_author
            if surname:
                author_suffix = f" {surname}"

        # Single filetype:pdf query only — no landing-page query
        query_pdf = f"{quoted_title}{author_suffix} filetype:pdf"

        # Run the PDF query against all configured providers
        raw_results: list[dict] = []
        for fn in (_search_serper, _search_brave):
            try:
                hits = await fn(query_pdf, client)
                raw_results.extend(hits)
            except Exception:
                pass

        # Deduplicate by URL, filter to trusted domains
        seen_urls: set[str] = set()
        filtered: list[dict] = []
        for r in raw_results:
            url = r["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if _is_trusted_domain(url):
                filtered.append(r)

        logger.debug(
            "Web search for %r: %d raw hits, %d trusted",
            title[:60], len(raw_results), len(filtered),
        )
        return filtered
    finally:
        if _owned:
            await client.aclose()


# ---------------------------------------------------------------------------
# HeinOnline retrieval via Scrapling persistent session
# ---------------------------------------------------------------------------

async def fetch_from_heinonline(
    title: str,
    client: httpx.AsyncClient,
) -> "Path | None":
    """Search HeinOnline and download a law review PDF via a persistent session.

    Requires: GOST_PROXY_URL (institutional access) + SCRAPLING_MCP_URL.
    Uses the open_session / fetch / close_session flow from the Scrapling MCP
    server to maintain cookies across the multi-step HeinOnline download.
    """
    from pathlib import Path

    if not (config.gost_proxy_url and config.scrapling_mcp_url):
        logger.debug("HeinOnline: skipping (no proxy or Scrapling MCP URL)")
        return None

    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.session import ClientSession
    except ImportError:
        return None

    from urllib.parse import quote

    mcp_url = config.scrapling_mcp_url.strip()

    try:
        async with streamablehttp_client(mcp_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # Step 1: Open persistent session with proxy + Cloudflare solving
                open_result = await session.call_tool("open_session", {
                    "session_type": "dynamic",
                    "proxy": config.gost_proxy_url,
                    "solve_cloudflare": True,
                })
                if open_result.isError:
                    logger.debug("HeinOnline: open_session failed")
                    return None
                session_data = _parse_json_result(open_result)
                session_id = (session_data or {}).get("session_id")
                if not session_id:
                    logger.debug("HeinOnline: no session_id in open_session response")
                    return None

                try:
                    # Step 2: Search HeinOnline
                    search_url = (
                        f"https://heinonline.org/HOL/LuceneSearch?"
                        f"terms={quote(title)}&collection=all&searchtype=advanced"
                        f"&typea=text&submit=Go&all=true"
                    )
                    search_html = await _scrapling_session_fetch(session, session_id, search_url)
                    if not search_html:
                        return None

                    # Step 3: Extract first result link
                    article_url = _extract_heinonline_result(search_html, "https://heinonline.org")
                    if not article_url:
                        logger.debug("HeinOnline: no search result found for %r", title[:60])
                        return None

                    # Step 4: Fetch article page
                    article_html = await _scrapling_session_fetch(session, session_id, article_url)
                    if not article_html:
                        return None

                    # Step 5: Find PDF download button
                    pdf_page_url = _extract_hein_pdf_button(article_html, article_url)
                    if not pdf_page_url:
                        logger.debug("HeinOnline: no PDF button found on %s", article_url)
                        return None

                    # Step 6: Fetch PDF staging page → meta refresh URL
                    staging_html = await _scrapling_session_fetch(session, session_id, pdf_page_url)
                    if not staging_html:
                        return None

                    actual_pdf_url = _extract_meta_refresh_url(staging_html, pdf_page_url)
                    if not actual_pdf_url:
                        logger.debug("HeinOnline: no meta-refresh URL on staging page")
                        return None

                    # Step 7: Download the actual PDF via proxied HTTP
                    from .pdf_fetcher import fetch_proxied
                    path = await fetch_proxied(actual_pdf_url)
                    if path:
                        logger.info("Downloaded law review PDF from HeinOnline: %s", actual_pdf_url)
                    return path

                finally:
                    try:
                        await session.call_tool("close_session", {"session_id": session_id})
                    except Exception:
                        pass

    except Exception as e:
        logger.debug("HeinOnline fetch failed: %s", e)
    return None


def _parse_json_result(mcp_result) -> dict | None:
    """Extract a JSON dict from an MCP tool result."""
    import json
    for item in mcp_result.content:
        if hasattr(item, "text") and item.text:
            try:
                return json.loads(item.text)
            except Exception:
                pass
    return None


async def _scrapling_session_fetch(session, session_id: str, url: str) -> str | None:
    """Fetch a URL using an existing Scrapling MCP session."""
    from .pdf_fetcher import _unwrap_scrapling_response
    try:
        result = await session.call_tool("fetch", {
            "session_id": session_id,
            "url": url,
            "extraction_type": "html",
        })
        if result.isError:
            return None
        parts = [
            item.text for item in result.content
            if hasattr(item, "text") and item.text
        ]
        raw = "\n".join(parts)
        html, _ = _unwrap_scrapling_response(raw)
        return html or None
    except Exception as e:
        logger.debug("Scrapling session fetch failed for %s: %s", url, e)
    return None


def _extract_heinonline_result(html: str, base_url: str) -> str | None:
    """Extract the first article URL from a HeinOnline search results page."""
    from urllib.parse import urljoin
    # HeinOnline result links are in elements with class lucene_search_result_b
    match = re.search(
        r'class=["\'][^"\']*lucene_search_result_b[^"\']*["\'][^>]*>.*?<a[^>]+href=["\']([^"\']+)["\']',
        html, re.DOTALL | re.IGNORECASE,
    )
    if match:
        href = match.group(1)
        return urljoin(base_url, href)

    # Fallback: any HOL link
    match = re.search(r'href=["\'](/HOL/Page\?[^"\']+)["\']', html, re.IGNORECASE)
    if match:
        return urljoin(base_url, match.group(1))
    return None


def _extract_hein_pdf_button(html: str, base_url: str) -> str | None:
    """Extract the PDF download button URL from a HeinOnline article page."""
    from urllib.parse import urljoin
    # Zotero translator looks for [data-original-title*="Download PDF"]
    match = re.search(
        r'data-original-title=["\'][^"\']*[Dd]ownload\s*PDF[^"\']*["\'][^>]*>.*?href=["\']([^"\']+)["\']|'
        r'href=["\']([^"\']+)["\'][^>]*data-original-title=["\'][^"\']*[Dd]ownload\s*PDF[^"\']*["\']',
        html, re.DOTALL | re.IGNORECASE,
    )
    if match:
        href = match.group(1) or match.group(2)
        return urljoin(base_url, href)

    # Fallback: any link with "PDF" in the text near download
    match = re.search(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>[^<]*[Dd]ownload\s*PDF[^<]*</a>',
        html, re.IGNORECASE,
    )
    if match:
        return urljoin(base_url, match.group(1))
    return None


def _extract_meta_refresh_url(html: str, base_url: str) -> str | None:
    """Extract URL from <meta http-equiv="Refresh" content="N;url=...">."""
    from urllib.parse import urljoin
    match = re.search(
        r'<meta[^>]+http-equiv=["\']?Refresh["\']?[^>]+content=["\']'
        r'(\d+)\s*;\s*url=([^"\'>\s]+)',
        html, re.IGNORECASE,
    )
    if not match:
        # Also try the reversed attribute order
        match = re.search(
            r'<meta[^>]+content=["\'](\d+)\s*;\s*url=([^"\'>\s]+)["\'][^>]+'
            r'http-equiv=["\']?Refresh["\']?',
            html, re.IGNORECASE,
        )
    if not match:
        return None
    url = match.group(2).strip().strip("'\"")
    return urljoin(base_url, url)


# ---------------------------------------------------------------------------
# SSRN cookie injection
# ---------------------------------------------------------------------------

def _parse_cookies(raw: str) -> list[dict] | None:
    """Parse SSRN_COOKIES in JSON or Cookie Quick Manager TSV format.

    JSON format (array of objects):
        [{"name":"SSRN_TOKEN","value":"...","domain":".ssrn.com","path":"/"}]

    Cookie Quick Manager TSV format (tab-separated, one cookie per line):
        .ssrn.com \\t false \\t / \\t false \\t 1779808177 \\t name \\t value
        Columns: domain, hostOnly, path, secure, expires, name, value
    """
    import json as _json

    raw = raw.strip()
    if not raw:
        return None

    # Try JSON first
    if raw.startswith("[") or raw.startswith("{"):
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            return parsed
        except Exception as e:
            logger.warning("SSRN_COOKIES JSON parse failed: %s", e)
            return None

    # TSV format: domain TAB hostOnly TAB path TAB secure TAB expires TAB name TAB value
    cookies = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _host_only, path, secure, expires, name, value = (
            parts[0], parts[1], parts[2], parts[3], parts[4], parts[5],
            "\t".join(parts[6:]),  # value may theoretically contain tabs
        )
        cookie: dict = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": secure.lower() == "true",
        }
        try:
            exp = int(expires)
            if exp > 0:
                cookie["expires"] = exp
        except ValueError:
            pass
        cookies.append(cookie)

    if not cookies:
        logger.warning("SSRN_COOKIES TSV parse produced no cookies")
        return None
    return cookies


async def fetch_ssrn_with_cookies(url: str) -> str | None:
    """Fetch an SSRN URL using injected cookies via Scrapling MCP persistent session.

    Opens a session with the cookies from SSRN_COOKIES env var, navigates to
    the URL, and returns the response HTML.
    """
    if not (config.ssrn_cookies and config.scrapling_mcp_url):
        return None

    cookies = _parse_cookies(config.ssrn_cookies)
    if not cookies:
        return None

    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.session import ClientSession
    except ImportError:
        return None

    mcp_url = config.scrapling_mcp_url.strip()

    try:
        async with streamablehttp_client(mcp_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                open_args: dict = {
                    "session_type": "dynamic",
                    "cookies": cookies,
                    "solve_cloudflare": True,
                }
                if config.gost_proxy_url:
                    open_args["proxy"] = config.gost_proxy_url

                open_result = await session.call_tool("open_session", open_args)
                if open_result.isError:
                    logger.debug("SSRN cookie session: open_session failed")
                    return None
                session_data = _parse_json_result(open_result)
                session_id = (session_data or {}).get("session_id")
                if not session_id:
                    return None

                try:
                    html = await _scrapling_session_fetch(session, session_id, url)
                    return html
                finally:
                    try:
                        await session.call_tool("close_session", {"session_id": session_id})
                    except Exception:
                        pass

    except Exception as e:
        logger.debug("SSRN cookie fetch failed for %s: %s", url, e)
    return None
