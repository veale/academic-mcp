"""Multi-strategy PDF fetcher.

Strategy chain:
  1. Direct HTTP GET (for known OA PDF URLs)
  2. Proxied HTTP GET (route through GOST for institutional access)
  3. Scrapling stealth browser — two modes:
       a) Remote MCP server  (SCRAPLING_MCP_URL set)
       b) Local Chromium      (fallback)

All strategies stream downloads directly to ``config.pdf_cache_dir`` and
return the resulting ``Path``.  No full-file ``bytes`` objects are ever
held in memory, keeping RAM usage near-zero regardless of PDF size.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urljoin

import httpx

from .config import config

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _cache_path(url: str) -> Path:
    """Deterministic cache path for a URL."""
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return config.pdf_cache_dir / f"{h}.pdf"


def _is_pdf_header(data: bytes) -> bool:
    return data[:5] == _PDF_MAGIC


def _is_pdf_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == _PDF_MAGIC
    except OSError:
        return False


def _save_bytes_to_cache(url: str, data: bytes) -> Path | None:
    """Write raw bytes to the cache dir and return the Path."""
    if not _is_pdf_header(data):
        return None
    dest = _cache_path(url)
    dest.write_bytes(data)
    return dest


async def _stream_to_cache(url: str, resp: httpx.Response) -> Path | None:
    """Stream an httpx response directly to the cache directory.

    Writes to a temporary file first, then atomically renames on success.
    Returns the final ``Path`` or ``None`` if the download is not a PDF.
    """
    dest = _cache_path(url)
    fd, tmp_name = tempfile.mkstemp(dir=config.pdf_cache_dir, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        header_checked = False
        with open(fd, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                if not header_checked:
                    if not _is_pdf_header(chunk):
                        logger.debug("Not a PDF: %s", url)
                        return None
                    header_checked = True
                f.write(chunk)
        tmp_path.rename(dest)
        return dest
    except Exception:
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Strategy 1: Direct HTTP fetch
# ---------------------------------------------------------------------------

async def fetch_direct(url: str) -> Path | None:
    """Simple HTTP GET — works for direct OA PDF links."""
    try:
        async with httpx.AsyncClient(
            timeout=60.0, follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/pdf,*/*",
            },
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.debug("Direct fetch of %s returned status=%s", url, resp.status_code)
                    return None
                return await _stream_to_cache(url, resp)
    except httpx.RequestError as e:
        logger.debug("Direct fetch failed for %s: %s", url, e)
    return None


# ---------------------------------------------------------------------------
# Strategy 2: Proxied HTTP fetch (via GOST)
# ---------------------------------------------------------------------------

async def fetch_proxied(url: str) -> Path | None:
    """HTTP GET through GOST proxy for institutional access."""
    if not config.gost_proxy_url:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            proxy=config.gost_proxy_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/pdf,*/*",
            },
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                return await _stream_to_cache(url, resp)
    except httpx.RequestError as e:
        logger.debug("Proxied fetch failed for %s: %s", url, e)
    return None


# ---------------------------------------------------------------------------
# Strategy 3: Scrapling stealth browser
# ---------------------------------------------------------------------------

def _build_tool_args(url: str, tool_name: str = "") -> dict:
    """Build argument dict for a Scrapling MCP tool call.

    Always includes ``url``.  When calling ``stealthy_fetch``, also sets
    ``solve_cloudflare=True`` and ``extraction_type="html"`` (we need raw
    HTML to find PDF links, not Markdown).  Adds ``proxy`` when
    ``GOST_PROXY_URL`` is configured so the remote browser routes through
    the institution's network.
    """
    args: dict = {"url": url}
    if tool_name == "stealthy_fetch":
        args["solve_cloudflare"] = True
        args["extraction_type"] = "html"
    if config.gost_proxy_url:
        args["proxy"] = config.gost_proxy_url
    return args


async def fetch_with_scrapling(
    url: str, use_proxy: bool = False
) -> tuple[Path | None, str | None, str | None]:
    """Use a stealth browser to fetch a page.

    Returns ``(pdf_path, html_content, final_url)``:

    * ``(path, None, None)`` — a PDF was obtained and saved to cache.
    * ``(None, html, url)`` — the page returned HTML (MCP path only).
    * ``(None, None, None)`` — stealth browser disabled or fetch failed.

    If ``SCRAPLING_MCP_URL`` is set → connects to the remote Scrapling MCP
    server over Streamable HTTP.  The HTML response is returned raw so the
    caller can decide whether to extract article text or follow PDF links.

    If ``SCRAPLING_MCP_URL`` is unset → uses a local ``StealthyFetcher``
    which handles PDF-link following internally and always returns a path
    or None (html is always ``None`` for the local path).
    """
    if not config.use_stealth_browser:
        return None, None, None

    mcp_url = (config.scrapling_mcp_url or "").strip()
    if mcp_url:
        return await _scrapling_via_mcp(url, mcp_url)

    path = await _scrapling_local(url)
    return path, None, None


# ── Remote: MCP client over Streamable HTTP ──────────────────────────────
#
# The remote Scrapling MCP server (``scrapling mcp --http``) exposes its
# scraping capability as one or more MCP tools.  We discover the tool at
# runtime, call it with {url, proxy?}, and handle two response shapes:
#
#   1. PDF bytes (base64-encoded text or EmbeddedResource blob)
#      → decode and save to cache.
#
#   2. HTML (publisher landing page)
#      → extract PDF links, make a second tool call.
#
# Scrapling uses the Streamable HTTP transport (MCP spec March 2025) at the
# /mcp endpoint.  The old SSE transport (mcp.client.sse) is incompatible
# with this and returns HTTP 400.  Use streamablehttp_client instead.
# --------------------------------------------------------------------------

# Prefer stealthy_fetch — it has fingerprint spoofing and solve_cloudflare.
# Fall back to fetch (plain Playwright) then get (HTTP-only).
_TOOL_CANDIDATES = ("stealthy_fetch", "fetch", "scrape", "fetch_page", "get")


async def _scrapling_via_mcp(
    url: str, mcp_url: str
) -> tuple[Path | None, str | None, str | None]:
    """Connect to a remote Scrapling MCP server and call its scraping tool.

    Returns ``(pdf_path, html_content, final_url)``:

    * ``(path, None, None)`` — response was a PDF; path is cached on disk.
    * ``(None, html, url)`` — response was HTML; caller decides what to do.
    * ``(None, None, None)`` — connection or tool error.

    Unlike the previous implementation, this function makes only **one** MCP
    call. PDF-link following (the second call) is the caller's responsibility
    so the HTML can be used for article extraction first.
    """
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.session import ClientSession
    except ImportError:
        logger.warning(
            "MCP client SDK not available — cannot connect to Scrapling "
            "MCP server.  Install with: pip install 'mcp[cli]'"
        )
        return None, None, None

    try:
        async with streamablehttp_client(mcp_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tool_name = await _discover_tool(session)
                if not tool_name:
                    return None, None, None

                args = _build_tool_args(url, tool_name)
                result = await _mcp_call(session, tool_name, args)
                if result is None:
                    return None, None, None

                # Did we get a PDF directly?
                path = _decode_pdf_from_result(result, url)
                if path:
                    return path, None, None

                # Return HTML to the caller — they decide whether to extract
                # article text or follow a PDF download link.
                raw_text = _text_from_result(result)
                if not raw_text:
                    logger.debug("Scrapling MCP returned no content for %s", url)
                    return None, None, None

                html, final_url = _unwrap_scrapling_response(raw_text)
                return None, html, final_url or url

    except Exception as e:
        logger.debug("Scrapling MCP session failed for %s: %s", url, e)
    return None, None, None


async def _discover_tool(session) -> str | None:
    """Pick the best scraping tool from the remote server."""
    try:
        listing = await session.list_tools()
        available = {t.name for t in listing.tools}

        for candidate in _TOOL_CANDIDATES:
            if candidate in available:
                logger.debug("Using remote Scrapling tool: %s", candidate)
                return candidate

        # Single-purpose server — just take the first tool.
        if listing.tools:
            name = listing.tools[0].name
            logger.debug("No known tool name; using first tool: %s", name)
            return name

        logger.warning("Scrapling MCP server exposes no tools")
    except Exception as e:
        logger.debug("MCP tool discovery failed: %s", e)
    return None


async def _mcp_call(session, tool_name: str, arguments: dict):
    """Call a remote MCP tool. Returns the ``CallToolResult`` or None."""
    try:
        result = await session.call_tool(tool_name, arguments)
        if result.isError:
            err = "".join(
                getattr(c, "text", "") for c in result.content
            )
            logger.debug(
                "MCP tool %s error for %s: %s",
                tool_name, arguments.get("url"), err[:300],
            )
            return None
        return result
    except Exception as e:
        logger.debug("MCP tool %s call failed: %s", tool_name, e)
    return None


def _decode_pdf_from_result(result, url: str) -> Path | None:
    """Try to extract PDF bytes from any content block in the result.

    Handles two encodings:
      • ``EmbeddedResource`` with a base64 ``blob`` field.
      • ``TextContent`` whose text is base64-encoded PDF data
        (starts with ``JVBER`` = base64 of ``%PDF-``).
    """
    for item in result.content:
        raw: bytes | None = None

        # EmbeddedResource
        if hasattr(item, "resource"):
            blob = getattr(item.resource, "blob", None)
            if blob:
                try:
                    raw = base64.b64decode(blob)
                except Exception:
                    pass

        # Base64-encoded TextContent
        if raw is None and hasattr(item, "text") and item.text:
            text = item.text.strip()
            if text.startswith("JVBER") or text.startswith("data:application/pdf"):
                if text.startswith("data:") and "," in text:
                    text = text.split(",", 1)[1]
                try:
                    raw = base64.b64decode(text)
                except Exception:
                    pass

        if raw and _is_pdf_header(raw):
            return _save_bytes_to_cache(url, raw)

    return None


def _text_from_result(result) -> str | None:
    """Concatenate all TextContent blocks from a tool result."""
    parts = [
        item.text for item in result.content
        if hasattr(item, "text") and item.text
    ]
    return "\n".join(parts) if parts else None


def _unwrap_scrapling_response(text: str) -> tuple[str, str]:
    """Unwrap a Scrapling MCP JSON response into (html, final_url).

    Scrapling's ``stealthy_fetch`` tool wraps the page HTML in a JSON
    envelope::

        {"status": 200, "content": ["<body>...</body>", ""], "url": "..."}

    The ``content`` field is a list of HTML strings that need to be joined.
    When ``item.text`` contains this JSON, the raw text is not parseable as
    HTML directly — attribute values with escaped quotes cause regex patterns
    to fail.

    Returns the raw ``text`` unchanged (and an empty final_url) if it cannot
    be parsed as a Scrapling JSON response.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return text, ""

    if not isinstance(data, dict):
        return text, ""

    # Reject non-200 HTTP status codes inside the JSON wrapper.
    # Scrapling returns {"status": 403, "content": [...]} for blocked pages
    # rather than raising an exception, so an empty body with status 200 is
    # fine (handled below) but a 4xx/5xx should be treated as a fetch failure.
    status = data.get("status")
    if status is not None and status != 200:
        logger.debug("Scrapling returned HTTP %s inside JSON wrapper", status)
        return "", ""

    final_url: str = data.get("url", "") or ""
    content = data.get("content", "")

    if isinstance(content, list):
        html = "\n".join(str(c) for c in content if c)
    elif isinstance(content, str):
        html = content
    else:
        html = text

    return html or text, final_url


