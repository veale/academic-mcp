"""HTML article extraction using trafilatura.

Used as a fast path when a stealth browser retrieves a publisher landing page
that contains the full article body in HTML — avoiding the need to locate,
download, and parse a PDF.
"""

import asyncio
import logging
import math
import re
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)

try:
    import trafilatura
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    trafilatura = None  # type: ignore[assignment]
    _TRAFILATURA_AVAILABLE = False
    logger.warning(
        "trafilatura not installed — HTML article extraction disabled. "
        "Install with: uv add trafilatura"
    )

# Minimum word count to consider an extraction a full article.
# Pages that return only an abstract or a login gate are well under this.
_MIN_WORD_COUNT = 1500

# CSS selectors for the main article body container on known publisher sites.
# Applied before passing HTML to trafilatura to strip navigation, cookies
# banners, and login modals that confuse the extraction algorithm.
PUBLISHER_SELECTORS: dict[str, str] = {
    "onlinelibrary.wiley.com": "div.article__body",
    "sciencedirect.com": "div#body",
    "link.springer.com": "article.c-article-body",
    "academic.oup.com": "div.article-body",
    "tandfonline.com": "div.NLM_sec_level_1",
    "journals.sagepub.com": "div.body",
    "cambridge.org": "div.article-body",
}

# Marker delimiter — chosen to be invisible in real academic text.
_MARKER_DELIM = "§§"
_MARKER_RE = re.compile(r"§§SEC:(\d+):(\d+):(.+?)§§", re.DOTALL)


