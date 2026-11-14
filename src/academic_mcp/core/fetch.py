"""Business logic for fetch_fulltext (PDF / HTML article retrieval).

Contains:
- Per-DOI async locking primitives
- Formatting helpers (_apply_mode_filter, etc.)
- fetch_article(args) — the full retrieval pipeline
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
from pathlib import Path

import httpx

from .types import ArticleId, FetchedArticle, FetchMode, PreviewChunk, Section

from .. import (
    apis,
    content_extractor,
    core_api,
    identity,
    pdf_extractor,
    pdf_work,
    pdf_fetcher,
    text_cache,
    web_search,
    zotero,
    zotero_import,
    zotero_sqlite,
)
from ..config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-DOI async lock for concurrent fetch protection
# ---------------------------------------------------------------------------

# Per-DOI locks to prevent concurrent fetches for the same paper.
# The lock ensures that if two calls request the same uncached DOI
# simultaneously, the second waits for the first to finish fetching
# and then reads from cache.
_doi_locks: dict[str, asyncio.Lock] = {}
_doi_locks_lock = asyncio.Lock()  # protects the dict itself


async def _get_doi_lock(doi: str) -> asyncio.Lock:
    """Get or create an async lock for a specific DOI."""
    async with _doi_locks_lock:
        if doi not in _doi_locks:
            _doi_locks[doi] = asyncio.Lock()
        return _doi_locks[doi]


# ---------------------------------------------------------------------------
# Identity gate
# ---------------------------------------------------------------------------
#
# Each fetch carries the bibliographic record it is trying to satisfy.  Every
# tier hands its text to the gate before caching: text that is a different
# work is rejected (and the next tier tried), never returned or cached under
# the requested identifier, and never written to Zotero.

class _FetchContext:
    def __init__(self) -> None:
        self.expected: dict = {}
        self.rejections: list[dict] = []
        # Zotero attachments created by this server's auto-import are subject
        # to the same rejection rule as web sources.
        self.zotero_untrusted = False


_ctx: contextvars.ContextVar[_FetchContext | None] = contextvars.ContextVar(
    "academic_fetch_ctx", default=None
)


def _current_ctx() -> _FetchContext:
    ctx = _ctx.get()
    if ctx is None:
        ctx = _FetchContext()
        _ctx.set(ctx)
    return ctx


def _set_expected(meta: dict | None) -> None:
    """Record the requested work's title/authors (first non-empty wins per field)."""
    if not meta:
        return
    ctx = _current_ctx()
    for key in ("title", "authors", "year", "venue"):
        value = meta.get(key)
        if value and not ctx.expected.get(key):
            ctx.expected[key] = value
    # Keep every title the record is known by (library vs published title).
    titles = ctx.expected.setdefault("titles", [])
    for title in [meta.get("title"), *(meta.get("titles") or [])]:
        if title and title not in titles:
            titles.append(title)


def _zotero_expected(zot_result: dict) -> dict:
    meta = zot_result.get("metadata") or {}
    creators = meta.get("creators") or []
    return {
        "title": meta.get("title") or "",
        "authors": [c.get("lastName") or c.get("name") or c.get("firstName") or ""
                    for c in creators if isinstance(c, dict)],
        "year": str(meta.get("date") or "")[:4],
        "venue": meta.get("publicationTitle") or "",
    }


def _is_zotero_source(source: str) -> bool:
    return (source or "").lower().startswith(("zotero_", "sqlite_"))


_SEARCH_DERIVED_SOURCES = ("web_search", "core.ac.uk", "core_source_url", "heinonline")


_HTML_SOURCES = ("html_extraction", "desktop_browser_html", "ssrn_cookies_html")


def _is_html_source(source: str) -> bool:
    return (source or "").lower().startswith(_HTML_SOURCES)


def _soften_for_library(check, source: str, untrusted: bool):
    """The user's own attachment is never rejected; flag it gently instead.

    Item titles in a personal library are often informal ('DFA proposal'),
    and encyclopedia or proceedings PDFs hold many works, so a missing title
    is a prompt to check, not proof of a wrong file.
    """
    if check.rejected and _is_zotero_source(source) and not untrusted:
        return identity.IdentityCheck(
            identity.UNCERTAIN,
            "the attachment's opening text does not show the library item's title; "
            "check the attachment if this is not the work",
            check.expected_title, check.found_title,
        )
    return check


def _gate(
    text: str, source: str, *, pdf_title: str = "", pages: int | None = None,
    page_title: str = "", page_doi: str = "", requested_doi: str = "",
):
    """Check identity and quality. Returns (check, warnings, rejected)."""
    ctx = _current_ctx()
    if (
        page_doi and requested_doi and _is_real_doi(requested_doi)
        and zotero._normalize_doi(page_doi) == zotero._normalize_doi(requested_doi)
    ):
        check = identity.IdentityCheck(
            identity.PASS, "publisher page declares this DOI",
            str(ctx.expected.get("title") or ""), page_title[:200],
        )
    else:
        check = identity.check_identity_any(
            text, ctx.expected.get("titles") or [ctx.expected.get("title") or ""],
            ctx.expected.get("authors"),
            pdf_title=pdf_title, page_title=page_title, html=_is_html_source(source),
        )
    warnings = identity.assess_content(text, pages=pages)
    rejected = check.rejected and (not _is_zotero_source(source) or ctx.zotero_untrusted)
    if not rejected:
        check = _soften_for_library(check, source, ctx.zotero_untrusted)
    # Search-derived hits (title queries against search engines, CORE,
    # HeinOnline) are only candidates: they must positively match.
    if (
        not rejected
        and check.status != identity.PASS
        and (source or "").lower().startswith(_SEARCH_DERIVED_SOURCES)
        and ctx.expected.get("title")
    ):
        rejected = True
        check = identity.IdentityCheck(
            identity.MISMATCH, f"search result not confirmed as the requested work ({check.reason})",
            check.expected_title, check.found_title,
        )
    if check.rejected:
        logger.warning(
            "Identity %s from %s: expected %r, found %r (%s)%s",
            check.status, source, check.expected_title[:80], check.found_title[:80],
            check.reason, " — rejected" if rejected else " — kept (user's Zotero copy)",
        )
    if rejected:
        ctx.rejections.append({
            "source": source,
            "found_title": check.found_title,
            "reason": check.reason,
        })
    return check, warnings, rejected


def _provenance_metadata(base: dict | None, check, warnings: list[str]) -> dict:
    """Citation metadata for a cache entry: the requested record, not the PDF's own fields."""
    ctx = _current_ctx()
    meta = dict(base or {})
    if not check.rejected:
        for key, value in ctx.expected.items():
            if value and key != "titles":
                meta[key] = value
    meta["identity"] = check.to_dict()
    meta["content_warnings"] = list(warnings)
    return meta


def _is_real_doi(doi: str | None) -> bool:
    return bool(doi) and not doi.startswith(("zotero:", "url:")) and doi.startswith("10.")


async def _expected_for(doi: str | None, zotero_key: str | None = None) -> dict:
    """Bibliographic record for an identifier: Zotero first, then Crossref, then S2."""
    try:
        item = None
        if zotero_key and zotero_sqlite.sqlite_config.available:
            item = await zotero_sqlite.search_by_key(zotero_key)
        elif _is_real_doi(doi) and zotero_sqlite.sqlite_config.available:
            item = await zotero_sqlite.search_by_doi(doi)
        if item and item.title:
            return {
                "title": item.title,
                "authors": [c.lastName or c.firstName for c in item.creators],
                "year": (item.date or "")[:4],
            }
    except Exception as exc:
        logger.debug("Zotero metadata lookup failed for %s: %s", doi or zotero_key, exc)
    if not _is_real_doi(doi):
        return {}
    try:
        work = await apis.crossref_work(doi)
        if work and work.get("title"):
            title = work["title"][0] if isinstance(work["title"], list) else work["title"]
            subtitle = (work.get("subtitle") or [""])[0] if isinstance(work.get("subtitle"), list) else ""
            if subtitle and subtitle.lower() not in title.lower():
                title = f"{title}: {subtitle}"
            return {
                "title": title,
                "authors": [a.get("family") or a.get("name") or "" for a in work.get("author") or []],
            }
    except Exception as exc:
        logger.debug("Crossref metadata lookup failed for %s: %s", doi, exc)
    try:
        paper = await apis.s2_paper(f"DOI:{doi}")
        if paper and paper.get("title"):
            return {
                "title": paper["title"],
                "authors": [a.get("name", "") for a in paper.get("authors") or []],
            }
    except Exception as exc:
        logger.debug("S2 metadata lookup failed for %s: %s", doi, exc)
    return {}


async def _is_auto_imported(doi: str | None, zotero_key: str | None) -> bool:
    """Whether the library item behind *doi*/*zotero_key* was created by auto-import."""
    try:
        item = None
        if zotero_key:
            item = (await zotero_sqlite.search_by_keys([zotero_key])).get(zotero_key)
        elif _is_real_doi(doi):
            item = await zotero_sqlite.search_by_doi(doi)
            if item is None:
                from .. import zotero_catalog
                keys = zotero_catalog.find_keys(dois=[doi])
                if keys:
                    item = (await zotero_sqlite.search_by_keys(keys[:1])).get(keys[0])
        return bool(item and "auto-imported" in (item.tags or []))
    except Exception as exc:
        logger.debug("Auto-import tag lookup failed for %s: %s", doi or zotero_key, exc)
        return False