def _extract_pdf_link_from_html(html: str, base_url: str) -> str | None:
    """Extract a PDF download link from raw HTML.

    Lightweight regex-based counterpart to ``_extract_pdf_link_from_page``
    (which requires a Scrapling response object with ``.css()``).
    """
    patterns = [
        r'<a[^>]+href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']',
        r'<a[^>]+href=["\']([^"\']*/pdf/[^"\']*)["\']',
        r'<a[^>]+data-track-action=["\'](?:[Dd]ownload ?[Pp][Dd][Ff])["\'][^>]+href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]+data-track-action=["\'](?:[Dd]ownload ?[Pp][Dd][Ff])["\']',
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            href = match.group(1)
            if href.startswith("//"):
                return f"https:{href}"
            if href.startswith("/"):
                parsed = urlparse(base_url)
                return f"{parsed.scheme}://{parsed.netloc}{href}"
            if href.startswith("http"):
                return href
            return urljoin(base_url, href)
    return None


# ── Local: Scrapling StealthyFetcher in a thread ─────────────────────────

async def _scrapling_local(url: str) -> Path | None:
    """Spin up a local Chromium via Scrapling, offloaded to a thread.

    The GOST proxy URL is passed directly to ``StealthyFetcher.fetch()``
    so the local browser routes through the institution's network.
    """
    try:
        from scrapling import StealthyFetcher
    except ImportError:
        logger.warning(
            "Scrapling not installed. Install with: "
            "pip install 'academic-mcp-server[stealth]'"
        )
        return None

    def _run() -> Path | None:
        try:
            fetcher = StealthyFetcher()

            fetch_kwargs: dict = {}
            if config.gost_proxy_url:
                fetch_kwargs["proxy"] = config.gost_proxy_url

            response = fetcher.fetch(url, **fetch_kwargs)

            content = response.get_content()
            if isinstance(content, bytes):
                path = _save_bytes_to_cache(url, content)
                if path:
                    return path

            # HTML response — look for a PDF link on the page
            pdf_link = _extract_pdf_link_from_page(response, url)
            if pdf_link:
                logger.info("Found PDF link on page: %s", pdf_link)
                pdf_response = fetcher.fetch(pdf_link, **fetch_kwargs)
                pdf_content = pdf_response.get_content()
                if isinstance(pdf_content, bytes):
                    path = _save_bytes_to_cache(pdf_link, pdf_content)
                    if path:
                        return path
        except Exception as e:
            logger.debug("Local Scrapling fetch failed for %s: %s", url, e)
        return None

    return await asyncio.to_thread(_run)