def extract_citation_meta(html: str, base_url: str) -> dict[str, str]:
    """Extract Google Scholar ``citation_*`` meta tags from HTML.

    Publishers that want Google Scholar indexing (essentially all of them)
    embed machine-readable metadata in their page ``<head>``.  The
    ``citation_pdf_url`` tag gives the publisher's own canonical PDF URL —
    the same link Google Scholar uses.  This is often more stable than what
    Unpaywall/OpenAlex report because it comes directly from the page just
    fetched.

    Also extracts ``DC.identifier`` tags whose content ends in ``.pdf``.

    Returns a dict of lower-cased tag names to resolved absolute URLs / values,
    e.g.::

        {
            "citation_pdf_url": "https://...",
            "citation_title":   "...",
            "citation_doi":     "10.xxxx/...",
        }

    The ``citation_pdf_url`` value (if present) is resolved to an absolute URL
    relative to ``base_url``.
    """
    result: dict[str, str] = {}

    # Match <meta name="citation_*" content="..."> (and name='...' variants)
    for m in re.finditer(
        r'<meta\s[^>]*name=["\']([^"\']+)["\'][^>]*content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    ):
        name = m.group(1).lower().strip()
        value = m.group(2).strip()
        if name.startswith("citation_") or name.startswith("dc."):
            result[name] = value

    # Also try content=... before name=... ordering
    for m in re.finditer(
        r'<meta\s[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    ):
        value = m.group(1).strip()
        name = m.group(2).lower().strip()
        if name not in result and (
            name.startswith("citation_") or name.startswith("dc.")
        ):
            result[name] = value

    # DC.identifier with .pdf content (some repositories use this instead)
    for m in re.finditer(
        r'<meta\s[^>]*name=["\']DC\.identifier["\'][^>]*content=["\']([^"\']+\.pdf[^"\']*)["\']',
        html,
        re.IGNORECASE,
    ):
        if "citation_pdf_url" not in result:
            result["citation_pdf_url"] = m.group(1).strip()

    # Resolve citation_pdf_url to absolute URL
    if "citation_pdf_url" in result:
        raw = result["citation_pdf_url"]
        if raw.startswith("//"):
            parsed = urlparse(base_url)
            result["citation_pdf_url"] = f"{parsed.scheme}:{raw}"
        elif raw.startswith("/") or not raw.startswith("http"):
            result["citation_pdf_url"] = urljoin(base_url, raw)

    return result


def _pre_filter_html(html: str, url: str) -> str:
    """Narrow HTML to the article body for known publisher domains.

    If the URL matches a key in ``PUBLISHER_SELECTORS``, extracts just the
    content inside that element and returns it as an HTML string. Falls back
    to the original HTML if the domain is unknown or the selector finds
    nothing.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lstrip("www.")

    selector = None
    for pub_domain, css_sel in PUBLISHER_SELECTORS.items():
        if pub_domain in domain:
            selector = css_sel
            break

    if not selector:
        return html

    try:
        import lxml.html
        import lxml.etree as etree

        doc = lxml.html.fromstring(html)
        elements = doc.cssselect(selector)
        if elements:
            return etree.tostring(elements[0], encoding="unicode", method="html")
    except Exception as exc:
        logger.debug("Pre-filter HTML failed for %s (%s): %s", url, selector, exc)

    return html


def _extract_with_sections_sync(html: str, url: str) -> dict | None:
    """Synchronous marker-based extraction — call via ``asyncio.to_thread``.

    Strategy:
    1. Pre-filter HTML to the article body container.
    2. Inject unique ``§§SEC:id:level:title§§`` markers immediately before
       every ``<h2>`` / ``<h3>`` tag.  trafilatura preserves inline text
       content adjacent to heading elements, so the markers survive extraction.
    3. Run trafilatura on the marked HTML.
    4. Parse the markers out of the output to build the section index with
       exact character offsets — no fuzzy string matching required.

    Graceful degradation: if trafilatura drops a marker (possible for
    heavily JavaScript-rendered pages), that section boundary is silently
    omitted.  If all markers are dropped, ``sections`` is an empty list and
    the caller falls back to ``detect_sections_from_text``.
    """
    if not _TRAFILATURA_AVAILABLE:
        return None

    filtered_html = _pre_filter_html(html, url)

    # ── Inject markers ────────────────────────────────────────────────
    counter = [0]

    def _inject(m: re.Match) -> str:
        tag = m.group(1)          # "h2" or "h3"
        inner_html = m.group(2)   # raw inner HTML of the heading element
        level = 2 if tag.lower() == "h2" else 3
        # Strip nested tags to get plain title text
        title = re.sub(r"<[^>]+>", "", inner_html).strip()
        # Remove delimiter characters to prevent broken marker regex
        title = title.replace(_MARKER_DELIM, "").strip()
        if not title:
            return m.group(0)  # skip empty/icon-only headings
        mid = counter[0]
        counter[0] += 1
        marker = f"{_MARKER_DELIM}SEC:{mid}:{level}:{title}{_MARKER_DELIM}"
        # Prepend marker as a standalone line so trafilatura sees it as a
        # distinct text node rather than run-on text inside the heading.
        return f"\n{marker}\n" + m.group(0)

    marked_html = re.sub(
        r"<(h[23])\b[^>]*>(.*?)</\1>",
        _inject,
        filtered_html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # ── Run trafilatura ───────────────────────────────────────────────
    raw_text = trafilatura.extract(
        marked_html,
        include_tables=True,
        include_comments=False,
        include_links=False,
        favor_precision=True,
        url=url,
    )

    if not raw_text:
        return None

    # ── Parse markers out of the extracted text ───────────────────────
    sections: list[dict] = []
    clean_parts: list[str] = []
    cursor = 0

    for m in _MARKER_RE.finditer(raw_text):
        clean_parts.append(raw_text[cursor : m.start()])
        sections.append(
            {
                "title": m.group(3).strip(),
                "level": int(m.group(2)),
                "start": sum(len(p) for p in clean_parts),
            }
        )
        cursor = m.end()

    clean_parts.append(raw_text[cursor:])
    clean_text = "".join(clean_parts)

    word_count = len(clean_text.split())
    if word_count < _MIN_WORD_COUNT:
        logger.debug(
            "HTML extraction for %s returned only %d words (threshold %d) — "
            "likely abstract-only or paywalled page",
            url, word_count, _MIN_WORD_COUNT,
        )
        return None

    # Compute section end offsets and word counts
    for i, sec in enumerate(sections):
        sec["end"] = sections[i + 1]["start"] if i + 1 < len(sections) else len(clean_text)
        sec["word_count"] = len(clean_text[sec["start"] : sec["end"]].split())

    # If no markers survived trafilatura, sections is empty — caller should
    # fall back to detect_sections_from_text for a best-effort index.
    section_detection = "html_headings" if sections else "text_heuristic"

    domain = urlparse(url).netloc.lstrip("www.")
    return {
        "text": clean_text,
        "word_count": word_count,
        "source": domain,
        "sections": sections,
        "section_detection": section_detection,
    }


async def extract_article_with_sections(html: str | None, url: str) -> dict | None:
    """Extract full article text from HTML with section boundaries.

    Runs the CPU-bound work in a worker thread.

    Returns on success::

        {
            "text": str,
            "word_count": int,
            "source": str,
            "sections": list[dict],      # [{title, level, start, end, word_count}]
            "section_detection": str,    # "html_headings" or "text_heuristic"
        }

    Returns ``None`` if trafilatura is unavailable, *html* is empty/None,
    extraction produces nothing, or the result is under 1500 words.

    If no ``<h2>`` / ``<h3>`` markers survive trafilatura, ``sections`` will
    be empty and ``section_detection`` will be ``"text_heuristic"`` — the
    caller should call :func:`detect_sections_from_text` and substitute those
    results.
    """
    if not _TRAFILATURA_AVAILABLE or not html:
        return None
    return await asyncio.to_thread(_extract_with_sections_sync, html, url)


# ---------------------------------------------------------------------------
# Shared TF-IDF utilities
# ---------------------------------------------------------------------------

_STOP_WORDS: frozenset = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "not",
    "no", "nor", "so", "yet", "both", "either", "neither", "each", "every",
    "all", "any", "both", "few", "more", "most", "other", "some", "such",
    "than", "too", "very", "just", "that", "this", "these", "those", "it",
    "its", "we", "they", "their", "there", "our", "he", "she", "his", "her",
    "you", "your", "my", "me", "him", "them", "us", "who", "which", "what",
    "when", "where", "how", "why", "if", "then", "because", "while",
    "although", "however", "also", "thus", "hence", "therefore", "while",
    "through", "between", "about", "against", "during", "before", "after",
    "above", "below", "under", "over", "into", "out", "up", "down",
    "i", "s", "t", "re", "ve", "d", "ll", "m",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase-split on non-word chars; drop stop words and short tokens."""
    tokens = re.split(r"[^\w'-]+", text.lower())
    return [
        t.strip("'-")
        for t in tokens
        if len(t.strip("'-")) >= 3 and t.strip("'-") not in _STOP_WORDS
    ]


def _tfidf_keywords(docs: list[list[str]], top_k: int = 5) -> list[list[str]]:
    """Return top-k TF-IDF keywords for each document (list of token lists).

    IDF is computed across all documents in *docs*.  Smoothed IDF:
    ``log((n+1)/(df+1)) + 1``.  Returns one keyword list per input document,
    in the same order.
    """
    n = len(docs)
    if n == 0:
        return []

    # Document frequency per token
    token_df: dict[str, int] = {}
    for doc in docs:
        for tok in set(doc):
            token_df[tok] = token_df.get(tok, 0) + 1

    def _idf(tok: str) -> float:
        return math.log((n + 1) / (token_df.get(tok, 1) + 1)) + 1.0

    results: list[list[str]] = []
    for doc in docs:
        if not doc:
            results.append([])
            continue
        total = len(doc)
        tf: dict[str, float] = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1 / total
        tfidf = {tok: tf[tok] * _idf(tok) for tok in tf}
        results.append(sorted(tfidf, key=lambda t: tfidf[t], reverse=True)[:top_k])
    return results


def generate_keyword_skeleton(text: str, n_chunks: int = 20) -> list[dict]:
    """Build a navigational map of *text* when structural headings are absent.

    Splits the text into *n_chunks* equal-sized character windows, then uses
    TF-IDF to find the 5 most *distinctive* tokens in each chunk — words that
    are frequent in that chunk but rare across the document.

    No external dependencies: uses only ``collections.Counter``, ``math.log``,
    and a small hardcoded English stop-word set.

    Returns::

        [
            {
                "chunk": 1,        # 1-indexed
                "start": 0,
                "end": 4700,
                "word_count": 750,
                "keywords": ["cloud", "platform", "asset-light", ...],
            },
            ...
        ]
    """
    if not text:
        return []

    # Snap chunk boundaries to the nearest whitespace to avoid mid-word splits
    chunk_len = max(len(text) // n_chunks, 200)
    boundaries: list[int] = [0]
    for i in range(1, n_chunks):
        pos = i * chunk_len
        # Walk forward to the next whitespace
        while pos < len(text) and not text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        boundaries.append(pos)
    boundaries.append(len(text))

    # Tokenize all chunks
    raw_chunks: list[dict] = []
    for idx in range(len(boundaries) - 1):
        start = boundaries[idx]
        end = boundaries[idx + 1]
        chunk_text = text[start:end]
        raw_chunks.append({
            "chunk": idx + 1,
            "start": start,
            "end": end,
            "word_count": len(chunk_text.split()),
            "tokens": _tokenize(chunk_text),
        })

    if not raw_chunks:
        return []

    keyword_lists = _tfidf_keywords([c["tokens"] for c in raw_chunks], top_k=5)

    total_chunks = len(raw_chunks)
    results: list[dict] = []
    for chunk, kw in zip(raw_chunks, keyword_lists):
        title = f"[{chunk['chunk']}/{total_chunks}] " + (", ".join(kw) if kw else "—")
        results.append({
            "chunk": chunk["chunk"],
            "start": chunk["start"],
            "end": chunk["end"],
            "word_count": chunk["word_count"],
            "keywords": kw,
            "title": title,
            "level": 2,
        })

    return results


def keywords_for_sections(
    text: str, sections: list[dict], top_k: int = 5
) -> list[list[str]]:
    """Return top-k TF-IDF keywords for each section's text range.

    Each section's slice of *text* is treated as a document; IDF is computed
    across all sections so words common to every section get low weight and
    words distinctive to one section get high weight.

    Returns a list of keyword lists in the same order as *sections*.
    An empty list is returned for sections with no usable text.
    """
    if not text or not sections:
        return [[] for _ in sections]

    docs = [_tokenize(text[sec.get("start", 0): sec.get("end", len(text))]) for sec in sections]
    return _tfidf_keywords(docs, top_k=top_k)


def infill_keyword_chunks(
    text: str,
    sections: list[dict],
    gap_threshold: int = 4000,
    chunk_target: int = 3500,
    top_k: int = 5,
) -> list[dict]:
    """Insert keyword chunks into large gaps between structural sections.

    Scans for gaps larger than *gap_threshold* before the first section,
    between consecutive sections, and after the last section.  Each large
    gap is split into ~*chunk_target*-character pieces snapped to the
    nearest whitespace, and TF-IDF keywords are computed using only the
    chunks within that gap as the corpus — giving locally distinctive
    keywords rather than article-wide ones.

    Returns a new list with the original sections and infill chunks
    interleaved in document order.  Infill chunks carry ``'_infill': True``
    and ``'level': 3``.  Original sections are returned unmodified.

    If *sections* is empty the entire text is treated as one gap and a
    full keyword skeleton is returned (equivalent to
    :func:`generate_keyword_skeleton`).
    """
    if not text:
        return list(sections)

    if not sections:
        return generate_keyword_skeleton(text)

    text_len = len(text)

    # Build the list of gaps: (gap_start, gap_end)
    # Before first section, between sections, after last section.
    boundaries = [0] + [
        offset
        for sec in sections
        for offset in (sec.get("start", 0), sec.get("end", text_len))
    ] + [text_len]
    # Pairs: (boundaries[0], boundaries[1]), (boundaries[2], boundaries[3]), ...
    # i.e. pre-section gap, then each inter-section gap, then post-section gap
    gap_pairs: list[tuple[int, int]] = []
    gap_pairs.append((boundaries[0], sections[0].get("start", 0)))
    for i in range(len(sections) - 1):
        gap_pairs.append((sections[i].get("end", text_len), sections[i + 1].get("start", text_len)))
    gap_pairs.append((sections[-1].get("end", text_len), text_len))

    infill: list[dict] = []

    for gap_start, gap_end in gap_pairs:
        gap_len = gap_end - gap_start
        if gap_len <= gap_threshold:
            continue

        # Split gap into ~chunk_target-char pieces, snap to whitespace
        n_chunks = max(1, round(gap_len / chunk_target))
        raw_chunk_size = gap_len // n_chunks
        chunk_boundaries: list[int] = [gap_start]
        for j in range(1, n_chunks):
            pos = gap_start + j * raw_chunk_size
            while pos < gap_end and not text[pos].isspace():
                pos += 1
            if pos >= gap_end:
                break
            chunk_boundaries.append(pos)
        chunk_boundaries.append(gap_end)

        # Tokenize each chunk for local IDF
        gap_chunks: list[dict] = []
        for k in range(len(chunk_boundaries) - 1):
            cs, ce = chunk_boundaries[k], chunk_boundaries[k + 1]
            gap_chunks.append({
                "start": cs,
                "end": ce,
                "tokens": _tokenize(text[cs:ce]),
                "word_count": len(text[cs:ce].split()),
            })

        if not gap_chunks:
            continue

        keyword_lists = _tfidf_keywords([c["tokens"] for c in gap_chunks], top_k=top_k)
        total_in_gap = len(gap_chunks)

        for chunk_num, (chunk, kw) in enumerate(zip(gap_chunks, keyword_lists), start=1):
            infill.append({
                "title": f"[{chunk_num}/{total_in_gap}]",
                "level": 3,
                "start": chunk["start"],
                "end": chunk["end"],
                "word_count": chunk["word_count"],
                "keywords": kw,
                "_infill": True,
            })

    if not infill:
        return list(sections)

    combined = list(sections) + infill
    combined.sort(key=lambda s: s.get("start", 0))
    return combined


def consolidate_tiny_sections(
    sections: list[dict],
    text: str,
    min_words: int = 50,
) -> list[dict]:
    """Merge sections that are too small to be real into their predecessors.

    Academic sections are almost always 100+ words.  Sections under
    *min_words* are artefacts — footnotes misidentified as headings,
    author-affiliation lines picked up by font analysis, or OCR noise.

    Strategy: walk sections in order; if a section has fewer than *min_words*
    words, absorb it into the previous section (extend the predecessor's
    ``end`` and recompute its ``word_count``).  If the tiny section is the
    first, absorb it into the next section instead.  Iterates until stable.

    Does NOT discard sections — only merges, so text coverage is preserved.
    """
    if len(sections) <= 1:
        return sections

    changed = True
    while changed:
        changed = False
        new_sections: list[dict] = []
        i = 0
        while i < len(sections):
            sec = sections[i]
            wc = sec.get("word_count", 0)
            if wc < min_words:
                changed = True
                if new_sections:
                    # Merge into predecessor
                    pred = new_sections[-1]
                    pred["end"] = sec["end"]
                    pred["word_count"] = len(
                        text[pred["start"]: pred["end"]].split()
                    )
                elif i + 1 < len(sections):
                    # First section is tiny — merge into successor
                    succ = sections[i + 1]
                    succ["start"] = sec["start"]
                    succ["word_count"] = len(
                        text[succ["start"]: succ["end"]].split()
                    )
                else:
                    # Only section, keep as-is
                    new_sections.append(sec)
            else:
                new_sections.append(sec)
            i += 1
        sections = new_sections

    return sections


def _majority_tiny(sections: list[dict], min_words: int = 50) -> bool:
    """Return True if >60% of sections are below *min_words*.

    When this is true, section detection has probably failed — it's matching
    footnotes or reference entries rather than real headings.  The caller
    should fall back to ``generate_keyword_skeleton`` instead.
    """
    if len(sections) < 3:
        return False
    tiny = sum(1 for s in sections if s.get("word_count", 0) < min_words)
    return tiny / len(sections) > 0.6


def detect_sections_from_text(text: str, is_ocr: bool = False) -> list[dict]:
    """Detect section headings in plain extracted text (conservative).

    Designed for Zotero ``.zotero-ft-cache`` text where no structural metadata
    is available.  Uses conservative patterns to minimise false positives from
    figure captions, table headers, footnotes, and running journal-name headers.

    Detection patterns (applied in order):
    - **Well-known section names** — ``"Conclusion"``, ``"References"`` etc.
      on isolated lines (zero false-positive risk)
    - **Roman numeral sections** — ``"I Introduction"``, ``"III Results"``
    - **Numbered sections** — ``"1. Introduction"``, ``"2.1 Methods"``
    - **ALL CAPS isolated lines** — ≤ 80 chars, ≤ 12 words, with surrounding
      blank/long-paragraph lines
    - **Common academic section names** — preceded by blank or long paragraph

    Post-collection filters:
    - **Footnote sequence filter** — discard entire numbered group (dotted or
      undotted) if the max integer in that group exceeds 30 (endnote sequences)
    - **OCR footnote filter** (when *is_ocr* is True) — discard dotted numbered
      groups when max integer exceeds 15 AND structural candidates exist
    - **Individual length filter** — numbered candidates whose body text is
      > 60 characters are footnotes, not section headings
    - **Running header deduplication** — normalise (strip trailing page numbers,
      lowercase) and discard any heading that appears 3+ times; for ALL CAPS
      headings use a threshold of 2+
    - **Page-number-suffixed ALL CAPS** — if the normalised form of an ALL CAPS
      candidate matches the normalised form of another (with a different page
      number suffix), discard both
    - **Size consolidation** — tiny artefact sections (< 50 words) are merged
      into their neighbours via :func:`consolidate_tiny_sections`
    - **Majority-tiny guard** — if >60% of sections are tiny after consolidation,
      returns ``[]`` so the caller can fall back to keyword_skeleton

    Returns ``[{"title": str, "level": int, "start": int, "end": int,
    "word_count": int}]``, or ``[]`` if detection quality is too poor.
    """
    lines = text.split("\n")
    n = len(lines)

    # Pre-compute cumulative byte offsets for each line start
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1  # +1 for the newline

    _COMMON_HEADINGS = {
        "abstract", "introduction", "methods", "methodology", "results",
        "discussion", "conclusion", "conclusions", "references",
        "acknowledgements", "acknowledgments", "background",
        "related work", "literature review", "materials and methods",
        "experimental", "findings",
    }

    # Well-known section names that should ALWAYS be detected on isolated lines
    # (case-insensitive).  Zero false-positive risk as standalone lines.
    _WELL_KNOWN = {
        "conclusion", "conclusions", "concluding remarks", "summary",
        "acknowledgments", "acknowledgements", "references", "bibliography",
        "appendix",
    }

    # Roman numeral pattern: line begins with I–XII followed by 1-2 spaces and
    # a capital letter.  The numeral must be the ENTIRE first token (prevents
    # "In this article…" or "I am…" from matching).
    _ROMAN_NUMERALS = frozenset({
        "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII",
    })

    candidates: list[dict] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        prev_blank = (i == 0) or (not lines[i - 1].strip())
        next_blank = (i == n - 1) or (not lines[i + 1].strip())
        # trafilatura compresses each paragraph into a single long line with no
        # blank separators.  Treat a long adjacent line (>200 chars) as an
        # acceptable alternative to a blank line for heading isolation.
        prev_is_long = i > 0 and len(lines[i - 1].strip()) > 200
        next_is_long = i < n - 1 and len(lines[i + 1].strip()) > 200
        isolated = prev_blank or next_blank or prev_is_long or next_is_long

        is_heading = False

        # ── Well-known section names (5f) ─────────────────────────────────
        if not is_heading and isolated:
            if stripped.lower().rstrip(".") in _WELL_KNOWN:
                is_heading = True

        # ── Roman numeral sections (5b) ───────────────────────────────────
        if not is_heading and len(stripped) <= 120:
            first_word = stripped.split()[0]
            if (
                first_word in _ROMAN_NUMERALS
                and len(stripped) > len(first_word) + 1
                and stripped[len(first_word) + 1 : len(first_word) + 2].isupper()
            ):
                is_heading = True

        # ── Numbered sections (always reliable) ───────────────────────────
        if not is_heading:
            if re.match(r"^\d+\.?\d*\.?\s+[A-Za-z]", stripped):
                is_heading = True

        # ── ALL CAPS: short line surrounded by blank/long-paragraph context ─
        # Limits: 80 chars / 12 words — covers longer titles like
        # "THE EU APPROACH: CONSTRUCTING CORPORATE ACCOUNTABILITY VIA DUE DILIGENCE"
        if not is_heading and isolated:
            if (
                stripped.isupper()
                and len(stripped) <= 80
                and len(stripped.split()) <= 12
            ):
                is_heading = True

        # ── Common academic section names ─────────────────────────────────
        # One-sided check (preceding context) is enough — these names are
        # specific enough that false positives are very rare.
        if not is_heading and (prev_blank or prev_is_long):
            if stripped.lower().rstrip(".") in _COMMON_HEADINGS:
                is_heading = True

        if is_heading:
            candidates.append({
                "title": stripped,
                "level": 2,
                "start": offsets[i],
                "_caps": stripped.isupper(),
            })

    # ── 5e: Footnote sequence detection (population-based) ───────────────
    # Separate numbered candidates into dotted/undotted groups and discard
    # any group whose maximum integer exceeds 30 (endnote sequences).
    # Applied BEFORE the individual length filter: more reliable and also
    # catches short footnotes like "3 See however section 2…".
    dotted_ids: list[int] = []
    undotted_ids: list[int] = []

    for c in candidates:
        t = c["title"]
        if re.match(r"^\d+\.", t):
            dotted_ids.append(id(c))
        elif re.match(r"^\d+\s", t):
            undotted_ids.append(id(c))

    discard: set[int] = set()

    def _max_int_in_group(group_ids: list[int]) -> int:
        objs = {id(c): c for c in candidates}
        nums = []
        for oid in group_ids:
            if oid in objs:
                m = re.match(r"^(\d+)", objs[oid]["title"])
                if m:
                    try:
                        nums.append(int(m.group(1)))
                    except ValueError:
                        pass
        return max(nums) if nums else 0

    # Dotted numbers (1. Introduction) are strongly associated with section
    # numbering — never discard by count alone.
    # Undotted numbers are ambiguous: discard only when max > 30 AND dotted
    # candidates also exist (the dotted ones are the real sections, the
    # undotted ones are footnotes).  If there are no dotted candidates, let
    # the individual length filter (5c) and running-header dedup handle them.
    if _max_int_in_group(undotted_ids) > 30 and dotted_ids:
        discard.update(undotted_ids)

    # ── OCR-specific: aggressive dotted-footnote filtering ────────────────
    # In OCR'd PDFs (scanned journal articles), page-footnotes are numbered
    # sequentially (sometimes reaching 40–70 per paper) and match the dotted
    # numbered pattern "16. Code, above n 4, at 93–94."  For OCR text, if
    # dotted numbers go high AND structural candidates (ALL CAPS, well-known
    # names) also exist, the dotted group is footnotes — discard it.
    if is_ocr:
        dotted_max = _max_int_in_group(dotted_ids)
        structural_ids = set(id(c) for c in candidates if id(c) not in set(dotted_ids) | set(undotted_ids))
        if dotted_max > 15 and structural_ids:
            discard.update(dotted_ids)

    # ── 5c: Individual length filter for numbered candidates ─────────────
    # A numbered line whose body text (after the number) is > 60 chars is
    # likely a footnote, not a section heading.  Applied after 5e.
    for c in candidates:
        if id(c) in discard:
            continue
        m = re.match(r"^\d+\.?\s+(.*)", c["title"])
        if m and len(m.group(1)) > 60:
            discard.add(id(c))

    candidates = [c for c in candidates if id(c) not in discard]

    # ── 5a + 5d: Running header deduplication ─────────────────────────────
    # Normalise: lowercase, strip leading/trailing whitespace, strip trailing
    # 2–4 digit page numbers from ALL CAPS lines.  Any normalised heading
    # appearing 3+ times (2+ for ALL CAPS) is a running header — discard ALL
    # instances.

    def _norm_heading(c: dict) -> str:
        t = c["title"]
        if c.get("_caps"):
            t = re.sub(r"\s+\d{2,4}$", "", t)
        return t.lower().strip()

    norm_count: dict[str, int] = {}
    for c in candidates:
        k = _norm_heading(c)
        norm_count[k] = norm_count.get(k, 0) + 1

    threshold_caps = 2
    threshold_other = 3

    candidates = [
        c for c in candidates
        if norm_count[_norm_heading(c)] < (threshold_caps if c.get("_caps") else threshold_other)
    ]

    # ── 5a-extra: Numbered-candidate body-text deduplication ──────────────
    # Page-number-prefixed running headers look like "2 Journal Name 0(0)",
    # "4 Journal Name 0(0)", etc.  Each candidate has a unique leading integer
    # (the page number) so the title-level dedup above misses them, but the
    # body text after the leading number is identical.  If the same body text
    # (after stripping the leading integer and punctuation) appears 3+ times
    # across numbered candidates, treat the whole set as running headers.
    body_text_count: dict[str, int] = {}
    for c in candidates:
        m = re.match(r"^\d+\.?\s+(.*)", c["title"])
        if m:
            body = m.group(1).lower().strip()
            body_text_count[body] = body_text_count.get(body, 0) + 1

    candidates = [
        c for c in candidates
        if not (
            re.match(r"^\d+\.?\s+(.*)", c["title"])
            and body_text_count.get(
                re.match(r"^\d+\.?\s+(.*)", c["title"]).group(1).lower().strip(), 0
            ) >= 3
        )
    ]

    # ── Clean up internal keys ────────────────────────────────────────────
    for c in candidates:
        c.pop("_caps", None)

    # ── Fill in end offsets and word counts ───────────────────────────────
    for j, sec in enumerate(candidates):
        sec["end"] = (
            candidates[j + 1]["start"] if j + 1 < len(candidates) else len(text)
        )
        sec["word_count"] = len(text[sec["start"] : sec["end"]].split())

    # ── Post-hoc size filtering ───────────────────────────────────────────
    # Merge tiny artefact sections (footnotes, author lines) into neighbours.
    candidates = consolidate_tiny_sections(candidates, text)

    # If the majority of remaining sections are still tiny, detection has
    # latched onto a non-section pattern.  Return empty so the caller's
    # keyword_skeleton fallback produces more useful navigation.
    if _majority_tiny(candidates):
        return []

    return candidates