async def _published_titles(doi: str) -> list[str]:
    try:
        work = await apis.crossref_work(doi)
    except Exception:
        return []
    titles = list((work or {}).get("title") or [])
    subtitle = ((work or {}).get("subtitle") or [""])[0]
    if titles and subtitle:
        titles.append(f"{titles[0]}: {subtitle}")
    return [t for t in titles if t]


_TRUNCATION_MARKER = "[... TRUNCATED — full text exceeds context limit ...]"


async def _revalidate_cached(
    cached: "text_cache.CachedArticle", doi: str, zotero_key: str | None,
) -> "text_cache.CachedArticle | None":
    """Verify a cache entry written before identity checks existed.

    Returns the (annotated) entry, or None when it holds a different work, in
    which case it is deleted so the normal pipeline fetches the right one.
    Zotero-sourced entries are annotated but kept: the attachment is the
    user's own file and the response shows the warning.
    """
    meta = cached.metadata if isinstance(cached.metadata, dict) else {}
    verdict = meta.get("identity")
    if isinstance(verdict, dict) and not (
        # Library copies given the benefit of the doubt are rechecked in case
        # the attachment turns out to be one this server auto-imported.
        _is_zotero_source(cached.source)
        and str(verdict.get("reason", "")).startswith("the attachment's opening text")
        and not meta.get("auto_import_checked")
    ):
        return cached
    expected = await _expected_for(doi, zotero_key)
    if not expected.get("title"):
        return cached
    kwargs = {"pdf_title": str(meta.get("pdf_title") or ""), "html": _is_html_source(cached.source)}
    check = identity.check_identity(cached.text, expected["title"], expected.get("authors"), **kwargs)
    if check.status != identity.PASS and _is_real_doi(doi):
        # The library may hold a preprint title for a published DOI.
        published = await _published_titles(doi)
        if published:
            check = identity.check_identity_any(
                cached.text, [expected["title"], *published], expected.get("authors"), **kwargs)
    auto_imported = False
    if check.rejected and _is_zotero_source(cached.source):
        auto_imported = await _is_auto_imported(doi, zotero_key)
    check = _soften_for_library(check, cached.source, auto_imported)
    if check.rejected and (auto_imported or not _is_zotero_source(cached.source)):
        logger.warning(
            "Purging cached %s for %s: expected %r, found %r (%s)",
            cached.source, doi, check.expected_title[:80], check.found_title[:80], check.reason,
        )
        text_cache.delete_cached(doi)
        if cached.pdf_path:
            _discard_rejected_pdf(Path(cached.pdf_path))
        _current_ctx().rejections.append({
            "source": f"{cached.source} (cached)",
            "found_title": check.found_title,
            "reason": check.reason,
        })
        return None
    meta = dict(meta)
    if not check.rejected:
        for key, value in expected.items():
            if value and key in ("title", "authors", "year"):
                meta[key] = value
    meta["identity"] = check.to_dict()
    meta["content_warnings"] = identity.assess_content(cached.text, pages=meta.get("pages"))
    if _is_zotero_source(cached.source):
        meta["auto_import_checked"] = True
    return text_cache.put_cached(
        cached.doi, cached.text, cached.source,
        sections=cached.sections,
        section_detection=cached.section_detection,
        word_count=cached.word_count,
        metadata=meta,
        pdf_path=cached.pdf_path,
        html_path=cached.html_path,
    )


def _discard_rejected_pdf(pdf_source) -> None:
    """Remove a downloaded wrong document so URL-keyed PDF caching cannot revive it."""
    if not isinstance(pdf_source, Path):
        return
    try:
        if pdf_source.resolve().parent == config.pdf_cache_dir.resolve():
            pdf_source.unlink(missing_ok=True)
    except OSError:
        pass


def _accept_text(
    doi: str,
    raw_text: str,
    source: str,
    *,
    sections: list,
    section_detection: str,
    word_count: int | None = None,
    metadata: dict | None = None,
    pdf_path: str | None = None,
    page_title: str = "",
    page_doi: str = "",
) -> "text_cache.CachedArticle | None":
    """Gate extracted (non-PDF) text and cache it, or return None when rejected."""
    check, warnings, rejected = _gate(
        raw_text, source, page_title=page_title, page_doi=page_doi, requested_doi=doi,
    )
    if rejected:
        return None
    return text_cache.put_cached(
        doi, raw_text, source,
        sections=sections,
        section_detection=section_detection,
        word_count=word_count or len(raw_text.split()),
        metadata=_provenance_metadata(metadata, check, warnings),
        pdf_path=pdf_path,
    )


# ---------------------------------------------------------------------------
# Landing-page extraction helper
# ---------------------------------------------------------------------------

async def _extract_from_landing_page(
    url: str,
    use_proxy: bool,
    expected_doi: str | None = None,
) -> dict | None:
    """Fetch a landing-page URL via stealth browser and extract content.

    Shared by both the DOI landing-page tier (pass *expected_doi* for the
    publisher-redirect mismatch guard) and the URL-driven tier (no expected_doi).

    Returns a dict with keys ``text``, ``source``, ``pdf_path``,
    ``sections``, ``section_detection``, ``word_count`` on success,
    or ``None`` on failure.  Exactly one of *text* or *pdf_path* is non-None.
    """
    if not config.use_stealth_browser:
        return None

    scrapling_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(url, use_proxy=use_proxy)
    effective_url = final_url or url

    if scrapling_path:
        return {
            "text": None, "source": "scrapling_direct_pdf",
            "pdf_path": scrapling_path,
            "sections": None, "section_detection": None, "word_count": None,
        }

    if not html:
        return None

    meta = content_extractor.extract_citation_meta(html, effective_url)

    # DOI mismatch guard: some publisher resolvers redirect to a different
    # article on lookup failure.  Only applied when caller supplies expected_doi.
    if expected_doi:
        citation_doi = meta.get("citation_doi", "")
        if citation_doi and zotero._normalize_doi(citation_doi) != zotero._normalize_doi(expected_doi):
            logger.warning(
                "DOI mismatch: requested %s, page reports %s — discarding HTML",
                expected_doi, citation_doi,
            )
            return None

    citation_pdf = meta.get("citation_pdf_url", "")
    if citation_pdf:
        logger.info("Found citation_pdf_url: %s", citation_pdf)
        path = await pdf_fetcher.fetch_direct(citation_pdf)
        if not path and use_proxy:
            path = await pdf_fetcher.fetch_proxied(citation_pdf)
        if path:
            return {
                "text": None, "source": "citation_pdf_url (direct)",
                "pdf_path": path,
                "sections": None, "section_detection": None, "word_count": None,
            }

    # Trafilatura HTML extraction.
    extraction = await content_extractor.extract_article_with_sections(html, effective_url)
    if extraction:
        raw_text = extraction["text"]
        sections = extraction["sections"] or content_extractor.detect_sections_from_text(raw_text)
        section_det = extraction["section_detection"] if extraction["sections"] else "text_heuristic"
        return {
            "text": raw_text,
            "source": f"html_extraction ({extraction['source']})",
            "pdf_path": None,
            "sections": sections,
            "section_detection": section_det,
            "word_count": extraction["word_count"],
            "page_title": meta.get("citation_title", ""),
            "page_doi": meta.get("citation_doi", ""),
        }

    # Last-resort PDF link scan from HTML.
    pdf_link = pdf_fetcher._extract_pdf_link_from_html(html, effective_url)
    if pdf_link:
        logger.info("Trying PDF link found in HTML: %s", pdf_link)
        path = await pdf_fetcher.fetch_direct(pdf_link)
        if not path and use_proxy:
            path = await pdf_fetcher.fetch_proxied(pdf_link)
        if path:
            return {
                "text": None, "source": "html_pdf_link",
                "pdf_path": path,
                "sections": None, "section_detection": None, "word_count": None,
            }

    return None


# ---------------------------------------------------------------------------
# Section / mode helpers
# ---------------------------------------------------------------------------

def _fuzzy_match_section(query: str, sections: list[dict]) -> dict | None:
    """Return the best section match for *query* against section titles.

    Tries exact match, then substring (query in title), then reverse
    substring (title in query), to handle queries like "methods" matching
    "Materials and Methods".
    """
    q = query.lower().strip()
    for sec in sections:
        if sec["title"].lower().strip() == q:
            return sec
    for sec in sections:
        if q in sec["title"].lower():
            return sec
    for sec in sections:
        if sec["title"].lower() in q:
            return sec
    return None


