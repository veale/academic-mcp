"""core.documents — read any document at a URL (PDF, Word, PowerPoint, Excel).

General-purpose counterpart to ``fetch_fulltext``: it is not tied to a
scholarly work. HTML scrapers return raw bytes for a PDF or Office file,
which is useless to a model; this downloads the file, converts it to text,
and stores it in the article cache so the usual navigation (sections,
section, preview, range, search_in_article) works on it.

Conversion:
  * PDF  → the server's own PDF pipeline (pymupdf4llm Markdown, heading
           detection, OCR for scans); markitdown is the fallback.
  * Word / PowerPoint / Excel / EPUB / CSV → markitdown.
  * An HTML page → markitdown's HTML converter (or trafilatura), plus a list
           of document links found on the page.

Nothing is written to Zotero unless the caller asks (``save_to_zotero``).
The text is cached under ``url:<sha256-16>`` — the same key
``fetch_fulltext(url=...)`` uses — and is marked so that the DOI identity
checks and auto-import never treat it as a scholarly work.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .. import content_extractor, pdf_work, text_cache
from ..config import config

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "document_url"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ACCEPT = (
    "application/pdf,application/vnd.openxmlformats-officedocument.*,"
    "application/vnd.ms-excel,application/msword,application/epub+zip,"
    "text/csv,text/html;q=0.8,*/*;q=0.5"
)
_MAX_BYTES = int(os.getenv("DOCUMENT_MAX_BYTES", str(150 * 1024 * 1024)))
_MAX_HTML_BYTES = 8 * 1024 * 1024
# Short documents come back whole under mode="auto"; longer ones as a map.
_AUTO_FULL_CHARS = int(os.getenv("DOCUMENT_AUTO_FULL_CHARS", "20000"))

# kind → (file suffix, label)
KINDS: dict[str, tuple[str, str]] = {
    "pdf": (".pdf", "PDF"),
    "docx": (".docx", "Word document"),
    "pptx": (".pptx", "PowerPoint presentation"),
    "xlsx": (".xlsx", "Excel workbook"),
    "xls": (".xls", "Excel 97-2003 workbook"),
    "epub": (".epub", "EPUB book"),
    "csv": (".csv", "CSV table"),
}
_EXT_KIND = {suffix: kind for kind, (suffix, _) in KINDS.items()}
_CTYPE_KIND = (
    ("application/pdf", "pdf"),
    ("wordprocessingml", "docx"),
    ("presentationml", "pptx"),
    ("spreadsheetml", "xlsx"),
    ("application/vnd.ms-excel", "xls"),
    ("application/epub", "epub"),
    ("text/csv", "csv"),
)
_DOC_LINK_RE = re.compile(
    r"""href\s*=\s*["']([^"'#]+?\.(?:pdf|docx|pptx|xlsx|xls|epub|csv))(?:[?#][^"']*)?["']""",
    re.IGNORECASE,
)


def cache_key_for(url: str) -> str:
    """The article-cache key for *url* (shared with ``fetch_fulltext(url=...)``)."""
    return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@dataclass
class Download:
    kind: str | None = None          # a KINDS key, "html", or None on failure
    path: Path | None = None         # local file for document kinds
    html: str | None = None          # page HTML when kind == "html"
    final_url: str = ""
    content_type: str = ""
    via: str = ""
    attempts: list[str] = field(default_factory=list)


def _url_kind(url: str) -> str | None:
    path = unquote(urlparse(url).path or "").lower()
    return _EXT_KIND.get(Path(path).suffix)


def _sniff(path: Path, content_type: str, url: str) -> str | None:
    """Decide what was downloaded from its bytes, then its headers and name."""
    try:
        with path.open("rb") as stream:
            head = stream.read(4096)
    except OSError:
        return None
    ctype = content_type.lower()
    hinted = next((kind for marker, kind in _CTYPE_KIND if marker in ctype), None) or _url_kind(url)
    if head.startswith(b"%PDF-") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile:
            return None
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if "mimetype" in names and hinted == "epub":
            return "epub"
        return hinted if hinted in ("docx", "pptx", "xlsx", "epub") else None
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2: legacy Excel converts; legacy .doc/.ppt are not supported.
        return "xls" if hinted == "xls" else "ole"
    text_head = head.lstrip().lower()
    if "html" in ctype or text_head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "html"
    if hinted == "csv" or "text/csv" in ctype:
        return "csv"
    if b"<html" in text_head[:2048] or b"<!doctype" in text_head[:512]:
        return "html"
    return None