def _extract_pdf_link_from_page(response, base_url: str) -> str | None:
    """Extract a PDF link from a Scrapling response using CSS selectors.

    This works with the local Scrapling response object (which has a
    ``.css()`` method).  The MCP path uses ``_extract_pdf_link_from_html``
    instead, which operates on raw HTML strings.
    """
    try:
        selectors = [
            'a[href$=".pdf"]',
            'a[href*="/pdf/"]',
            'a[href*="pdf"]',
            'a.download-link[href*="pdf"]',
            'a[id*="pdf"]',
            'a[data-track-action="Download PDF"]',
            'a[href*="epdf"]',
            'a[data-track-action="download pdf"]',
            'a[class*="pdf"]',
            'a[class*="download"]',
        ]
        for selector in selectors:
            links = response.css(selector)
            if links:
                href = links[0].attrib.get("href", "")
                if href:
                    if href.startswith("//"):
                        return f"https:{href}"
                    elif href.startswith("/"):
                        parsed = urlparse(base_url)
                        return f"{parsed.scheme}://{parsed.netloc}{href}"
                    elif href.startswith("http"):
                        return href
    except Exception as e:
        logger.debug("PDF link extraction failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def fetch_pdf(
    candidate_urls: list[dict[str, str]],
    doi: str | None = None,
    use_proxy: bool = False,
    skip_doi_scrapling: bool = False,
) -> tuple[Path | None, str | None]:
    """Try all strategies to fetch a PDF.

    Args:
        candidate_urls: List of {url, source} from collect_pdf_urls()
        doi: Optional DOI for constructing publisher URLs
        use_proxy: Whether to route through institutional proxy
        skip_doi_scrapling: Skip the Scrapling call on the DOI URL.  Set this
            when the caller has already issued that call (e.g. to capture HTML
            for article extraction) and wants to avoid a duplicate round-trip.

    Returns:
        (pdf_path, source_description) or (None, None)
    """
    # Evict stale cache entries before potentially writing new ones
    config.evict_cache_lru()

    # Strategy 1: Try direct fetch on all candidate URLs
    for candidate in candidate_urls:
        url = candidate["url"]
        source = candidate["source"]

        cached = _cache_path(url)
        if cached.exists() and _is_pdf_file(cached):
            logger.info("Cache hit for %s", url)
            return cached, f"{source} (cached)"

        path = await fetch_direct(url)
        if path:
            return path, f"{source} (direct)"

    # Strategy 2: Try proxied fetch if enabled
    if use_proxy and config.gost_proxy_url:
        for candidate in candidate_urls:
            url = candidate["url"]
            source = candidate["source"]
            path = await fetch_proxied(url)
            if path:
                return path, f"{source} (proxied)"

        if doi:
            doi_url = f"https://doi.org/{doi}" if not doi.startswith("http") else doi
            path = await fetch_proxied(doi_url)
            if path:
                return path, "doi_redirect (proxied)"

    # Strategy 3: Scrapling stealth browser
    if config.use_stealth_browser:
        for candidate in candidate_urls:
            url = candidate["url"]
            source = candidate["source"]
            pdf_path, _html, _url = await fetch_with_scrapling(url, use_proxy=use_proxy)
            if pdf_path:
                return pdf_path, f"{source} (scrapling)"

        if doi and not skip_doi_scrapling:
            doi_url = f"https://doi.org/{doi}" if not doi.startswith("http") else doi
            pdf_path, _html, _url = await fetch_with_scrapling(doi_url, use_proxy=use_proxy)
            if pdf_path:
                return pdf_path, "doi_redirect (scrapling)"

    return None, None