async def _backfill_zotero_pdf_path(
    doi: str | None, zotero_key: str | None,
) -> str | None:
    """Try to find the local Zotero PDF path for an article without one cached.

    Tries both lookup paths in order: by zotero_key (most reliable), then
    by doi (fallback when only the DOI is known or short-DOI resolution
    drifted from the SQLite-stored DOI).  Returns the first path that
    points to an existing file.  Best-effort — never raises.
    """
    candidates: list[tuple[str, str]] = []
    if zotero_key:
        candidates.append(("by_key", zotero_key))
    if doi and not doi.startswith(("zotero:", "url:")):
        candidates.append(("by_doi", doi))
    for kind, value in candidates:
        try:
            if kind == "by_key":
                zr = await zotero.get_paper_from_zotero_by_key(value)
            else:
                zr = await zotero.get_paper_from_zotero(value)
            if not zr:
                continue
            pdf = zr.get("pdf_path")
            if pdf and Path(str(pdf)).exists():
                return str(pdf)
        except Exception as exc:
            logger.debug("PDF backfill via %s=%r failed: %s", kind, value, exc)
    return None


def _cached_article_result(
    cached: "text_cache.CachedArticle",
    text: str,
    *,
    error: str | None = None,
    truncated: bool = False,
    pdf_path: str | None = None,
    html_path: str | None = None,
) -> FetchedArticle:
    # Cached fields may be missing on test mocks; coerce to str|None defensively.
    _c_pdf = getattr(cached, "pdf_path", None)
    _c_html = getattr(cached, "html_path", None)
    _meta = cached.metadata if isinstance(getattr(cached, "metadata", None), dict) else {}
    _identity = _meta.get("identity") if isinstance(_meta.get("identity"), dict) else None
    _warnings = [w for w in (_meta.get("content_warnings") or []) if isinstance(w, str)]
    return FetchedArticle(
        identity=_identity,
        content_warnings=_warnings,
        doi=str(cached.doi or ""),
        text=text,
        source=str(cached.source or ""),
        sections=list(cached.sections or []),
        section_detection=str(cached.section_detection or "unknown"),
        word_count=int(cached.word_count or 0),
        metadata=dict(cached.metadata or {}),
        error=error,
        truncated=truncated,
        pdf_path=pdf_path or (_c_pdf if isinstance(_c_pdf, str) else None),
        html_path=html_path or (_c_html if isinstance(_c_html, str) else None),
        cache_key=text_cache._cache_key(str(cached.doi or "")),
    )


def _apply_mode_filter(
    cached: "text_cache.CachedArticle",
    mode: str,
    section_name: str | None,
    range_start: int | None,
    range_end: int | None,
) -> FetchedArticle:
    """Apply mode filter: populate structured fields, set fa.text to raw slice."""
    doi = cached.doi
    # HTML full text is a successful retrieval too. Save its citation even
    # when the publisher exposes no downloadable PDF; a later PDF fetch can
    # attach to this same record.
    if cached.source.startswith("html_extraction"):
        zotero_import.enqueue_zotero_import(doi, None, cached)

    if mode == "sections":
        structured_sections: list[Section] = []

        if not cached.sections:
            # Generate keyword skeleton as a last-resort navigation fallback
            skeleton = content_extractor.generate_keyword_skeleton(cached.text)
            if skeleton:
                cached = text_cache.put_cached(
                    cached.doi, cached.text, cached.source,
                    sections=skeleton,
                    section_detection="keyword_skeleton",
                    word_count=cached.word_count,
                    metadata=cached.metadata,
                    pdf_path=cached.pdf_path,
                    html_path=cached.html_path,
                )
                total = len(skeleton)
                for chunk in skeleton:
                    wc = chunk.get("word_count", 0)
                    start = chunk.get("start", 0)
                    end = chunk.get("end", 0)
                    n = chunk.get("chunk", 0)
                    structured_sections.append(Section(
                        title=f"[{n}/{total}]",
                        char_start=start,
                        char_end=end,
                        level=2,
                        keywords=chunk.get("keywords", []),
                        word_count=wc,
                        is_infill=True,
                    ))

        else:
            # Per-section TF-IDF keywords (structural sections only — infill gets
            # its own local keywords computed inside infill_keyword_chunks).
            sec_keywords = content_extractor.keywords_for_sections(
                cached.text, cached.sections
            )

            # Gap infill: insert keyword chunks into large uncovered spans.
            display_sections = content_extractor.infill_keyword_chunks(
                cached.text, cached.sections
            )

            # Build an index from section start offset → keyword list so we can
            # look up keywords quickly while iterating display_sections.
            kw_by_start: dict[int, list[str]] = {
                sec.get("start", 0): kw
                for sec, kw in zip(cached.sections, sec_keywords)
            }

            structural_idx = 0  # counter for non-infill sections only
            for entry in display_sections:
                wc = entry.get("word_count", 0)
                start = entry.get("start", 0)
                end = entry.get("end", 0)

                if entry.get("_infill"):
                    structured_sections.append(Section(
                        title=entry["title"],
                        char_start=start,
                        char_end=end,
                        level=3,
                        keywords=entry.get("keywords", []),
                        word_count=wc,
                        is_infill=True,
                    ))
                else:
                    kw_list = kw_by_start.get(start, [])
                    structured_sections.append(Section(
                        title=entry["title"],
                        char_start=start,
                        char_end=end,
                        level=entry.get("level", 2),
                        keywords=kw_list,
                        word_count=wc,
                        is_infill=False,
                    ))
                    structural_idx += 1

        fa = _cached_article_result(cached, "")
        fa.mode = FetchMode.sections
        fa.available_sections = structured_sections
        fa.text = ""
        return fa

    if mode == "section":
        if not section_name:
            _msg = "mode='section' requires the 'section' parameter with a heading name."
            fa = _cached_article_result(cached, _msg, error=_msg)
            fa.mode = FetchMode.section
            return fa
        if not cached.sections:
            _msg = (
                f"No sections detected for DOI: {doi}. "
                "Use mode='full' to read the entire text."
            )
            fa = _cached_article_result(cached, _msg, error=_msg)
            fa.mode = FetchMode.section
            return fa
        match = _fuzzy_match_section(section_name, cached.sections)
        if not match:
            available = "\n".join(
                f"  [{i}] {s['title']}" for i, s in enumerate(cached.sections[:20])
            )
            _msg = (
                f"Section '{section_name}' not found in DOI: {doi}\n\n"
                f"Available sections:\n{available}"
            )
            fa = _cached_article_result(cached, _msg, error=_msg)
            fa.mode = FetchMode.section
            fa.available_sections = [
                Section(
                    title=s["title"],
                    char_start=s.get("start", 0),
                    char_end=s.get("end", 0),
                    level=s.get("level", 2),
                    word_count=s.get("word_count", 0),
                )
                for s in cached.sections[:20]
            ]
            return fa
        end = match.get("end") or len(cached.text)
        section_text = cached.text[match["start"]:end]
        if len(section_text) > config.max_context_length:
            section_text = section_text[:config.max_context_length]
        fa = _cached_article_result(cached, section_text)
        fa.mode = FetchMode.section
        fa.matched_section = Section(
            title=match["title"],
            char_start=match["start"],
            char_end=end,
            level=match.get("level", 2),
            word_count=match.get("word_count", 0),
        )
        return fa

    if mode == "preview":
        preview_chunks: list[PreviewChunk] = []
        if not cached.sections:
            preview_chunks.append(PreviewChunk(
                section_title=None,
                text=cached.text[:2000],
                word_count_total=len(cached.text.split()),
                word_count_shown=len(cached.text[:2000].split()),
            ))
        else:
            abstract_sec = next(
                (s for s in cached.sections if "abstract" in s["title"].lower()), None
            )
            if abstract_sec:
                end = abstract_sec.get("end") or len(cached.text)
                abs_text = cached.text[abstract_sec["start"]:end]
                preview_chunks.append(PreviewChunk(
                    section_title=abstract_sec["title"],
                    text=abs_text,
                    word_count_total=len(abs_text.split()),
                    word_count_shown=len(abs_text.split()),
                ))
            else:
                first_start = cached.sections[0]["start"] if cached.sections else len(cached.text)
                if first_start > 0:
                    preamble = cached.text[:first_start]
                    preview_chunks.append(PreviewChunk(
                        section_title=None,
                        text=preamble,
                        word_count_total=len(preamble.split()),
                        word_count_shown=len(preamble.split()),
                    ))
            for sec in cached.sections:
                if sec == abstract_sec:
                    continue
                end = sec.get("end") or len(cached.text)
                section_text = cached.text[sec["start"]:end]
                words = section_text.split()
                preview_chunks.append(PreviewChunk(
                    section_title=sec["title"],
                    text=" ".join(words[:200]),
                    word_count_total=len(words),
                    word_count_shown=min(200, len(words)),
                ))
        fa = _cached_article_result(cached, "")
        fa.mode = FetchMode.preview
        fa.preview_chunks = preview_chunks
        fa.text = ""
        return fa

    if mode == "range":
        total = len(cached.text)
        start = max(0, range_start or 0)
        end = range_end or min(start + config.max_context_length, total)
        end = min(end, start + config.max_context_length)
        if start >= total or end <= start:
            _msg = (
                f"range_out_of_bounds: the cached text for {doi} has {total:,} characters "
                f"(source: {cached.source}); requested [{start:,}:{end:,}]. "
                "Offsets from a different source or an earlier extraction do not apply; "
                "use mode='sections' or search_in_article to get current offsets."
            )
            fa = _cached_article_result(cached, _msg, error=_msg)
            fa.mode = FetchMode.range
            return fa
        snippet = cached.text[start:end]
        fa = _cached_article_result(cached, snippet)
        fa.truncated = end < total
        fa.mode = FetchMode.range
        fa.range_chars = (start, end)
        fa.text = snippet
        return fa

    # mode == "full" (default)
    body_text = cached.text
    truncated = False
    header_len = len(
        f"Full text (cached) for DOI: {doi}\n"
        f"Source: {cached.source}\n"
        + "=" * 60 + "\n\n"
    )
    if header_len + len(body_text) > config.max_context_length:
        body_text = body_text[:config.max_context_length - header_len]
        truncated = True
    fa = _cached_article_result(cached, body_text, truncated=truncated)
    fa.mode = FetchMode.full
    fa.text = body_text
    return fa