def _http_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(**kwargs)


async def _download(url: str, *, proxy: str | None = None, via: str = "direct") -> Download:
    result = Download(final_url=url, via=via)
    fd, tmp_name = tempfile.mkstemp(dir=config.pdf_cache_dir, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        async with _http_client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            proxy=proxy,
            headers={"User-Agent": _UA, "Accept": _ACCEPT},
        ) as client:
            async with client.stream("GET", url) as resp:
                result.final_url = str(resp.url)
                result.content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                if resp.status_code != 200:
                    result.attempts.append(f"{via}: HTTP {resp.status_code}")
                    return result
                declared = int(resp.headers.get("content-length") or 0)
                if declared > _MAX_BYTES:
                    result.attempts.append(f"{via}: file is {declared / 1e6:.0f} MB, over the {_MAX_BYTES / 1e6:.0f} MB limit")
                    return result
                size = 0
                with open(fd, "wb", closefd=True) as out:
                    fd = -1
                    async for chunk in resp.aiter_bytes(65536):
                        size += len(chunk)
                        if size > _MAX_BYTES:
                            result.attempts.append(f"{via}: download exceeded the {_MAX_BYTES / 1e6:.0f} MB limit")
                            return result
                        out.write(chunk)
        kind = _sniff(tmp, result.content_type, result.final_url)
        if kind == "html":
            if size > _MAX_HTML_BYTES:
                result.attempts.append(f"{via}: HTML page too large ({size / 1e6:.0f} MB)")
                return result
            result.kind = "html"
            result.html = tmp.read_bytes().decode("utf-8", errors="replace")
            result.attempts.append(f"{via}: got an HTML page ({result.content_type or 'text/html'})")
            return result
        if kind == "ole":
            result.attempts.append(f"{via}: legacy Office binary (.doc/.ppt) — not convertible")
            return result
        if kind is None:
            result.attempts.append(f"{via}: unrecognised content ({result.content_type or 'no content-type'})")
            return result
        dest = config.pdf_cache_dir / f"{cache_key_for(url)[4:]}{KINDS[kind][0]}"
        tmp.replace(dest)
        result.kind, result.path = kind, dest
        result.attempts.append(f"{via}: {KINDS[kind][1]} ({size / 1024:.0f} KB)")
        return result
    except httpx.HTTPError as exc:
        result.attempts.append(f"{via}: network error ({type(exc).__name__})")
        return result
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def _looks_blocked(html: str) -> bool:
    """A bot-challenge or interstitial rather than real content."""
    low = html[:20000].lower()
    return any(marker in low for marker in (
        "cf-challenge", "challenge-platform", "just a moment...", "attention required",
        "captcha", "verify you are human", "enable javascript and cookies",
        "ddos-guard", "access denied",
    ))


async def acquire(url: str, use_proxy: bool = False) -> Download:
    """Fetch *url*: plain HTTP, then the institutional proxy, then Scrapling.

    Scrapling's stealth browser is used only to get past blocks; it returns
    PDFs as files, other formats are fetched over HTTP only.
    """
    config.evict_cache_lru()
    attempts: list[str] = []
    expected = _url_kind(url)

    got = await _download(url)
    attempts += got.attempts
    if got.kind and got.kind != "html":
        got.attempts = attempts
        return got
    page = got if got.kind == "html" and not _looks_blocked(got.html or "") else None

    if page is None and use_proxy and config.gost_proxy_url:
        proxied = await _download(url, proxy=config.gost_proxy_url, via="institutional proxy")
        attempts += proxied.attempts
        if proxied.kind and proxied.kind != "html":
            proxied.attempts = attempts
            return proxied
        if proxied.kind == "html" and not _looks_blocked(proxied.html or ""):
            page = proxied

    if page is None and config.use_stealth_browser and expected in (None, "pdf"):
        from .. import pdf_fetcher
        try:
            pdf_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(url, use_proxy=use_proxy)
        except Exception as exc:
            pdf_path, html, final_url = None, None, None
            attempts.append(f"stealth browser: failed ({type(exc).__name__})")
        if pdf_path:
            attempts.append("stealth browser: PDF")
            return Download(kind="pdf", path=Path(pdf_path), final_url=url, via="stealth browser",
                            content_type="application/pdf", attempts=attempts)
        if html and not _looks_blocked(html):
            attempts.append("stealth browser: got an HTML page")
            page = Download(kind="html", html=html, final_url=final_url or url, via="stealth browser")
        elif html:
            attempts.append("stealth browser: still blocked")
        else:
            attempts.append("stealth browser: no content")

    if page is not None:
        page.attempts = attempts
        return page
    return Download(final_url=got.final_url, content_type=got.content_type, attempts=attempts)


