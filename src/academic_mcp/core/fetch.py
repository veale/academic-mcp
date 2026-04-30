"""Business logic for fetch_fulltext (PDF / HTML article retrieval).

Contains:
- Per-DOI async locking primitives
- Formatting helpers (_format_citation_header, _apply_mode_filter, etc.)
- fetch_article(args) — the full retrieval pipeline
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import httpx

from .types import FetchedArticle, FetchMode, PreviewChunk, Section

from .. import (
    apis,
    content_extractor,
    core_api,
    pdf_extractor,
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
# Citation header formatter
# ---------------------------------------------------------------------------

def _format_citation_header(doi: str, metadata: dict | None = None) -> str:
    """Format a short citation block for prepending to article text.

    Returns an empty string if no metadata is available.
    """
    if not metadata:
        return ""

    parts = []
    if metadata.get("title"):
        parts.append(f"Title: {metadata['title']}")
    if metadata.get("authors"):
        authors = metadata["authors"]
        if isinstance(authors, list):
            if len(authors) <= 3:
                parts.append(f"Authors: {', '.join(authors)}")
            else:
                parts.append(f"Authors: {', '.join(authors[:3])} et al.")
        elif isinstance(authors, str):
            parts.append(f"Authors: {authors}")
    if metadata.get("year"):
        parts.append(f"Year: {metadata['year']}")
    if metadata.get("venue"):
        parts.append(f"Venue: {metadata['venue']}")
    parts.append(f"DOI: {doi}")

    if not parts:
        return ""

    return "── Citation ──\n" + "\n".join(parts) + "\n──────────────\n\n"


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

    scrapling_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(url)
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


def _cached_article_result(
    cached: "text_cache.CachedArticle",
    text: str,
    *,
    error: str | None = None,
    truncated: bool = False,
    pdf_path: str | None = None,
    html_path: str | None = None,
) -> FetchedArticle:
    return FetchedArticle(
        doi=str(cached.doi or ""),
        text=text,
        source=str(cached.source or ""),
        sections=list(cached.sections or []),
        section_detection=str(cached.section_detection or "unknown"),
        word_count=int(cached.word_count or 0),
        metadata=dict(cached.metadata or {}),
        error=error,
        truncated=truncated,
        pdf_path=pdf_path,
        html_path=html_path,
        cache_key=text_cache._cache_key(str(cached.doi or "")),
    )


def _apply_mode_filter(
    cached: "text_cache.CachedArticle",
    mode: str,
    section_name: str | None,
    range_start: int | None,
    range_end: int | None,
) -> FetchedArticle:
    """Apply a mode filter to a cached article and return formatted text."""
    doi = cached.doi

    # Prepend citation header for all modes except "sections"
    if mode != "sections":
        citation_header = _format_citation_header(doi, cached.metadata)
    else:
        citation_header = ""

    if mode == "sections":
        det = cached.section_detection
        det_note = {
            "html_headings":    "html_headings (high confidence — publisher <h2>/<h3> tags)",
            "pdf_font_analysis": "pdf_font_analysis (reliable — font-size threshold on spans)",
            "text_heuristic":   "text_heuristic (approximate — regex on plain text)",
            "keyword_skeleton": "keyword_skeleton (TF-IDF chunks — no structural headings found)",
            "unknown":          "unknown (migrated cache entry)",
        }.get(det, det)

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
                )
                lines = [
                    f"Document map for DOI: {doi}\n"
                    f"Source: {cached.source}\n"
                    "Navigation: keyword_skeleton (no structural headings detected)\n",
                    "=" * 60 + "\n",
                ]
                total = len(skeleton)
                for chunk in skeleton:
                    kw = ", ".join(chunk.get("keywords", []))
                    wc = chunk.get("word_count", 0)
                    start = chunk.get("start", 0)
                    end = chunk.get("end", 0)
                    n = chunk.get("chunk", 0)
                    lines.append(
                        f"[{n}/{total}]  chars {start:,}–{end:,} ({wc} words): {kw}\n"
                    )
                    structured_sections.append(Section(
                        title=f"[{n}/{total}]",
                        char_start=start,
                        char_end=end,
                        level=2,
                        keywords=chunk.get("keywords", []),
                        word_count=wc,
                        is_infill=True,
                    ))
                lines += [
                    "\n",
                    f"→ fetch_fulltext(doi=\"{doi}\", mode=\"range\", range_start=N, range_end=M)\n",
                    f"→ search_in_article(doi=\"{doi}\", terms=[\"keyword\"])\n",
                ]
                text = "".join(lines)
            else:
                text = (
                    f"No sections detected for DOI: {doi}\n"
                    f"Section detection: {det_note}\n"
                    "Try mode='full' to read the entire text.\n"
                )

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
            has_infill = any(s.get("_infill") for s in display_sections)

            effective_det_note = (det_note + " + keyword infill") if has_infill else det_note

            lines = [
                f"Sections for DOI: {doi}\n"
                f"Source: {cached.source}\n"
                f"Section detection: {effective_det_note}\n",
                "=" * 60 + "\n",
            ]

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
                    kw = ", ".join(entry.get("keywords", []))
                    title = entry["title"]
                    lines.append(
                        f"  {title} chars {start:,}–{end:,} ({wc} words): {kw}\n"
                    )
                    structured_sections.append(Section(
                        title=title,
                        char_start=start,
                        char_end=end,
                        level=3,
                        keywords=entry.get("keywords", []),
                        word_count=wc,
                        is_infill=True,
                    ))
                else:
                    indent = "  " if entry.get("level", 2) == 3 else ""
                    kw_list = kw_by_start.get(start, [])
                    kw_str = ", ".join(kw_list) if kw_list else ""
                    lines.append(
                        f"{indent}[{structural_idx}] {entry['title']}  ({wc} words, chars {start:,}–{end:,})\n"
                    )
                    if kw_str:
                        lines.append(f"{indent}    → {kw_str}\n")
                    structural_idx += 1
                    structured_sections.append(Section(
                        title=entry["title"],
                        char_start=start,
                        char_end=end,
                        level=entry.get("level", 2),
                        keywords=kw_list,
                        word_count=wc,
                        is_infill=False,
                    ))

            lines += [
                "\n",
                f"→ fetch_fulltext(doi=\"{doi}\", mode=\"range\", range_start=N, range_end=M)\n",
                f"→ search_in_article(doi=\"{doi}\", terms=[\"keyword\"])\n",
            ]
            text = "".join(lines)

        fa = _cached_article_result(cached, citation_header + text)
        fa.mode = FetchMode.sections
        fa.available_sections = structured_sections
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
        header = (
            f"Section: {match['title']}\n"
            f"DOI: {doi}\n"
            f"Source: {cached.source}\n"
            + "=" * 60 + "\n\n"
        )
        full = header + section_text
        if len(full) > config.max_context_length:
            full = full[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
        fa = _cached_article_result(cached, citation_header + full)
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
        lines = [
            f"Preview for DOI: {doi}\nSource: {cached.source}\n",
            "=" * 60 + "\n\n",
        ]
        preview_chunks: list[PreviewChunk] = []
        if not cached.sections:
            # No section data — return first 2 000 chars as a preview
            lines.append(cached.text[:2000])
            preview_chunks.append(PreviewChunk(
                section_title=None,
                text=cached.text[:2000],
                word_count_total=len(cached.text.split()),
                word_count_shown=len(cached.text[:2000].split()),
            ))
            if len(cached.text) > 2000:
                lines.append(f"\n\n[... {len(cached.text) - 2000} more characters — use mode='full' ...]")
        else:
            abstract_sec = next(
                (s for s in cached.sections if "abstract" in s["title"].lower()), None
            )
            if abstract_sec:
                end = abstract_sec.get("end") or len(cached.text)
                abs_text = cached.text[abstract_sec["start"]:end]
                lines.append(f"## {abstract_sec['title']}\n")
                lines.append(abs_text)
                lines.append("\n\n")
                preview_chunks.append(PreviewChunk(
                    section_title=abstract_sec["title"],
                    text=abs_text,
                    word_count_total=len(abs_text.split()),
                    word_count_shown=len(abs_text.split()),
                ))
            else:
                # Show pre-first-heading preamble (likely abstract)
                first_start = cached.sections[0]["start"] if cached.sections else len(cached.text)
                if first_start > 0:
                    preamble = cached.text[:first_start]
                    lines.append(preamble)
                    lines.append("\n\n")
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
                lines.append(f"## {sec['title']}\n")
                lines.append(" ".join(words[:200]))
                remaining = len(words) - 200
                if remaining > 0:
                    lines.append(
                        f"\n[... {remaining} more words — use mode='section', "
                        f"section='{sec['title']}' to read in full ...]\n"
                    )
                lines.append("\n\n")
                preview_chunks.append(PreviewChunk(
                    section_title=sec["title"],
                    text=" ".join(words[:200]),
                    word_count_total=len(words),
                    word_count_shown=min(200, len(words)),
                ))
        text = "".join(lines)
        if len(text) > config.max_context_length:
            text = text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
            fa = _cached_article_result(cached, citation_header + text, truncated=True)
            fa.mode = FetchMode.preview
            fa.preview_chunks = preview_chunks
            return fa
        fa = _cached_article_result(cached, citation_header + text)
        fa.mode = FetchMode.preview
        fa.preview_chunks = preview_chunks
        return fa

    if mode == "range":
        start = range_start or 0
        end = range_end or min(start + config.max_context_length, len(cached.text))
        snippet = cached.text[start:end]
        header = (
            f"Character range [{start}:{end}] for DOI: {doi}\n"
            f"Source: {cached.source}\n"
            + "=" * 60 + "\n\n"
        )
        fa = _cached_article_result(cached, citation_header + header + snippet)
        fa.mode = FetchMode.range
        fa.range_chars = (start, end)
        return fa

    # mode == "full" (default)
    header = (
        f"Full text (cached) for DOI: {doi}\n"
        f"Source: {cached.source}\n"
        + "=" * 60 + "\n\n"
    )
    full = header + cached.text
    if len(full) > config.max_context_length:
        full = full[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
        fa = _cached_article_result(cached, citation_header + full, truncated=True)
        fa.mode = FetchMode.full
        return fa
    fa = _cached_article_result(cached, citation_header + full)
    fa.mode = FetchMode.full
    return fa


# ---------------------------------------------------------------------------
# PDF extraction helpers
# ---------------------------------------------------------------------------

def _format_extracted_pdf(
    pdf_source: "Path | bytes", doi: str, source: str, pages_str: str | None = None,
) -> FetchedArticle:
    """Extract text from a PDF file (Path preferred) and format as a tool response."""
    if pages_str:
        parts = pages_str.split("-")
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else start
        extracted_text = pdf_extractor.extract_text_by_pages(pdf_source, start, end)
        return FetchedArticle(
            doi=doi,
            text=(
                f"Extracted text from pages {pages_str} of DOI: {doi}\n"
                f"Source: {source}\n\n"
                f"{extracted_text}"
            ),
            source=source,
        )

    result = pdf_extractor.extract_text(pdf_source)

    header = (
        f"Full text extracted from DOI: {doi}\n"
        f"Source: {source}\n"
        f"Pages: {result['pages']}\n"
        f"Truncated: {result['truncated']}\n"
    )

    if result["metadata"].get("title"):
        header += f"PDF Title: {result['metadata']['title']}\n"

    if result["sections"]:
        header += "Sections: " + ", ".join(
            s["title"] for s in result["sections"][:15]
        ) + "\n"

    header += "\n" + "=" * 60 + "\n\n"

    return FetchedArticle(
        doi=doi,
        text=header + result["text"],
        source=source,
        sections=result.get("sections") or [],
        section_detection="pdf_font_analysis",
        word_count=len(result["text"].split()),
        truncated=bool(result.get("truncated")),
    )


def _cache_pdf_and_return(
    pdf_source: "Path | bytes",
    doi: str,
    source: str,
    pages_str: str | None,
    mode: str,
    section_name: str | None,
    range_start: int | None,
    range_end: int | None,
) -> FetchedArticle:
    """Extract PDF text, write to article cache, apply mode filter, and return.

    When *pages_str* is set we return a partial extraction and skip caching
    (partial text is not useful for section-based access).
    """
    def _append_import_hint(fa: FetchedArticle) -> FetchedArticle:
        hint = zotero_import.get_auto_import_hint(doi)
        if not hint:
            return fa
        fa.auto_import_status = hint
        if hint not in fa.text:
            fa.text = fa.text + "\n\n" + hint
        return fa

    if pages_str:
        return _format_extracted_pdf(pdf_source, doi, source, pages_str)

    if config.use_pymupdf4llm:
        result = pdf_extractor.extract_text_pymupdf4llm(pdf_source)
    else:
        result = pdf_extractor.extract_text_with_sections(pdf_source)
    raw_text = result["text"]

    # Build metadata from PDF extraction result
    pdf_meta: dict = {}
    if result["metadata"]:
        pdf_meta["title"] = result["metadata"].get("title", "")
        # Authors may be in the PDF metadata
        if result["metadata"].get("author"):
            pdf_meta["authors"] = result["metadata"]["author"].split(",") if isinstance(result["metadata"]["author"], str) else result["metadata"]["author"]

    cached_article = text_cache.put_cached(
        doi, raw_text, source,
        sections=result["sections"],
        section_detection=result.get("section_detection", "pdf_font_analysis"),
        word_count=len(raw_text.split()),
        metadata=pdf_meta,
    )

    # Queue for background Zotero import (non-blocking; only when we have a file)
    if isinstance(pdf_source, Path):
        zotero_import.enqueue_zotero_import(doi, pdf_source, cached_article)
        # Surface startup/probe issues immediately on the same response.
        hint = zotero_import.get_auto_import_hint(doi)
        if hint:
            logger.warning("Auto-import warning for %s: %s", doi, hint)

    if mode != "full":
        _pdf_path_for_mode = str(pdf_source) if isinstance(pdf_source, Path) else None
        fa = _apply_mode_filter(cached_article, mode, section_name, range_start, range_end)
        fa.pdf_path = fa.pdf_path or _pdf_path_for_mode
        fa.cache_key = fa.cache_key or text_cache._cache_key(doi)
        return _append_import_hint(fa)

    # mode == "full" — format exactly as _format_extracted_pdf does
    header = (
        f"Full text extracted from DOI: {doi}\n"
        f"Source: {source}\n"
        f"Pages: {result['pages']}\n"
        f"Truncated: {result['truncated']}\n"
    )
    if result["metadata"].get("title"):
        header += f"PDF Title: {result['metadata']['title']}\n"
    if result["sections"]:
        header += "Sections: " + ", ".join(
            s["title"] for s in result["sections"][:15]
        ) + "\n"
    header += "\n" + "=" * 60 + "\n\n"

    # Add citation header for full mode
    citation_header = _format_citation_header(doi, pdf_meta)
    full_text = header + raw_text
    if len(full_text) > config.max_context_length:
        full_text = full_text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
    _pdf_path = str(pdf_source) if isinstance(pdf_source, Path) else None
    fa = FetchedArticle(
        doi=doi,
        text=citation_header + full_text,
        source=source,
        sections=cached_article.sections or [],
        section_detection=cached_article.section_detection or "pdf_font_analysis",
        word_count=cached_article.word_count or 0,
        metadata=pdf_meta,
        truncated=bool(result.get("truncated")),
        pdf_path=_pdf_path,
        cache_key=text_cache._cache_key(doi),
    )
    return _append_import_hint(fa)


# ---------------------------------------------------------------------------
# Main fetch pipeline
# ---------------------------------------------------------------------------

async def fetch_article(args: dict) -> FetchedArticle:
    """Full article retrieval pipeline.

    Tries (in order): article cache, Zotero, URL tier, SSRN remapping,
    external APIs (S2/OA/Unpaywall), direct HTTP, CORE.ac.uk, web search,
    DOI landing page, proxied fetch, stealth browser, HeinOnline, SSRN cookies.
    """
    zotero_key = (args.get("zotero_key") or "").strip() or None
    doi = args.get("doi")
    url = (args.get("url") or "").strip() or None
    if not doi and not zotero_key and not url:
        _msg = "fetch_fulltext requires at least one of 'doi', 'zotero_key', or 'url'."
        return FetchedArticle(doi="", text=_msg, error=_msg)
    if zotero_key and not doi:
        # Synthesize a stable cache key so the article cache works uniformly.
        doi = f"zotero:{zotero_key}"
    if not doi and url:
        # URL-only: synthesize a stable cache key from the URL hash.
        _url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        doi = f"url:{_url_hash}"
    use_proxy = args.get("use_proxy", False)
    pages_str = args.get("pages")
    mode = args.get("mode", "sections")
    section_name = args.get("section")
    range_start = args.get("range_start")
    range_end = args.get("range_end")
    force_html = args.get("source", "auto") == "html"

    # Initialize variables for failure message
    html = None
    extraction = None

    # ── Cache read: fastest path — skip all network/PDF work ────────
    # Bypassed when source='html' so the caller can force a fresh scrape.
    cached_article = text_cache.get_cached(doi) if not force_html else None
    if cached_article:
        logger.debug("Article cache hit for %s", doi)
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
                    new_result = pdf_extractor.extract_text_pymupdf4llm(pdf_path)
                    cached_article = text_cache.put_cached(
                        cached_article.doi,
                        new_result["text"],
                        cached_article.source,
                        sections=new_result["sections"],
                        section_detection=new_result.get("section_detection", "pymupdf4llm_markdown"),
                        word_count=len(new_result["text"].split()),
                        metadata=cached_article.metadata,
                    )
                    logger.info(
                        "Re-extracted %s with pymupdf4llm: %d → %d sections",
                        doi, len(cached_article.sections or []), len(new_result["sections"]),
                    )
                except Exception as exc:
                    logger.warning("pymupdf4llm re-extraction failed for %s: %s", doi, exc)

        return _apply_mode_filter(
            cached_article, mode, section_name, range_start, range_end
        )

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
                if zotero_key:
                    zot_result = await zotero.get_paper_from_zotero_by_key(zotero_key)
                else:
                    zot_result = await zotero.get_paper_from_zotero(doi)
                if zot_result and zot_result.get("found"):
                    # Got fulltext directly (already extracted by Zotero — best case!)
                    if zot_result.get("text"):
                        raw_text = zot_result["text"]
                        sections = content_extractor.detect_sections_from_text(raw_text)
                        # Extract metadata from Zotero item if available
                        zot_meta: dict = {}
                        if zot_result.get("item"):
                            item = zot_result["item"]
                            zot_meta["title"] = item.get("title", "")
                            zot_meta["year"] = str(item.get("date", ""))[:4] if item.get("date") else ""
                            zot_meta["venue"] = item.get("publicationTitle", "")
                            creators = item.get("creators", [])
                            if creators:
                                zot_meta["authors"] = [c.get("lastName", "") or c.get("firstName", "") for c in creators]
                        cached_article = text_cache.put_cached(
                            doi, raw_text, zot_result["source"],
                            sections=sections,
                            section_detection="text_heuristic",
                            word_count=len(raw_text.split()),
                            metadata=zot_meta,
                        )
                        if mode != "full":
                            result = _apply_mode_filter(
                                cached_article, mode, section_name, range_start, range_end
                            )
                        else:
                            header = f"Full text from Zotero (already indexed) for DOI: {doi}\n"
                            header += f"Source: {zot_result['source']}\n"
                            if zot_result.get("indexed_pages") and zot_result.get("total_pages"):
                                header += f"Pages indexed: {zot_result['indexed_pages']}/{zot_result['total_pages']}\n"
                            if zot_result.get("truncated"):
                                header += (
                                    "⚠ WARNING: Text is TRUNCATED — Zotero only indexed "
                                    f"{zot_result.get('indexed_pages', '?')}/{zot_result.get('total_pages', '?')} pages. "
                                    "Increase in Zotero > Settings > Search > PDF Indexing, reindex, and sync.\n"
                                    "Alternatively, use fetch_fulltext with use_proxy=true to get the full PDF.\n"
                                )
                            header += "=" * 60 + "\n\n"
                            text = header + raw_text
                            if len(text) > config.max_context_length:
                                text = text[:config.max_context_length] + "\n\n[... TRUNCATED ...]"
                            result = FetchedArticle(
                                doi=doi,
                                text=text,
                                source=zot_result["source"],
                                sections=cached_article.sections or [],
                                section_detection=cached_article.section_detection or "text_heuristic",
                                word_count=cached_article.word_count or 0,
                                metadata=zot_meta,
                                truncated=bool(zot_result.get("truncated")),
                            )

                        # Add citation header for full mode
                        if result is not None and mode == "full":
                            citation_header = _format_citation_header(doi, zot_meta)
                            result.text = citation_header + result.text

                    # Got PDF path from Zotero — extract text from disk
                    elif zot_result.get("pdf_path"):
                        result = _cache_pdf_and_return(
                            zot_result["pdf_path"], doi, zot_result["source"],
                            pages_str, mode, section_name, range_start, range_end,
                        )

                # For Zotero-only (no real DOI), the rest of the pipeline has
                # nothing to work with unless the Zotero item has a `url` field.
                # Check for that first; if found, promote it so the URL tier
                # (below) can handle it.
                if result is None and zotero_key:
                    if url is None:
                        # Try to read the `url` field from the Zotero item.
                        try:
                            _zot_item = await zotero_sqlite.search_by_key(zotero_key)
                            if _zot_item and (_zot_item.url or "").strip():
                                url = _zot_item.url.strip()
                                _url_hash2 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                                doi = f"url:{_url_hash2}"
                                logger.info(
                                    "zotero_key %s has no attachment but has url=%s; "
                                    "delegating to URL tier.",
                                    zotero_key, url,
                                )
                        except Exception as _e:
                            logger.debug("Failed to look up Zotero url for %s: %s", zotero_key, _e)

                    if result is None and url is None:
                        zot_source = (zot_result or {}).get("source") or "not found"
                        meta = (zot_result or {}).get("metadata") or {}
                        title = meta.get("title") or "(unknown title)"
                        _msg = (
                            f"Zotero item {zotero_key} ({title}) has no indexed fulltext "
                            f"or retrievable PDF (source: {zot_source}). No DOI is available "
                            "to try open-access or proxy fallbacks. "
                            "In Zotero, check that the item has a PDF attachment and that "
                            "PDF indexing has run (Settings > Search)."
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

                        if _url_pdf_path:
                            result = _cache_pdf_and_return(
                                _url_pdf_path, doi, "url_direct_pdf",
                                pages_str, mode, section_name, range_start, range_end,
                            )

                        # Landing-page path: stealth browser + trafilatura pipeline.
                        if result is None and config.use_stealth_browser:
                            try:
                                _lp = await _extract_from_landing_page(url, use_proxy)
                                if _lp:
                                    if _lp["pdf_path"]:
                                        result = _cache_pdf_and_return(
                                            _lp["pdf_path"], doi, _lp["source"],
                                            pages_str, mode, section_name, range_start, range_end,
                                        )
                                    elif _lp["text"]:
                                        _raw = _lp["text"]
                                        _cached_lp = text_cache.put_cached(
                                            doi, _raw, _lp["source"],
                                            sections=_lp["sections"],
                                            section_detection=_lp["section_detection"],
                                            word_count=_lp["word_count"],
                                            metadata={"url": url},
                                        )
                                        if mode != "full":
                                            result = _apply_mode_filter(
                                                _cached_lp, mode, section_name, range_start, range_end
                                            )
                                        else:
                                            _text = (
                                                f"Full text extracted from URL: {url}\n"
                                                f"Source: {_lp['source']}\n"
                                                f"Word count: {_lp['word_count']}\n"
                                                f"{'=' * 60}\n\n" + _raw
                                            )
                                            if len(_text) > config.max_context_length:
                                                _text = (
                                                    _text[:config.max_context_length]
                                                    + "\n\n[... TRUNCATED — full text exceeds context limit ...]"
                                                )
                                            result = FetchedArticle(
                                                doi=doi,
                                                text=_text,
                                                source=_lp["source"],
                                                sections=_cached_lp.sections or [],
                                                section_detection=_cached_lp.section_detection or "unknown",
                                                word_count=_cached_lp.word_count or 0,
                                                metadata={"url": url},
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
                    if doi.startswith("10.2139/ssrn.") and not args.get("_original_ssrn_doi"):
                        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as _c:
                            _ssrn_remap = await apis.resolve_ssrn_doi(doi, _c)

                        # Try OA PDF URLs discovered by remap
                        for _oa_url in (_ssrn_remap or {}).get("oa_pdf_urls", []):
                            _oa_path = await pdf_fetcher.fetch_direct(_oa_url)
                            if _oa_path:
                                result = _cache_pdf_and_return(
                                    _oa_path, doi, "ssrn_remap_oa",
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                                if result:
                                    break

                        # If a published DOI was found, re-enter pipeline with it
                        if result is None and (_ssrn_remap or {}).get("published_doi"):
                            _pub_doi = _ssrn_remap["published_doi"]
                            logger.info("SSRN %s → published %s", doi, _pub_doi)
                            result = await fetch_article({
                                **args,
                                "doi": _pub_doi,
                                "_original_ssrn_doi": doi,
                            })

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
                                result = await fetch_article({
                                    **args,
                                    "doi": _title_remap["published_doi"],
                                    "_original_ssrn_doi": doi,
                                })

                    # ── Step 1: Gather candidate PDF URLs from external APIs ────────
                    s2_paper = None
                    oa_paper = None
                    unpaywall_data = None

                    try:
                        s2_paper = await apis.s2_paper(f"DOI:{doi}")
                    except Exception:
                        pass
                    try:
                        oa_paper = await apis.openalex_work(doi)
                    except Exception:
                        pass
                    try:
                        unpaywall_data = await apis.unpaywall_lookup(doi)
                    except Exception:
                        pass

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

                    # ── Step 2: Direct HTTP on candidate URLs (fast; handles arXiv/OA) ──
                    config.evict_cache_lru()
                    for candidate in candidate_urls:
                        url = candidate["url"]
                        source = candidate["source"]
                        pdf_cached = pdf_fetcher._cache_path(url)
                        if pdf_cached.exists() and pdf_fetcher._is_pdf_file(pdf_cached):
                            logger.info("Cache hit for %s", url)
                            result = _cache_pdf_and_return(
                                pdf_cached, doi, f"{source} (cached)",
                                pages_str, mode, section_name, range_start, range_end,
                            )
                            if result:
                                break
                        path = await pdf_fetcher.fetch_direct(url)
                        if path:
                            result = _cache_pdf_and_return(
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
                                        result = _cache_pdf_and_return(
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
                                            result = _cache_pdf_and_return(
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
                                    result = _cache_pdf_and_return(
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
                                result = _cache_pdf_and_return(
                                    lp["pdf_path"], doi, lp["source"],
                                    pages_str, mode, section_name, range_start, range_end,
                                )
                            elif lp["text"]:
                                raw_text = lp["text"]
                                cached_article = text_cache.put_cached(
                                    doi, raw_text, lp["source"],
                                    sections=lp["sections"],
                                    section_detection=lp["section_detection"],
                                    word_count=lp["word_count"],
                                    metadata=cite_meta,
                                )
                                if mode != "full":
                                    result = _apply_mode_filter(
                                        cached_article, mode, section_name, range_start, range_end
                                    )
                                else:
                                    text = (
                                        f"Full text extracted from DOI: {doi}\n"
                                        f"Source: {lp['source']}\n"
                                        f"Word count: {lp['word_count']}\n"
                                        f"{'=' * 60}\n\n" + raw_text
                                    )
                                    if len(text) > config.max_context_length:
                                        text = (
                                            text[:config.max_context_length]
                                            + "\n\n[... TRUNCATED — full text exceeds context limit ...]"
                                        )
                                    citation_header = _format_citation_header(doi, cite_meta)
                                    result = FetchedArticle(
                                        doi=doi,
                                        text=citation_header + text,
                                        source=lp["source"],
                                        sections=cached_article.sections or [],
                                        section_detection=cached_article.section_detection or "unknown",
                                        word_count=cached_article.word_count or 0,
                                        metadata=cite_meta,
                                    )

                    if force_html:
                        doi_url = (
                            f"https://doi.org/{doi}" if not doi.startswith("http") else doi
                        )
                        scrapling_path, html, final_url = await pdf_fetcher.fetch_with_scrapling(
                            doi_url
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
                                    if html_is_good:
                                        cached_article = text_cache.put_cached(
                                            doi, raw_text, html_source,
                                            sections=sections,
                                            section_detection=section_det,
                                            word_count=html_words,
                                            metadata=cite_meta,
                                        )
                                    else:
                                        # HTML exists but isn't good enough to replace cache
                                        cached_article = text_cache.get_cached(doi) or text_cache.put_cached(
                                            doi, raw_text, html_source,
                                            sections=sections,
                                            section_detection=section_det,
                                            word_count=html_words,
                                            metadata=cite_meta,
                                        )
                                    if mode != "full":
                                        result = _apply_mode_filter(
                                            cached_article, mode, section_name, range_start, range_end
                                        )
                                    else:
                                        text = (
                                            f"Full text extracted from DOI: {doi}\n"
                                            f"Source: {html_source}\n"
                                            f"Word count: {extraction['word_count']}\n"
                                            f"{'=' * 60}\n\n" + raw_text
                                        )
                                        if len(text) > config.max_context_length:
                                            text = (
                                                text[:config.max_context_length]
                                                + "\n\n[... TRUNCATED — full text exceeds context limit ...]"
                                            )
                                        citation_header = _format_citation_header(doi, cite_meta)
                                        result = FetchedArticle(
                                            doi=doi,
                                            text=citation_header + text,
                                            source=html_source,
                                            sections=cached_article.sections or [],
                                            section_detection=cached_article.section_detection or "unknown",
                                            word_count=cached_article.word_count or 0,
                                            metadata=cite_meta,
                                        )

                    # ── Step 4: Proxied fetch on candidates (institutional access) ───
                    if result is None and use_proxy and config.gost_proxy_url:
                        for candidate in candidate_urls:
                            path = await pdf_fetcher.fetch_proxied(candidate["url"])
                            if path:
                                result = _cache_pdf_and_return(
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
                                result = _cache_pdf_and_return(
                                    path, doi, "doi_redirect (proxied)",
                                    pages_str, mode, section_name, range_start, range_end,
                                )

                    # ── Step 5: Scrapling on candidate URLs (last resort) ────────────
                    if result is None and config.use_stealth_browser:
                        for candidate in candidate_urls:
                            scrap_path, _html, _scrap_url = await pdf_fetcher.fetch_with_scrapling(
                                candidate["url"]
                            )
                            if scrap_path:
                                result = _cache_pdf_and_return(
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
                                result = _cache_pdf_and_return(
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
                                    result = _cache_pdf_and_return(
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
                                    _cached_art = text_cache.put_cached(
                                        doi, _raw, "ssrn_cookies_html",
                                        sections=_secs,
                                        section_detection=_ssrn_extraction.get("section_detection", "text_heuristic"),
                                        word_count=_ssrn_extraction["word_count"],
                                        metadata=cite_meta,
                                    )
                                    result = _apply_mode_filter(
                                        _cached_art, mode, section_name, range_start, range_end
                                    )

                    # ── Failure ──────────────────────────────────────────────────────
                    if result is None:
                        _original_doi = args.get("_original_ssrn_doi") or doi
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
                            lines = [
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
                        else:
                            sources_tried = [c["source"] for c in candidate_urls]
                            doi_url = f"https://doi.org/{doi}"
                            lines = [
                                f"Could not retrieve full text for DOI: {doi}\n",
                                f"Sources tried: {', '.join(sources_tried) or 'none found'}\n",
                            ]

                            if not use_proxy and config.gost_proxy_url:
                                lines.append(
                                    "\n→ Try with institutional proxy: "
                                    f"fetch_fulltext(doi=\"{doi}\", use_proxy=true)\n"
                                )
                            elif not config.gost_proxy_url:
                                lines.append(
                                    "\n→ No institutional proxy configured. If you have institutional access, "
                                    "configure GOST_PROXY_URL in .env.\n"
                                )

                            lines.append(f"→ Check available URLs: find_pdf_urls(doi=\"{doi}\")\n")
                            lines.append(f"→ Verify metadata: get_paper(identifier=\"{doi}\")\n")
                            lines.append(
                                f"\n**Ask the user to:**\n"
                                f"1. Open {doi_url} in their browser and download the PDF\n"
                                f"2. Save it to Zotero, or attach the PDF to this conversation\n"
                                f"\nOnce available, re-run this request."
                            )

                            if html and not extraction:
                                lines.append(
                                    "\n\nNote: The publisher page was reached but the full article text was not "
                                    "available — likely behind a paywall. Only the abstract could be accessed.\n"
                                )

                        result = FetchedArticle(doi=doi, text="".join(lines), source="", error="".join(lines))
    finally:
        # Cleanup: remove the lock if nobody else is waiting on it
        async with _doi_locks_lock:
            if doi in _doi_locks and not _doi_locks[doi].locked():
                del _doi_locks[doi]

    return result