# ---------------------------------------------------------------------------
# PDF extraction helpers
# ---------------------------------------------------------------------------

async def _cache_pdf_and_return(
    pdf_source: "Path | bytes",
    doi: str,
    source: str,
    pages_str: str | None,
    mode: str,
    section_name: str | None,
    range_start: int | None,
    range_end: int | None,
) -> FetchedArticle | None:
    """Extract PDF text, write to article cache, apply mode filter, and return.

    When *pages_str* is set we return a partial extraction and skip caching
    (partial text is not useful for section-based access).

    Returns None when the PDF is a different work from the one requested
    (see :func:`_gate`); callers then try their next source.
    """
    def _append_import_hint(fa: FetchedArticle) -> FetchedArticle:
        hint = zotero_import.get_auto_import_hint(doi)
        if not hint:
            return fa
        fa.auto_import_status = hint
        return fa

    if pages_str:
        parts = pages_str.split("-")
        start_page = int(parts[0])
        end_page = int(parts[1]) if len(parts) > 1 else start_page
        extracted_text = (await pdf_work.extract(pdf_source, config.use_pymupdf4llm, pages_str))["text"]
        return FetchedArticle(
            doi=doi,
            text=extracted_text,
            source=f"pages {pages_str} ({source})",
            mode=FetchMode.full,
        )

    result = await pdf_work.extract(pdf_source, config.use_pymupdf4llm)
    raw_text = result["text"]

    # Build metadata from PDF extraction result
    pdf_meta: dict = {}
    _pdf_info = result.get("metadata") or {}
    if _pdf_info:
        pdf_meta["title"] = _pdf_info.get("title", "")
        pdf_meta["pdf_title"] = _pdf_info.get("title", "")
        if _pdf_info.get("author"):
            pdf_meta["authors"] = _pdf_info["author"].split(",") if isinstance(_pdf_info["author"], str) else _pdf_info["author"]
    _pages = result.get("pages") or _pdf_info.get("pages")
    if _pages:
        pdf_meta["pages"] = _pages

    check, warnings, rejected = _gate(
        raw_text, source, pdf_title=str(_pdf_info.get("title") or ""),
        pages=_pages if isinstance(_pages, int) else None,
    )
    if rejected:
        if not _is_zotero_source(source):
            _discard_rejected_pdf(pdf_source)
        return None

    cached_article = text_cache.put_cached(
        doi, raw_text, source,
        sections=result["sections"],
        section_detection=result.get("section_detection", "pdf_font_analysis"),
        word_count=len(raw_text.split()),
        metadata=_provenance_metadata(pdf_meta, check, warnings),
        pdf_path=str(pdf_source) if isinstance(pdf_source, Path) else None,
    )

    # Build and persist the charmap for PDF highlight positioning.
    # Only built for file-backed PDFs; bytes sources (tests, ad-hoc) are skipped.
    # Always rebuilt in sync with .article.json to keep them consistent.
    if isinstance(pdf_source, Path) and not config.use_pymupdf4llm:
        ck = text_cache._cache_key(doi)
        cm_path = text_cache.charmap_path(ck)
        try:
            charmap_bytes = result.get("_charmap")
            if charmap_bytes is None:
                raise ValueError("charmap unavailable")
            cm_path.write_bytes(charmap_bytes)
        except Exception as exc:
            logger.debug("Charmap build failed for %s: %s", doi, exc)

    # Queue for background Zotero import (non-blocking; only when we have a file)
    if isinstance(pdf_source, Path):
        zotero_import.enqueue_zotero_import(doi, pdf_source, cached_article)
        hint = zotero_import.get_auto_import_hint(doi)
        if hint:
            logger.warning("Auto-import warning for %s: %s", doi, hint)

    _pdf_path = str(pdf_source) if isinstance(pdf_source, Path) else None
    fa = _apply_mode_filter(cached_article, mode, section_name, range_start, range_end)
    fa.pdf_path = fa.pdf_path or _pdf_path
    fa.cache_key = fa.cache_key or text_cache._cache_key(doi)
    return _append_import_hint(fa)


# ---------------------------------------------------------------------------
# Short DOI resolution
# ---------------------------------------------------------------------------

def _is_short_doi(doi: str) -> bool:
    """Return True for short DOIs like '10/gm3p2t' (no period after '10')."""
    s = doi.strip().lower()
    return s.startswith("10/") and "." not in s.split("/")[0]


async def _resolve_short_doi(doi: str) -> str:
    """Resolve a short DOI to its canonical form via doi.org content negotiation.

    Returns the canonical DOI string, or the original if resolution fails.
    """
    url = f"https://doi.org/{doi}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8) as client:
            r = await client.get(url, headers={"Accept": "application/vnd.citationstyles.csl+json"})
        if r.status_code == 200:
            data = r.json()
            canonical = data.get("DOI", "")
            if canonical:
                logger.info("Resolved short DOI %s → %s", doi, canonical)
                return canonical.lower().strip()
    except Exception as exc:
        logger.debug("Short DOI resolution failed for %s: %s", doi, exc)
    return doi


# ---------------------------------------------------------------------------
# Main fetch pipeline
# ---------------------------------------------------------------------------