def document_links(html: str, base_url: str, limit: int = 15) -> list[str]:
    """Links on a page that point at downloadable documents."""
    seen: list[str] = []
    for match in _DOC_LINK_RE.finditer(html):
        link = urljoin(base_url, match.group(1).strip())
        if link.startswith(("http://", "https://")) and link not in seen:
            seen.append(link)
        if len(seen) >= limit:
            break
    return seen


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def markitdown_available() -> bool:
    try:
        import markitdown  # noqa: F401
    except ImportError:
        return False
    return True


def _markitdown_convert(path: str | None, html: str | None, url: str) -> tuple[str, str]:
    """Run markitdown on a file or an HTML string; returns ``(markdown, title)``."""
    import io

    from markitdown import MarkItDown, StreamInfo

    converter = MarkItDown(enable_plugins=False)
    if path is not None:
        result = converter.convert_local(path)
    else:
        stream = io.BytesIO((html or "").encode("utf-8"))
        result = converter.convert_stream(
            stream, stream_info=StreamInfo(mimetype="text/html", extension=".html", charset="utf-8", url=url)
        )
    return result.markdown or "", (result.title or "").strip()


async def _markitdown(path: Path | None = None, html: str | None = None, url: str = "") -> tuple[str, str]:
    return await asyncio.to_thread(_markitdown_convert, str(path) if path else None, html, url)


def _trafilatura_markdown(html: str, url: str) -> str:
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        return trafilatura.extract(
            html, url=url, output_format="markdown", include_tables=True,
            include_links=False, include_comments=False, favor_recall=True,
        ) or ""
    except Exception as exc:
        logger.debug("trafilatura failed on %s: %s", url, exc)
        return ""


def _markdown_sections(text: str) -> list[dict]:
    from ..pdf_extractor import _parse_markdown_sections

    return _parse_markdown_sections(text)


def _pptx_slide_sections(text: str) -> list[dict]:
    """Slides as sections: markitdown marks each with '<!-- Slide number: N -->'."""
    marks = list(re.finditer(r"<!--\s*Slide number:\s*(\d+)\s*-->", text))
    sections = []
    for i, mark in enumerate(marks):
        start = mark.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end]
        heading = re.search(r"^#{1,6}\s+(.+)$", body, re.MULTILINE)
        title = f"Slide {mark.group(1)}" + (f": {heading.group(1).strip()[:80]}" if heading else "")
        sections.append({"title": title, "level": 2, "start": start, "end": end,
                         "word_count": len(body.split())})
    return sections


@dataclass
class Converted:
    text: str
    sections: list
    section_detection: str
    title: str = ""
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    converter: str = ""


async def convert(download: Download) -> Converted:
    """Turn a downloaded document or page into navigable text."""
    if download.kind == "pdf":
        assert download.path is not None
        try:
            result = await pdf_work.extract(download.path, config.use_pymupdf4llm)
            text = result.get("text") or ""
            if text.strip():
                info = result.get("metadata") or {}
                meta: dict = {}
                if result.get("pages") or info.get("pages"):
                    meta["pages"] = result.get("pages") or info.get("pages")
                if info.get("author"):
                    meta["authors"] = [a.strip() for a in str(info["author"]).split(",") if a.strip()]
                warnings = []
                if result.get("ocr"):
                    meta["ocr"] = True
                    warnings.append(
                        f"ocr_text: no usable text layer; recognised by OCR from "
                        f"{result.get('ocr_pages')} of {result.get('pages')} pages — may contain errors"
                    )
                return Converted(
                    text=text, sections=result.get("sections") or [],
                    section_detection=result.get("section_detection", "pdf_font_analysis"),
                    title=str(info.get("title") or "").strip(), metadata=meta,
                    warnings=warnings, converter="pymupdf4llm" if config.use_pymupdf4llm else "pymupdf",
                )
        except Exception as exc:
            logger.info("PDF pipeline failed for %s, trying markitdown: %s", download.final_url, exc)
        if not markitdown_available():
            raise RuntimeError("the PDF has no extractable text")
        text, title = await _markitdown(download.path)
        return Converted(text=text, sections=_markdown_sections(text), section_detection="markdown_headings",
                         title=title, converter="markitdown")

    if download.kind == "html":
        # Main-content extraction first (drops navigation and cookie banners);
        # markitdown converts the whole page when that finds too little.
        html = download.html or ""
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html[:200000], re.IGNORECASE | re.DOTALL)
        page_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
        extracted = await content_extractor.extract_article_with_sections(html, download.final_url)
        if extracted and extracted.get("text"):
            return Converted(text=extracted["text"], sections=extracted.get("sections") or [],
                             section_detection=extracted.get("section_detection", "html_headings"),
                             title=page_title, converter="trafilatura")
        text = await asyncio.to_thread(_trafilatura_markdown, html, download.final_url)
        if len(text.split()) >= 50:
            return Converted(text=text, sections=_markdown_sections(text), section_detection="markdown_headings",
                             title=page_title, converter="trafilatura")
        title, converter = page_title, ""
        if markitdown_available():
            text, md_title = await _markitdown(html=html, url=download.final_url)
            title, converter = page_title or md_title, "markitdown"
        if not text.strip():
            raise RuntimeError("no readable text on the page")
        return Converted(text=text, sections=_markdown_sections(text), section_detection="markdown_headings",
                         title=title, converter=converter)

    if not markitdown_available():
        raise RuntimeError(
            f"converting a {KINDS[download.kind][1]} needs markitdown, which is not installed "
            "(install the 'documents' extra)"
        )
    assert download.path is not None
    text, title = await _markitdown(download.path)
    if download.kind == "pptx":
        sections, detection = _pptx_slide_sections(text), "slides"
    else:
        # Excel/CSV: markitdown writes one '## SheetName' heading per sheet.
        sections, detection = _markdown_sections(text), "markdown_headings"
    return Converted(text=text, sections=sections, section_detection=detection, title=title,
                     converter="markitdown")


def _fallback_title(text: str, url: str) -> str:
    heading = re.search(r"^#{1,3}\s+(.+)$", text[:5000], re.MULTILINE)
    if heading:
        return heading.group(1).strip()[:200]
    name = Path(unquote(urlparse(url).path or "")).name
    return name or urlparse(url).netloc


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def store(url: str, download: Download, converted: Converted) -> "text_cache.CachedArticle":
    """Cache the text under the URL key, marked as a general document."""
    meta = {
        **converted.metadata,
        "title": converted.title or _fallback_title(converted.text, download.final_url or url),
        "url": url,
        "final_url": download.final_url,
        "document_kind": download.kind,
        "content_type": download.content_type,
        "converter": converted.converter,
        "fetched_via": download.via,
        # Not a scholarly work: no expected title to verify against, and the
        # cached text must never be re-extracted or auto-imported as one.
        "identity": {"status": "not_checkable", "reason": "general document fetched by URL"},
        "content_warnings": converted.warnings,
        "full_extraction": True,
        "ocr_checked": True,
        "general_document": True,
    }
    if download.kind == "html":
        meta["document_links"] = document_links(download.html or "", download.final_url or url)
    sections, detection = converted.sections, converted.section_detection
    if len(sections) <= 1 and len(converted.text) > _AUTO_FULL_CHARS:
        # One heading over a long text (a big sheet, an unstructured report)
        # does not help navigation; let the keyword-chunk map take over.
        sections, detection = [], "keyword_skeleton"
    return text_cache.put_cached(
        cache_key_for(url), converted.text, f"{SOURCE_PREFIX}:{download.kind}",
        sections=sections, section_detection=detection,
        word_count=len(converted.text.split()), metadata=meta,
        pdf_path=str(download.path) if download.kind == "pdf" and download.path else None,
    )