async def _fetch_article(
    identifier: "ArticleId",
    mode: str = "sections",
    section: str | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
    use_proxy: bool = False,
    pages: str | None = None,
    source: str = "auto",
    _original_ssrn_doi: str | None = None,
) -> FetchedArticle:
    """Full article retrieval pipeline.

    Tries (in order): article cache, Zotero, URL tier, SSRN remapping,
    external APIs (S2/OA/Unpaywall), direct HTTP, CORE.ac.uk, web search,
    DOI landing page, proxied fetch, stealth browser, HeinOnline, SSRN cookies.
    """
    from .types import ArticleId as _ArticleId  # local import avoids circular at module level
    use_proxy = use_proxy or (config.institutional_proxy_auto and bool(config.gost_proxy_url))
    zotero_key = (identifier.zotero_key or "").strip() or None
    doi = identifier.doi
    url = (identifier.url or "").strip() or None
    if not doi and not zotero_key and not url:
        _msg = "fetch_fulltext requires at least one of 'doi', 'zotero_key', or 'url'."
        return FetchedArticle(doi="", text=_msg, error=_msg)
    # Library-first resolution: item keys, zotero:// links, Better BibTeX
    # citekeys, and URLs of items already in Zotero all open the library copy.
    if zotero_key:
        _ref = zotero_sqlite.normalize_reference_key(zotero_key)
        if zotero_sqlite.is_item_key(_ref):
            zotero_key = _ref
        else:
            _item, _via = await zotero_sqlite.resolve_reference(_ref)
            if _item is None:
                _msg = (
                    f"unknown_reference: '{zotero_key}' is not a Zotero item key (8 characters, "
                    "e.g. 'ABCD2345') and does not match any citation key or item URL in the synced "
                    "libraries. Use search_zotero to find the item; its key is shown with each result."
                )
                return FetchedArticle(doi="", text=_msg, error=_msg)
            logger.info("Resolved %r via %s to Zotero item %s", zotero_key, _via, _item.key)
            zotero_key = _item.key
            if not doi and _item.DOI:
                doi = _item.DOI
    elif url and not doi:
        try:
            _item, _via = await zotero_sqlite.resolve_reference(url)
        except Exception as exc:
            logger.debug("Zotero URL lookup failed for %s: %s", url, exc)
            _item = None
        if _item is not None:
            logger.info("URL %s matches Zotero item %s; using the library copy first", url, _item.key)
            zotero_key = _item.key
            doi = _item.DOI or None
    if zotero_key and not doi:
        # Synthesize a stable cache key so the article cache works uniformly.
        doi = f"zotero:{zotero_key}"
    if not doi and url:
        # URL-only: synthesize a stable cache key from the URL hash.
        _url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        doi = f"url:{_url_hash}"

    # Resolve short DOIs (e.g. '10/gm3p2t') to their canonical form before
    # any lookup — Zotero, S2, OpenAlex etc. all store the canonical DOI only.
    if doi and _is_short_doi(doi):
        doi = await _resolve_short_doi(doi)
    pages_str = pages
    section_name = section
    force_html = source == "html"

    # Initialize variables for failure message
    html = None
    extraction = None

    # ── Cache read: fastest path — skip all network/PDF work ────────
    # Bypassed when source='html' so the caller can force a fresh scrape.
    cached_article = text_cache.get_cached(doi) if not force_html else None
    if cached_article:
        cached_article = await _revalidate_cached(cached_article, doi, zotero_key)
    if cached_article and _TRUNCATION_MARKER in cached_article.text[-200:] and not pages:
        # Written when extraction was capped at the response limit; the PDF
        # is still on disk, so extract the whole document now.
        _old_pdf = cached_article.pdf_path
        if _old_pdf and Path(_old_pdf).exists():
            _set_expected({k: cached_article.metadata.get(k) for k in ("title", "authors")})
            try:
                _full = await _cache_pdf_and_return(
                    Path(_old_pdf), doi, cached_article.source,
                    None, mode, section, range_start, range_end,
                )
                if _full is not None:
                    logger.info("Re-extracted truncated cache entry for %s", doi)
                    return _full
            except Exception as exc:
                logger.warning("Re-extraction of truncated %s failed: %s", doi, exc)
    if cached_article:
        logger.debug("Article cache hit for %s", doi)
        if cached_article.pdf_path:
            # Gated inside: only verified web-fetched text is imported.
            zotero_import.enqueue_zotero_import(doi, Path(cached_article.pdf_path), cached_article)
        # Backfill pdf_path for cache entries written before path persistence
        # was added.  Cheap: SQLite lookup + filesystem stat.
        if not cached_article.pdf_path:
            _backfilled = await _backfill_zotero_pdf_path(
                cached_article.doi, zotero_key,
            )
            if _backfilled:
                text_cache.update_paths(
                    text_cache._cache_key(cached_article.doi),
                    pdf_path=_backfilled,
                )
                cached_article.pdf_path = _backfilled
        # Re-run text heuristic when sections are empty OR when the cached
        # article used the text_heuristic path (may have been populated by an
        # older version of the heuristic that lacked footnote/running-header
        # filtering).  HTML and PDF font-analysis entries are high-confidence
        # and not re-processed.
        _should_redetect = (
            mode in ("sections", "section", "preview", "range")
            and cached_article.section_detection in ("text_heuristic", "unknown", "")
        )
        if _should_redetect:
            new_sections = content_extractor.detect_sections_from_text(
                cached_article.text,
                is_ocr=cached_article.metadata.get("is_ocr", False),
            )
            if new_sections != cached_article.sections:
                logger.debug(
                    "Re-ran section detection for %s: %d → %d sections",
                    doi, len(cached_article.sections), len(new_sections),
                )
                cached_article = text_cache.put_cached(
                    cached_article.doi,
                    cached_article.text,
                    cached_article.source,
                    sections=new_sections,
                    section_detection="text_heuristic",
                    word_count=cached_article.word_count,
                    metadata=cached_article.metadata,
                    pdf_path=cached_article.pdf_path,
                    html_path=cached_article.html_path,
                )

        # Lazy upgrade: re-extract with pymupdf4llm when the cached entry used
        # the old font-analysis pipeline and the original PDF is still on disk.
        _should_reextract = (
            config.use_pymupdf4llm
            and cached_article.section_detection in ("pdf_font_analysis", "pdf_toc")
        )
        if _should_reextract and mode in ("sections", "section", "preview", "range"):
            doi_hash = hashlib.md5(doi.encode()).hexdigest()
            pdf_path = config.pdf_cache_dir / f"{doi_hash}.pdf"
            if pdf_path.exists():
                try:
                    new_result = await pdf_work.extract(pdf_path, True)
                    cached_article = text_cache.put_cached(
                        cached_article.doi,
                        new_result["text"],
                        cached_article.source,
                        sections=new_result["sections"],
                        section_detection=new_result.get("section_detection", "pymupdf4llm_markdown"),
                        word_count=len(new_result["text"].split()),
                        metadata=cached_article.metadata,
                        pdf_path=cached_article.pdf_path,
                        html_path=cached_article.html_path,
                    )
                    logger.info(
                        "Re-extracted %s with pymupdf4llm: %d → %d sections",
                        doi, len(cached_article.sections or []), len(new_result["sections"]),
                    )
                except Exception as exc:
                    logger.warning("pymupdf4llm re-extraction failed for %s: %s", doi, exc)

        result = _apply_mode_filter(
            cached_article, mode, section_name, range_start, range_end
        )
        result.auto_import_status = zotero_import.get_auto_import_hint(doi)
        return result

    # Reject force_html early if the stealth browser is not available
    if force_html and not config.use_stealth_browser:
        _msg = (
            "source='html' requires the stealth browser (USE_STEALTH_BROWSER=true). "
            "The stealth browser is not currently enabled in config."
        )
        return FetchedArticle(doi=doi, text=_msg, error=_msg)

    # Acquire per-DOI lock before fetching to prevent duplicate work
    lock = await _get_doi_lock(doi)
    result: FetchedArticle | None = None
    try:
        async with lock:
            # Double-check cache after acquiring lock — another concurrent call
            # may have fetched and cached this DOI while we were waiting.
            # Skip this for force_html — the caller explicitly wants a fresh scrape.
            cached_article = text_cache.get_cached(doi) if not force_html else None
            if cached_article:
                logger.debug("Article cache hit after lock for %s", doi)
                result = _apply_mode_filter(
                    cached_article, mode, section_name, range_start, range_end
                )
            else:
                # ── Step 0: Check Zotero FIRST ──────────────────────────────────
                # source='html' explicitly asks for the publisher page, so the
                # library copy is not consulted (it may be what is being replaced).
                zot_result = None
                if zotero_key:
                    zot_result = await zotero.get_paper_from_zotero_by_key(zotero_key)
                elif not force_html:
                    zot_result = await zotero.get_paper_from_zotero(doi)
                    # Preprints are often saved without a DOI; match the SSRN
                    # abstract id or arXiv id in the item's URL instead.
                    if (
                        not (zot_result or {}).get("found")
                        and doi.lower().startswith(("10.2139/ssrn.", "10.48550/arxiv."))
                    ):
                        try:
                            _alt = await zotero_sqlite.search_by_url(doi)
                            if _alt is not None:
                                logger.info("%s matches Zotero item %s by URL", doi, _alt.key)
                                zot_result = await zotero.get_paper_from_zotero_by_key(_alt.key)
                        except Exception as exc:
                            logger.debug("Zotero preprint URL match failed for %s: %s", doi, exc)
                if zot_result and zot_result.get("found"):
                    _ctx_now = _current_ctx()
                    _set_expected(_zotero_expected(zot_result))
                    # An attachment this server auto-imported is a web download
                    # like any other; the user's own attachments are trusted.
                    _ctx_now.zotero_untrusted = "auto-imported" in (zot_result.get("tags") or [])
                    if _ctx_now.zotero_untrusted and _is_real_doi(doi):
                        _set_expected({"titles": await _published_titles(doi)})
                    _zot_pdf = zot_result.get("pdf_path")
                    _zot_pdf_str = str(_zot_pdf) if _zot_pdf else None
                    _zot_meta = {
                        "zotero_key": zot_result.get("item_key") or zotero_key or "",
                        "attachment_key": zot_result.get("attachment_key") or "",
                        "library": zot_result.get("libraryName") or "",
                    }
                    _zot_meta.update({k: v for k, v in _zotero_expected(zot_result).items() if v})
                    # Zotero's own index stops after a configured number of
                    # pages; the attachment itself is complete.
                    _use_pdf = bool(
                        _zot_pdf_str and Path(_zot_pdf_str).exists()
                        and (not zot_result.get("text") or zot_result.get("truncated"))
                    )
                    if zot_result.get("text") and not _use_pdf:
                        raw_text = zot_result["text"]
                        cached_article = _accept_text(
                            doi, raw_text, zot_result["source"],
                            sections=content_extractor.detect_sections_from_text(raw_text),
                            section_detection="text_heuristic",
                            metadata=_zot_meta,
                            pdf_path=_zot_pdf_str,
                        )
                        if cached_article is not None:
                            result = _apply_mode_filter(
                                cached_article, mode, section_name, range_start, range_end
                            )
                            if _zot_pdf_str:
                                result.pdf_path = result.pdf_path or _zot_pdf_str

                    # Got PDF path from Zotero — extract text from disk
                    elif _zot_pdf_str:
                        result = await _cache_pdf_and_return(
                            zot_result["pdf_path"], doi, zot_result["source"],
                            pages_str, mode, section_name, range_start, range_end,
                        )
                        if result is not None and not pages_str:
                            _c = text_cache.get_cached(doi)
                            if _c is not None:
                                _c.metadata.update({k: v for k, v in _zot_meta.items() if v})
                                text_cache.put_cached(
                                    _c.doi, _c.text, _c.source, sections=_c.sections,
                                    section_detection=_c.section_detection,
                                    word_count=_c.word_count, metadata=_c.metadata,
                                    pdf_path=_c.pdf_path, html_path=_c.html_path,
                                )
                                result.metadata = dict(_c.metadata)
                    # Later web tiers are judged as web sources again.
                    _ctx_now.zotero_untrusted = False

                # For Zotero-only (no real DOI), the rest of the pipeline has
                # nothing to work with unless the Zotero item has a `url` field.
                # Check for that first; if found, promote it so the URL tier
                # (below) can handle it.
                if result is None and zotero_key and doi.startswith("zotero:"):
                    if url is None:
                        # Try to read the `url` field from the Zotero item.
                        try:
                            _zot_item = await zotero_sqlite.search_by_key(zotero_key)
                            if _zot_item and (_zot_item.url or "").strip():
                                url = _zot_item.url.strip()
                                _url_hash2 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                                doi = f"url:{_url_hash2}"
                                logger.info(
                                    "zotero_key %s has no usable attachment but has url=%s; "
                                    "delegating to URL tier.",
                                    zotero_key, url,
                                )
                        except Exception as _e:
                            logger.debug("Failed to look up Zotero url for %s: %s", zotero_key, _e)

                    if result is None and url is None:
                        zot_source = (zot_result or {}).get("source") or ""
                        meta = (zot_result or {}).get("metadata") or {}
                        title = meta.get("title") or "(untitled)"
                        _rej = _current_ctx().rejections
                        if not zot_result or not zot_result.get("found"):
                            _msg = (
                                f"unknown_reference: Zotero item {zotero_key} was not found in any "
                                "synced library. Check the key with search_zotero."
                            )
                        elif _rej:
                            _msg = (
                                f"identity_mismatch: the attachment of Zotero item {zotero_key} "
                                f"('{title}') contains a different work ('{_rej[-1].get('found_title') or 'unknown'}'). "
                                "It was added by this server's auto-import and was not used. "
                                "Replace the attachment in Zotero, then retry."
                            )
                        elif zot_source.endswith("metadata_only") or not zot_source:
                            _msg = (
                                f"item_found_no_attachment: Zotero item {zotero_key} ('{title}') "
                                "has no PDF attachment, and has no DOI or URL to fetch it from. "
                                "Attach the PDF in Zotero, then retry."
                            )
                        else:
                            _msg = (
                                f"attachment_unreadable: Zotero item {zotero_key} ('{title}') has an "
                                f"attachment (source: {zot_source}) but no text could be extracted from it."
                            )
                        return FetchedArticle(doi=doi, text=_msg, error=_msg)

                if result is None:
                    # ── Tier 0.5: URL-driven fetch ──────────────────────────────────
                    # Fires when a URL is available (passed in directly, or promoted
                    # from the Zotero item's url field above).  Runs before the
                    # DOI-based tiers; those tiers are no-ops for synthetic url:* keys.
                    if url:
                        logger.info("Trying URL tier for %s", url)
                        from urllib.parse import urlparse as _urlparse
                        _parsed = _urlparse(url)
                        _url_path_lower = (_parsed.path or "").lower()
                        _url_pdf_path: "Path | None" = None

                        # Fast path: URL ends in .pdf — try direct download first.
                        if _url_path_lower.endswith(".pdf"):
                            try:
                                _url_pdf_path = await pdf_fetcher.fetch_direct(url)
                            except Exception as _ue:
                                logger.info("URL tier: direct PDF fetch failed for %s: %s", url, _ue)

                        # Content-Type probe: some URLs serve PDFs without .pdf extension.
                        if not _url_pdf_path and not _url_path_lower.endswith(".pdf"):
                            try:
                                async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as _hc:
                                    _head = await _hc.head(url)
                                    _ctype = (_head.headers.get("content-type") or "").lower()
                                    if "application/pdf" in _ctype:
                                        _url_pdf_path = await pdf_fetcher.fetch_direct(url)
                            except Exception as _ue2:
                                logger.debug("URL tier: HEAD probe failed for %s: %s", url, _ue2)

                        if not _url_pdf_path and use_proxy:
                            _url_pdf_path = await pdf_fetcher.fetch_proxied(url)

                        if _url_pdf_path:
                            result = await _cache_pdf_and_return(
                                _url_pdf_path, doi, "url_direct_pdf",
                                pages_str, mode, section_name, range_start, range_end,
                            )

                        # Landing-page path: stealth browser + trafilatura pipeline.
                        if result is None and config.use_stealth_browser:
                            try:
                                _lp = await _extract_from_landing_page(url, use_proxy)
                                if _lp:
                                    if _lp["pdf_path"]:
                                        result = await _cache_pdf_and_return(
                                            _lp["pdf_path"], doi, _lp["source"],
                                            pages_str, mode, section_name, range_start, range_end,
                                        )
                                    elif _lp["text"]:
                                        _raw = _lp["text"]
                                        _cached_lp = _accept_text(
                                            doi, _raw, _lp["source"],
                                            sections=_lp["sections"],
                                            section_detection=_lp["section_detection"],
                                            word_count=_lp["word_count"],
                                            metadata={"url": url},
                                            page_title=_lp.get("page_title", ""),
                                            page_doi=_lp.get("page_doi", ""),
                                        )
                                        if _cached_lp is not None:
                                            result = _apply_mode_filter(
                                                _cached_lp, mode, section_name, range_start, range_end
                                            )
                            except Exception as _ue3:
                                logger.info("URL tier: landing-page extraction failed for %s: %s", url, _ue3)

                        # If URL tier succeeded and doi is synthetic, return now.
                        # If URL tier failed and doi is synthetic (url:*), bail out early
                        # — there are no DOI-based tiers that can help.
                        if doi.startswith("url:") and result is None:
                            _msg = (
                                f"Could not retrieve content from URL: {url}\n\n"
                                "Tried: direct PDF download"
                                + (", stealth browser + HTML extraction" if config.use_stealth_browser else "")
                                + ".\n"
                                "Check that the URL is publicly accessible, or save the "
                                "document to Zotero and retry with zotero_key."
                            )
                            return FetchedArticle(doi=doi, text=_msg, error=_msg)

                    # ── Step 0b: SSRN DOI remapping ─────────────────────────────────
                    # For SSRN preprints, try to find the published version's DOI and
                    # any OA PDF URLs before doing any network fetching.  We skip this
                    # when re-entering the pipeline via a remap (to avoid recursion).
                    _ssrn_remap: dict | None = None
                    if doi.startswith("10.2139/ssrn.") and not _original_ssrn_doi:
                        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _c:
                            _ssrn_remap = await apis.resolve_ssrn_doi(doi, _c)
                        _set_expected({"title": (_ssrn_remap or {}).get("title")})

                        # Try OA PDF URLs discovered by remap
                        for _oa_url in (_ssrn_remap or {}).get("oa_pdf_urls", []):
                            _oa_path = await pdf_fetcher.fetch_direct(_oa_url)
                            if _oa_path:
                                result = await _cache_pdf_and_return(
                                    _oa_path, doi, "ssrn_remap_oa",
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                                if result:
                                    break

                        # If a published DOI was found, re-enter pipeline with it
                        if result is None and (_ssrn_remap or {}).get("published_doi"):
                            _pub_doi = _ssrn_remap["published_doi"]
                            logger.info("SSRN %s → published %s", doi, _pub_doi)
                            result = await fetch_article(
                                _ArticleId(doi=_pub_doi),
                                mode=mode, section=section_name,
                                range_start=range_start, range_end=range_end,
                                use_proxy=use_proxy, pages=pages_str, source=source,
                                _original_ssrn_doi=doi,
                            )

                        # If still nothing, try title-based search for a published version
                        if result is None and (_ssrn_remap or {}).get("title"):
                            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _c:
                                _title_remap = await apis.search_by_title_for_published_version(
                                    _ssrn_remap["title"], doi, _c
                                )
                            if _title_remap and _title_remap.get("published_doi"):
                                logger.info(
                                    "SSRN title search %r → %s",
                                    _ssrn_remap["title"][:60], _title_remap["published_doi"],
                                )
                                result = await fetch_article(
                                    _ArticleId(doi=_title_remap["published_doi"]),
                                    mode=mode, section=section_name,
                                    range_start=range_start, range_end=range_end,
                                    use_proxy=use_proxy, pages=pages_str, source=source,
                                    _original_ssrn_doi=doi,
                                )

                    # ── Step 1: Gather candidate PDF URLs from external APIs ────────
                    s2_paper = None
                    oa_paper = None
                    unpaywall_data = None

                    metadata = await asyncio.gather(
                        apis.s2_paper(f"DOI:{doi}"), apis.openalex_work(doi),
                        apis.unpaywall_lookup(doi), return_exceptions=True,
                    )
                    s2_paper, oa_paper, unpaywall_data = [m if isinstance(m, dict) else None for m in metadata]

                    # Build citation metadata from whatever API data we have
                    cite_meta: dict = {}
                    if s2_paper:
                        cite_meta["title"] = s2_paper.get("title", "")
                        cite_meta["year"] = s2_paper.get("year", "")
                        cite_meta["venue"] = s2_paper.get("venue", "")
                        authors = s2_paper.get("authors", [])
                        if authors:
                            cite_meta["authors"] = [a.get("name", "") for a in authors if a.get("name")]
                    if oa_paper and not cite_meta.get("title"):
                        cite_meta["title"] = oa_paper.get("title", "")
                        cite_meta["year"] = str(oa_paper.get("publication_year", ""))
                        # OpenAlex has authorship structure
                        authorships = oa_paper.get("authorships", [])
                        if authorships:
                            cite_meta["authors"] = [
                                a.get("author", {}).get("display_name", "")
                                for a in authorships if a.get("author", {}).get("display_name")
                            ]

                    candidate_urls = apis.collect_pdf_urls(s2_paper, oa_paper, unpaywall_data)

                    # The record every later tier must match.
                    _set_expected(cite_meta)
                    if not _current_ctx().expected.get("title") and _is_real_doi(doi):
                        _set_expected(await _expected_for(doi))
                    if not cite_meta.get("title") and _current_ctx().expected.get("title"):
                        cite_meta.update({k: v for k, v in _current_ctx().expected.items() if v})

                    # ── Step 2: Direct HTTP on candidate URLs (fast; handles arXiv/OA) ──
                    config.evict_cache_lru()
                    for candidate in candidate_urls:
                        url = candidate["url"]
                        source = candidate["source"]
                        pdf_cached = pdf_fetcher._cache_path(url)
                        if pdf_cached.exists() and pdf_fetcher._is_pdf_file(pdf_cached):
                            logger.info("Cache hit for %s", url)
                            result = await _cache_pdf_and_return(
                                pdf_cached, doi, f"{source} (cached)",
                                pages_str, mode, section_name, range_start, range_end,
                            )
                            if result:
                                break
                        path = await pdf_fetcher.fetch_direct(url)
                        if path:
                            result = await _cache_pdf_and_return(
                                path, doi, f"{source} (direct)",
                                pages_str, mode, section_name, range_start, range_end,
                            )
                            if result:
                                break

                    # ── Step 2b: CORE.ac.uk ─────────────────────────────────────────
                    if result is None and config.core_api_key:
                        _core_title = cite_meta.get("title") or (_ssrn_remap or {}).get("title")
                        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _cc:
                            # DOI lookup first, then title if no direct hit
                            _core_hits = await core_api.search_core(doi=doi, client=_cc)
                            if not _core_hits and _core_title:
                                _core_hits = await core_api.search_core(title=_core_title, client=_cc)
                            for _ch in _core_hits:
                                if _ch.get("core_id"):
                                    _core_path = await core_api.download_from_core(
                                        _ch["core_id"], _ch.get("download_url"), _cc
                                    )
                                    if _core_path:
                                        result = await _cache_pdf_and_return(
                                            _core_path, doi, "core.ac.uk",
                                            pages_str, mode, section_name, range_start, range_end,
                                        )
                                        if result:
                                            break
                                # Also try sourceFulltextUrls directly
                                if result is None:
                                    for _src_url in _ch.get("source_fulltext_urls") or []:
                                        _src_path = await pdf_fetcher.fetch_direct(_src_url)
                                        if _src_path:
                                            result = await _cache_pdf_and_return(
                                                _src_path, doi, "core_source_url",
                                                pages_str, mode, section_name, range_start, range_end,
                                            )
                                            if result:
                                                break
                                if result:
                                    break

                    # ── Step 2c: Web search fallback (Serper / Brave) ────────────────
                    if result is None and (config.serper_api_key or config.brave_search_api_key):
                        _ws_title = cite_meta.get("title") or (_ssrn_remap or {}).get("title")
                        if _ws_title:
                            _ws_authors = cite_meta.get("authors") or []
                            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _wc:
                                _ws_hits = await web_search.search_for_pdf(
                                    _ws_title, _ws_authors, _wc
                                )
                            for _wh in _ws_hits:
                                _ws_url = _wh["url"]
                                # Only attempt direct PDF URLs; landing pages are
                                # handled by the stealth browser tier (step 3).
                                _ws_url_lower = _ws_url.lower()
                                if (
                                    not _ws_url_lower.endswith(".pdf")
                                    and "viewcontent.cgi" not in _ws_url_lower
                                ):
                                    continue
                                _ws_path = await pdf_fetcher.fetch_direct(_ws_url)
                                if not _ws_path and use_proxy:
                                    _ws_path = await pdf_fetcher.fetch_proxied(_ws_url)
                                if _ws_path:
                                    # Validate: is this actually the paper we wanted?
                                    if not pdf_fetcher._pdf_matches_expected_paper(
                                        _ws_path, _ws_title, _ws_authors
                                    ):
                                        logger.info(
                                            "Web search result rejected by validation: %s",
                                            _ws_url,
                                        )
                                        try:
                                            _ws_path.unlink(missing_ok=True)
                                        except OSError:
                                            pass
                                        continue
                                    result = await _cache_pdf_and_return(
                                        _ws_path, doi, f"web_search ({_wh['source']})",
                                        pages_str, mode, section_name, range_start, range_end,
                                    )
                                    if result:
                                        break

                    # ── Step 3: Scrapling fetch of DOI landing page ──────────────────
                    #
                    # For normal fetches: delegates to _extract_from_landing_page which
                    # handles citation_pdf_url, trafilatura, and PDF link scanning.
                    # For force_html: keeps inline for the specialised cache-refresh logic.
                    if result is None and not force_html and not doi.startswith("url:"):
                        doi_url = (
                            f"https://doi.org/{doi}" if not doi.startswith("http") else doi
                        )
                        lp = await _extract_from_landing_page(doi_url, use_proxy, expected_doi=doi)
                        if lp:
                            if lp["pdf_path"]:
                                result = await _cache_pdf_and_return(
                                    lp["pdf_path"], doi, lp["source"],
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                            elif lp["text"]:
                                raw_text = lp["text"]
                                cached_article = _accept_text(
                                    doi, raw_text, lp["source"],
                                    sections=lp["sections"],
                                    section_detection=lp["section_detection"],
                                    word_count=lp["word_count"],
                                    metadata=cite_meta,
                                    page_title=lp.get("page_title", ""),
                                    page_doi=lp.get("page_doi", ""),
                                )
                                if cached_article is not None:
                                    result = _apply_mode_filter(
                                        cached_article, mode, section_name, range_start, range_end
                                    )

                    if force_html:
                        doi_url = (
                            f"https://doi.org/{doi}" if not doi.startswith("http") else doi
                        )
                        scrapling_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(
                            doi_url, use_proxy=use_proxy
                        )
                        # force_html: skip direct PDF, use HTML extraction only.
                        if html:
                            effective_url = final_url or doi_url
                            meta = content_extractor.extract_citation_meta(html, effective_url)
                            citation_doi = meta.get("citation_doi", "")
                            if citation_doi and zotero._normalize_doi(citation_doi) != zotero._normalize_doi(doi):
                                logger.warning(
                                    "DOI mismatch: requested %s, page reports %s — discarding HTML",
                                    doi, citation_doi,
                                )
                                html = None
                            if html:
                                extraction = await content_extractor.extract_article_with_sections(
                                    html, effective_url
                                )
                                if extraction:
                                    raw_text = extraction["text"]
                                    sections = extraction["sections"] or content_extractor.detect_sections_from_text(raw_text)
                                    section_det = extraction["section_detection"] if extraction["sections"] else "text_heuristic"
                                    html_source = f"html_extraction ({extraction['source']})"
                                    html_words = extraction["word_count"]
                                    html_sections = len(sections)
                                    html_is_good = html_words > 1500 and html_sections >= 3
                                    _prior = text_cache.get_cached(doi)
                                    if html_is_good or _prior is None:
                                        cached_article = _accept_text(
                                            doi, raw_text, html_source,
                                            sections=sections,
                                            section_detection=section_det,
                                            word_count=html_words,
                                            metadata=cite_meta,
                                            page_title=meta.get("citation_title", ""),
                                            page_doi=citation_doi,
                                        )
                                    else:
                                        # HTML exists but isn't good enough to replace cache
                                        cached_article = _prior
                                    if cached_article is not None:
                                        result = _apply_mode_filter(
                                            cached_article, mode, section_name, range_start, range_end
                                        )

                    # ── Step 4: Proxied fetch on candidates (institutional access) ───
                    if result is None and use_proxy and config.gost_proxy_url:
                        for candidate in candidate_urls:
                            path = await pdf_fetcher.fetch_proxied(candidate["url"])
                            if path:
                                result = await _cache_pdf_and_return(
                                    path, doi, f"{candidate['source']} (proxied)",
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                                if result:
                                    break
                        if result is None and not doi.startswith("url:"):
                            doi_url = (
                                f"https://doi.org/{doi}" if not doi.startswith("http") else doi
                            )
                            path = await pdf_fetcher.fetch_proxied(doi_url)
                            if path:
                                result = await _cache_pdf_and_return(
                                    path, doi, "doi_redirect (proxied)",
                                    pages_str, mode, section_name, range_start, range_end,
                                )

                    # ── Step 5: Scrapling on candidate URLs (last resort) ────────────
                    if result is None and config.use_stealth_browser:
                        for candidate in candidate_urls:
                            scrap_path, _html, _scrap_url = await pdf_fetcher.fetch_with_scrapling(
                                candidate["url"], use_proxy=use_proxy
                            )
                            if scrap_path:
                                result = await _cache_pdf_and_return(
                                    scrap_path, doi, f"{candidate['source']} (scrapling)",
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                                if result:
                                    break

                    # ── Step 6: HeinOnline (law review + institutional proxy) ────────
                    if result is None and web_search._looks_like_law_review(cite_meta):
                        _hein_title = cite_meta.get("title") or (_ssrn_remap or {}).get("title")
                        if _hein_title:
                            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _hc:
                                _hein_path = await web_search.fetch_from_heinonline(_hein_title, _hc)
                            if _hein_path:
                                result = await _cache_pdf_and_return(
                                    _hein_path, doi, "heinonline",
                                    pages_str, mode, section_name, range_start, range_end,
                                )

                    # ── Step 8: SSRN cookie injection ────────────────────────────────
                    if result is None and doi.startswith("10.2139/ssrn.") and config.ssrn_cookies:
                        _ssrn_id = doi.rsplit(".", 1)[-1]
                        _ssrn_page_url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={_ssrn_id}"
                        logger.info("Trying SSRN cookie injection for %s", doi)
                        _ssrn_html = await web_search.fetch_ssrn_with_cookies(_ssrn_page_url)
                        if _ssrn_html:
                            # Extract direct PDF link from the SSRN page
                            _ssrn_pdf_link = pdf_fetcher._extract_pdf_link_from_html(
                                _ssrn_html, _ssrn_page_url
                            )
                            if _ssrn_pdf_link:
                                _ssrn_path = await pdf_fetcher.fetch_direct(_ssrn_pdf_link)
                                if not _ssrn_path and use_proxy:
                                    _ssrn_path = await pdf_fetcher.fetch_proxied(_ssrn_pdf_link)
                                if _ssrn_path:
                                    result = await _cache_pdf_and_return(
                                        _ssrn_path, doi, "ssrn_cookies",
                                        pages_str, mode, section_name, range_start, range_end,
                                    )
                            # Also try HTML extraction if the page has full text
                            if result is None:
                                _ssrn_extraction = await content_extractor.extract_article_with_sections(
                                    _ssrn_html, _ssrn_page_url
                                )
                                if _ssrn_extraction and _ssrn_extraction.get("word_count", 0) > 1500:
                                    _raw = _ssrn_extraction["text"]
                                    _secs = _ssrn_extraction["sections"] or content_extractor.detect_sections_from_text(_raw)
                                    _cached_art = _accept_text(
                                        doi, _raw, "ssrn_cookies_html",
                                        sections=_secs,
                                        section_detection=_ssrn_extraction.get("section_detection", "text_heuristic"),
                                        word_count=_ssrn_extraction["word_count"],
                                        metadata=cite_meta,
                                    )
                                    if _cached_art is not None:
                                        result = _apply_mode_filter(
                                            _cached_art, mode, section_name, range_start, range_end
                                        )

                    # ── Failure ──────────────────────────────────────────────────────
                    if result is None:
                        _original_doi = _original_ssrn_doi or doi
                        _is_ssrn = _original_doi.startswith("10.2139/ssrn.")

                        if _is_ssrn:
                            _ssrn_id = _original_doi.rsplit(".", 1)[-1]
                            _ssrn_url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={_ssrn_id}"
                            _pub_note = ""
                            if (_ssrn_remap or {}).get("published_doi"):
                                _pub_note = (
                                    "\n\nNote: this paper may also be published as "
                                    f"https://doi.org/{_ssrn_remap['published_doi']} — "
                                    "try fetch_fulltext on that DOI if available.\n"
                                )
                            _ssrn_lines = [
                                f"Could not retrieve {_original_doi} automatically "
                                "(SSRN blocks bots).\n\n",
                                "**Surface this clickable link to the user so they can grab it themselves:**\n\n",
                                f"  → {_ssrn_url}\n\n",
                                "Recommended: open the link, then use the **Zotero browser connector** "
                                "to save the PDF + metadata to their library. Once it's in Zotero, "
                                "this MCP will find it automatically on future searches (search_papers "
                                "checks Zotero first) — no DOI juggling needed next time.\n\n",
                                "Alternatives: download the PDF and attach it directly to this "
                                "conversation, or drop it into Zotero manually.\n",
                                _pub_note,
                                "\nOnce the paper is available, re-run this request.",
                            ]
                            _ssrn_text = "".join(_ssrn_lines)
                            result = FetchedArticle(
                                doi=doi, text=_ssrn_text, source="", error=_ssrn_text,
                            )
                        else:
                            sources_tried = [c["source"] for c in candidate_urls]
                            doi_url = f"https://doi.org/{doi}"
                            _hints: list[str] = []

                            if not use_proxy and config.gost_proxy_url:
                                _hints.append(
                                    "\n→ Try with institutional proxy: "
                                    f"fetch_fulltext(doi=\"{doi}\", use_proxy=true)\n"
                                )
                            elif not config.gost_proxy_url:
                                _hints.append(
                                    "\n→ No institutional proxy configured. If you have institutional access, "
                                    "configure GOST_PROXY_URL in .env.\n"
                                )

                            _hints.append(f"→ Check available URLs: find_pdf_urls(doi=\"{doi}\")\n")
                            _hints.append(f"→ Verify metadata: get_paper(identifier=\"{doi}\")\n")
                            _hints.append(
                                f"\n**Ask the user to:**\n"
                                f"1. Open {doi_url} in their browser and download the PDF\n"
                                f"2. Save it to Zotero, or attach the PDF to this conversation\n"
                                f"\nOnce available, re-run this request."
                            )

                            if html and not extraction:
                                _hints.append(
                                    "\n\nNote: The publisher page was reached but the full article text was not "
                                    "available — likely behind a paywall. Only the abstract could be accessed.\n"
                                )

                            _rejected = _current_ctx().rejections
                            if _rejected:
                                _lines = "".join(
                                    f"  - {r['source']}: found '{(r.get('found_title') or 'unknown document')[:120]}' "
                                    f"({r['reason']})\n"
                                    for r in _rejected
                                )
                                _hints.insert(0, (
                                    "\nidentity_mismatch: these sources returned a different work and were "
                                    "rejected rather than returned under this DOI:\n" + _lines
                                ))
                            _sources_str = ", ".join(sources_tried) or "none found"
                            result = FetchedArticle(
                                doi=doi,
                                text="",
                                source="",
                                error=f"Could not retrieve full text for DOI: {doi}\nSources tried: {_sources_str}",
                                failure_hints=_hints,
                                attempted_sources=sources_tried,
                            )
    finally:
        # Cleanup: remove the lock if nobody else is waiting on it
        async with _doi_locks_lock:
            if doi in _doi_locks and not _doi_locks[doi].locked():
                del _doi_locks[doi]

    return result


async def verified_cached(doi: str) -> "text_cache.CachedArticle | None":
    """Cached article for *doi* after identity re-verification (None if absent or purged)."""
    cached = text_cache.get_cached(doi)
    if cached is None:
        return None
    token = _ctx.set(_FetchContext())
    try:
        return await _revalidate_cached(cached, doi, None)
    finally:
        _ctx.reset(token)


async def fetch_article(
    identifier: ArticleId, mode="sections", section=None, range_start=None,
    range_end=None, use_proxy=False, pages=None, source="auto", _original_ssrn_doi=None,
) -> FetchedArticle:
    """Normal retrieval, with a durable desktop fallback for unavailable text."""
    ctx = _FetchContext()
    token = _ctx.set(ctx)
    attempts: list = []
    attempts_token = pdf_fetcher.attempt_log.set(attempts)
    try:
        result = await _fetch_article(identifier, mode, section, range_start, range_end,
                                      use_proxy, pages, source, _original_ssrn_doi)
    finally:
        pdf_fetcher.attempt_log.reset(attempts_token)
        _ctx.reset(token)
    if result.error and attempts and not result.source:
        lines = "".join(f"  - {a['url'][:140]} [{a['via']}]: {a['result']}\n" for a in attempts[-12:])
        result.failure_hints = list(result.failure_hints) + [
            "\nWhat each source returned:\n" + lines
        ]
    if ctx.rejections:
        result.rejected_sources = list(ctx.rejections)
    if result.error and not result.source and (identifier.doi or identifier.url):
        from .. import browser_acquisition
        if browser_acquisition.enabled():
            try:
                job = await browser_acquisition.request_acquisition(
                    doi=result.doi if not result.doi.startswith(("url:", "zotero:")) else None,
                    url=identifier.url)
                result.browser_acquisition = job
                result.failure_hints = [
                    h for h in result.failure_hints
                    if h.startswith(("\nidentity_mismatch", "\nWhat each source returned"))
                ] + [
                    f"\nDesktop acquisition {job['id']} is {job['state']}. "
                    "Use browser_acquisition(action='status', id='" + job['id'] +
                    "') for progress. Complete any login on the iMac, then resume. "
                    "Once content_ready is true, repeat fetch_fulltext.\n"
                ]
            except Exception as exc:
                logger.warning("Could not queue desktop browser acquisition: %s", exc)
    return result