def cached_document(url: str) -> "text_cache.CachedArticle | None":
    cached = text_cache.get_cached(cache_key_for(url))
    if cached and (cached.metadata or {}).get("general_document"):
        return cached
    return None


# ---------------------------------------------------------------------------
# Zotero (only on request)
# ---------------------------------------------------------------------------

async def save_to_zotero(cached: "text_cache.CachedArticle") -> str:
    """Create a Zotero 'document' item for the URL; attach the file when it is a PDF."""
    from .. import zotero, zotero_import, zotero_sqlite

    meta = cached.metadata or {}
    url = meta.get("url") or ""
    for candidate in dict.fromkeys(u for u in (url, meta.get("final_url")) if u):
        try:
            existing, _ = await zotero_sqlite.resolve_reference(candidate)
        except Exception:
            existing = None
        if existing is not None:
            return f"Zotero: already in the library as item {existing.key} ('{existing.title}'); nothing added."
    if not (zotero.zot_config.web_api_available or zotero.zot_config.local_enabled):
        return "Zotero: not saved — no writable Zotero API is configured."

    from datetime import UTC, datetime

    item: dict = {
        "itemType": "document",
        "title": (meta.get("title") or url)[:500],
        "url": url,
        "accessDate": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tags": [{"tag": "fetched-document"}],
        "creators": [
            {"creatorType": "author", "name": name.strip()}
            for name in (meta.get("authors") or [])[:10] if str(name).strip()
        ],
    }
    publisher = urlparse(meta.get("final_url") or url).netloc
    if publisher:
        item["publisher"] = publisher
    key, error, _ = await zotero_import._create_zotero_item({k: v for k, v in item.items() if v})
    if not key:
        return f"Zotero: not saved — {zotero_import._friendly_import_error(error or 'unknown error')}"
    if meta.get("document_kind") == "pdf" and cached.pdf_path and Path(cached.pdf_path).exists():
        filename = zotero_import._generate_pdf_filename(item)
        if await zotero_import._attach_pdf_to_item(key, Path(cached.pdf_path), filename):
            return f"Zotero: saved as document item {key} with the PDF attached."
        return (f"Zotero: saved as document item {key}, but attaching the PDF failed "
                f"({zotero_import._last_attachment_error or 'unknown error'}).")
    kind = meta.get("document_kind")
    if kind and kind != "html":
        return (f"Zotero: saved as document item {key} with its URL; the {KINDS[kind][1]} file "
                "itself was not attached (only PDFs are uploaded).")
    return f"Zotero: saved as document item {key} with its URL."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@dataclass
class DocumentResult:
    article: object | None = None   # FetchedArticle
    cached: "text_cache.CachedArticle | None" = None
    from_cache: bool = False
    attempts: list[str] = field(default_factory=list)
    zotero: str = ""
    error: str = ""


async def fetch_document(
    url: str,
    mode: str = "auto",
    section: str | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
    use_proxy: bool = False,
    refresh: bool = False,
    save: bool = False,
) -> DocumentResult:
    from .fetch import _apply_mode_filter

    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return DocumentResult(error="fetch_document needs an http(s) URL.")
    use_proxy = use_proxy or (config.institutional_proxy_auto and bool(config.gost_proxy_url))

    out = DocumentResult()
    cached = None if refresh else cached_document(url)
    if cached is not None:
        out.from_cache = True
    else:
        download = await acquire(url, use_proxy=use_proxy)
        out.attempts = download.attempts
        if not download.kind:
            out.error = f"Could not retrieve a readable document from {url}."
            return out
        try:
            converted = await convert(download)
        except Exception as exc:
            out.error = f"Retrieved {url} but could not convert it: {exc}"
            return out
        cached = store(url, download, converted)
    out.cached = cached

    if mode == "auto":
        mode = "full" if len(cached.text) <= _AUTO_FULL_CHARS else "sections"
    out.article = _apply_mode_filter(cached, mode, section, range_start, range_end)
    if save:
        try:
            out.zotero = await save_to_zotero(cached)
        except Exception as exc:
            logger.warning("Saving %s to Zotero failed: %s", url, exc)
            out.zotero = f"Zotero: not saved — {exc}"
    return out
