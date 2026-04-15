"""PDF text extraction using PyMuPDF (fitz)."""

import logging
import re
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

from .config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Span / font-name helpers
# ---------------------------------------------------------------------------

def _base_font_name(name: str) -> str:
    """Strip the 6-char subset prefix if present: ``'XAUHVA+CMR17'`` → ``'CMR17'``."""
    if "+" in name:
        return name.split("+", 1)[1]
    return name


def _join_spans_with_spacing(spans: list[dict], body_size: float = 10.0) -> str:
    """Join span texts, inserting spaces where bounding-box gaps indicate word breaks.

    LaTeX PDFs position words via x-coordinates rather than literal space
    characters.  Each span's ``bbox`` is ``(x0, y0, x1, y1)``.  When the
    gap between one span's x1 and the next span's x0 exceeds a fraction
    of the font size, a space is inserted.

    Falls back to simple concatenation if spans lack bbox data.
    """
    if not spans:
        return ""

    parts: list[str] = []
    prev_x1: float | None = None
    prev_y0: float | None = None

    for span in spans:
        text = span.get("text", "")
        if not text:
            continue
        bbox = span.get("bbox")

        if bbox and prev_x1 is not None and prev_y0 is not None:
            x0, y0, x1, y1 = bbox
            font_size = span.get("size", body_size) or body_size

            # Check if we're on a different line (y-coordinate jump)
            if abs(y0 - prev_y0) > font_size * 0.5:
                parts.append(" ")
            else:
                # Horizontal gap — a space is roughly 0.2–0.35em in most fonts.
                # Use 0.15em as threshold to catch narrow spaces without
                # false-triggering on kerned pairs.
                gap = x0 - prev_x1
                if gap > font_size * 0.15:
                    parts.append(" ")
                elif gap < -font_size * 0.5:
                    # Large negative gap means the span wrapped to a new
                    # column or the next line — treat as space
                    parts.append(" ")

        parts.append(text)

        if bbox:
            prev_x1 = bbox[2]   # x1
            prev_y0 = bbox[1]   # y0

    return "".join(parts)


def _open_doc(source: Path | bytes) -> fitz.Document:
    """Open a PDF from a file path (zero-copy) or raw bytes."""
    if isinstance(source, Path):
        return fitz.open(filename=str(source))
    return fitz.open(stream=source, filetype="pdf")


# ---------------------------------------------------------------------------
# Font-size analysis helpers
# ---------------------------------------------------------------------------

def _determine_body_font_size(doc: fitz.Document, sample_pages: int = 20) -> float:
    """Return the dominant body-text font size via weighted character frequency.

    Analyses the first *sample_pages* pages to determine which font size
    carries the most text — that size is the body text.  Headers use a
    larger size and are detected relative to this baseline.
    """
    font_size_chars: Counter = Counter()
    for page_num in range(min(sample_pages, len(doc))):
        page = doc[page_num]
        try:
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:  # 0 = text block
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if text:
                            size = round(span["size"], 1)
                            font_size_chars[size] += len(text)
        except Exception as exc:
            logger.debug("Font size scan failed on page %d: %s", page_num, exc)
    if not font_size_chars:
        return 10.0
    return font_size_chars.most_common(1)[0][0]


def _is_heading_span(span: dict, body_size: float) -> bool:
    """Return True if *span* looks like a section heading.

    Two signals:

    * **Size** — span is ≥ 1.5 pt larger than body text.  This catches
      numbered or title-case headings that don't use ALL CAPS.
    * **Bold at body size** — span has the bold flag (16) and is short
      (< 100 chars).  Handles PDFs that use bold at body size for headings
      instead of a size increase.  The length guard prevents bold inline
      emphasis (e.g. a key term) from being mistaken for a heading.
    """
    size = round(span.get("size", 0), 1)
    flags = span.get("flags", 0)
    text = span.get("text", "").strip()
    if not text:
        return False
    if size >= body_size + 1.5:
        return True
    if (flags & 16) and abs(size - body_size) < 0.5 and len(text) < 100:
        return True
    return False


def _line_is_predominantly_italic(spans: list[dict], body_size: float) -> bool:
    """Return True if >70% of the line's body-size character content is italic.

    Used to detect italic section headings at body font size — a common style
    in humanities and social-science journals (e.g. Politics & Society).
    Spans at a different size (footnotes, larger headings) are excluded from
    the count so they don't skew the ratio.
    """
    total_chars = 0
    italic_chars = 0
    for span in spans:
        text = span.get("text", "").strip()
        if not text:
            continue
        size = round(span.get("size", 0), 1)
        if abs(size - body_size) > 1.0:
            continue  # ignore non-body-size spans
        n = len(text)
        total_chars += n
        if span.get("flags", 0) & 2:  # bit 1 = italic
            italic_chars += n
    return total_chars > 0 and (italic_chars / total_chars) > 0.7


def _is_latex_pdf(doc: fitz.Document) -> bool:
    """Return True if the PDF was produced by a LaTeX toolchain.

    Checks the ``creator`` and ``producer`` metadata fields, which TeX
    engines and hyperref populate automatically.
    """
    meta = doc.metadata
    creator = (meta.get("creator") or "").lower()
    producer = (meta.get("producer") or "").lower()

    latex_producers = ("pdftex", "xetex", "luatex", "luahbtex",
                       "tex live", "miktex", "vtex")
    for lp in latex_producers:
        if lp in producer:
            return True

    latex_creators = ("latex", "pdftex", "xetex", "luatex",
                      "dvips", "dvipdfm", "xdvipdfmx")
    for lc in latex_creators:
        if lc in creator:
            return True

    # "tex" alone requires a word boundary to avoid false positives
    if re.search(r'\btex\b', creator):
        return True

    return False


def _is_ocr_pdf(doc: fitz.Document) -> bool:
    """Return True if the PDF appears to be from an OCR or scanning pipeline.

    Checks two signals:

    1. **Metadata** — the ``producer`` or ``creator`` field mentions a known
       scanning/OCR tool.
    2. **Font uniformity** — if 95%+ of all text characters are at a single
       font size and there are ≤2 distinct sizes, the PDF was likely OCR'd.
    """
    meta = doc.metadata
    creator = (meta.get("creator") or "").lower()
    producer = (meta.get("producer") or "").lower()

    ocr_signals = (
        "abbyy", "finereader", "tesseract", "omnipage", "readiris",
        "adobe scan", "scansoft", "nuance", "apex covantage",
        "apex pdflib", "epdf", "scanfix", "capture", "paperstream",
    )
    for sig in ocr_signals:
        if sig in creator or sig in producer:
            return True

    # Font uniformity check: sample first 10 pages
    char_counts: Counter = Counter()
    for page_num in range(min(10, len(doc))):
        try:
            for block in doc[page_num].get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if text:
                            size = round(span.get("size", 0), 1)
                            char_counts[size] += len(text)
        except Exception:
            pass

    if char_counts:
        total = sum(char_counts.values())
        dominant = char_counts.most_common(1)[0][1]
        distinct_sizes = len([s for s, c in char_counts.items()
                              if c > total * 0.01])
        if dominant / total > 0.95 and distinct_sizes <= 2:
            return True

    return False


def _clean_ocr_text(text: str) -> str:
    """Normalise common OCR artefacts.

    Applied only when the PDF is identified as OCR'd.  Conservative —
    only fixes patterns with near-zero false positive risk.
    """
    # Normalise ligature Unicode characters to ASCII pairs
    text = text.replace("\ufb01", "fi")   # ﬁ → fi
    text = text.replace("\ufb02", "fl")   # ﬂ → fl
    text = text.replace("\ufb00", "ff")   # ﬀ → ff
    text = text.replace("\ufb03", "ffi")  # ﬃ → ffi
    text = text.replace("\ufb04", "ffl")  # ﬄ → ffl

    # Normalise form-feed and vertical tab to newline
    text = text.replace("\f", "\n").replace("\v", "\n")

    # Collapse runs of 3+ newlines (OCR inserts many blank lines between regions)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


def _classify_latex_heading_level(font_name: str, font_size: float,
                                   body_size: float) -> int:
    """For LaTeX PDFs, use font name + size to assign heading level.

    Returns 2 (main section), 3 (subsection), or 0 if not a heading.
    """
    base = _base_font_name(font_name).lower()

    # ACM acmart: sans bold (Biolinum) = section, serif bold (Libertine) = subsection
    if "biolinum" in base and ("bold" in base or base.endswith("rb") or base.endswith("b")):
        return 2
    if "libertine" in base and ("bold" in base or base.endswith("rb") or base.endswith("b")):
        return 3

    # Computer Modern / Latin Modern bold: design size determines level
    if any(x in base for x in ("cmbx", "cmssbx", "lmbx", "lmssbx")):
        m = re.search(r'(\d+)', base)
        if m:
            design_size = int(m.group(1))
            return 2 if design_size >= 12 else 3
        return 2  # unknown size — assume section

    # KOMA-Script sans-serif headings
    if ("cmss" in base or "lmss" in base) and font_size > body_size:
        return 2

    # Times-based (NeurIPS, ACL, IEEE, NimbusRomNo9L, etc.): use size
    if font_size >= body_size + 2.0:
        return 2
    if font_size >= body_size + 0.5:
        return 3

    return 0


def _locate_headings_in_text(text: str, candidates: list[dict]) -> list[dict]:
    """Find candidate heading strings in *text* and compute character offsets.

    Searches sequentially (honouring document order) with an exact-match
    first pass and a case-insensitive fallback.  Candidates not found in the
    text are silently dropped.
    """
    sections: list[dict] = []
    search_from = 0

    for candidate in candidates:
        title = candidate["title"]
        # Normalise title in case the cleanup pass collapsed double-spaces
        norm_title = re.sub(r" {2,}", " ", title)
        idx = text.find(norm_title, search_from)
        if idx == -1:
            idx = text.lower().find(norm_title.lower(), search_from)
        if idx == -1:
            # Whitespace-collapsed search for LaTeX PDFs where extracted text
            # has no inter-word spaces but ToC titles are clean.
            collapsed_title = re.sub(r"\s+", "", norm_title)
            if collapsed_title:
                collapsed_region = re.sub(r"\s+", "", text[search_from:])
                cidx = collapsed_region.lower().find(collapsed_title.lower())
                if cidx != -1:
                    # Map collapsed offset back to original text offset by
                    # consuming cidx non-whitespace characters from search_from.
                    orig_pos = search_from
                    consumed = 0
                    while consumed < cidx and orig_pos < len(text):
                        if not text[orig_pos].isspace():
                            consumed += 1
                        orig_pos += 1
                    idx = orig_pos
        if idx == -1:
            continue
        sections.append(
            {
                "title": norm_title,
                "page": candidate.get("page", 0),
                "level": candidate.get("level", 2),
                "start": idx,
            }
        )
        search_from = idx + len(norm_title)

    # Fill in end offsets and word counts
    for i, sec in enumerate(sections):
        sec["end"] = sections[i + 1]["start"] if i + 1 < len(sections) else len(text)
        sec["word_count"] = len(text[sec["start"] : sec["end"]].split())

    return sections


def _sections_from_toc(doc: fitz.Document, clean_text: str) -> list[dict] | None:
    """Try to build sections from the PDF bookmark/outline tree.

    Returns a list of section dicts (same format as :func:`_locate_headings_in_text`)
    or ``None`` if the ToC is absent, too sparse, or fails to match the text.

    PDF bookmarks from hyperref map:
      - level 1 → ``\\section``        → our level 2 (main)
      - level 2 → ``\\subsection``     → our level 3 (sub)
      - level 3 → ``\\subsubsection``  → our level 3 (sub, capped)
    """
    toc = doc.get_toc()  # [[level, title, page_number], ...]
    if not toc:
        return None

    candidates = []
    for level, title, page_num in toc:
        if level > 3 or level < 1:
            continue
        title = title.strip()
        if not title:
            continue
        lower = title.lower()
        if lower in ("contents", "table of contents", "list of figures",
                     "list of tables", "index"):
            continue
        candidates.append({
            "title": title,
            "page": page_num,
            "level": min(level + 1, 3),  # map PDF levels to our 2/3 scheme
        })

    if len(candidates) < 3:
        return None

    sections = _locate_headings_in_text(clean_text, candidates)

    # Require ≥60% of candidates to have matched; otherwise the ToC titles
    # probably don't correspond well to the extracted text.
    if len(sections) < len(candidates) * 0.6:
        return None

    return sections


def _extract_filtered_text(
    doc: fitz.Document,
    body_size: float,
    max_len: int,
) -> tuple[str, bool]:
    """Extract text with footnote filtering but without heading markers.

    Returns ``(text, truncated)``.  Used as the first (plain) pass before
    attempting ToC-based section detection.
    """
    full_text: list[str] = []
    accumulated_len = 0
    truncated = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        header = f"\n--- Page {page_num + 1} ---\n"
        full_text.append(header)
        accumulated_len += len(header)

        try:
            page_dict = page.get_text("dict")
        except Exception as exc:
            logger.debug("get_text('dict') failed on page %d: %s", page_num, exc)
            plain = page.get_text("text")
            full_text.append(plain)
            accumulated_len += len(plain)
            if accumulated_len > max_len:
                truncated = True
                break
            continue

        page_lines: list[str] = []
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                kept_spans = [
                    s for s in spans
                    if s.get("text", "").strip()
                    and round(s.get("size", 0), 1) >= body_size - 1.0
                ]
                if not kept_spans:
                    continue
                line_text = _join_spans_with_spacing(kept_spans, body_size)
                if line_text.strip():
                    page_lines.append(line_text + "\n")

        page_text = "".join(page_lines)
        full_text.append(page_text)
        accumulated_len += len(page_text)

        if accumulated_len > max_len:
            truncated = True
            break

    combined = "".join(full_text)
    combined = re.sub(r"\n{3,}", "\n\n", combined)
    combined = re.sub(r" {2,}", " ", combined)

    if truncated and len(combined) > max_len:
        combined = combined[:max_len]
        combined += "\n\n[... TRUNCATED — full text exceeds context limit ...]"

    return combined, truncated


# ---------------------------------------------------------------------------
# Public extraction API
# ---------------------------------------------------------------------------

def extract_text(source: Path | bytes, max_length: int | None = None) -> dict:
    """Extract text from a PDF file path or bytes with font-based section detection.

    Uses ``page.get_text("dict")`` to obtain per-span font metadata so that
    section headings can be identified by size/weight rather than fragile
    text-content heuristics.  Two detection signals are used:

    * A span is ≥ 1.5 pt larger than the dominant body-text size.
    * A span carries the bold flag at body-text size and is short (< 100 chars).

    Lines where any span triggers either signal are treated as heading
    candidates, subject to a length cap (≤ 15 words) that prevents bold
    abstract-like lead-ins from being misclassified.

    Prefers ``Path`` for near-zero RAM usage (PyMuPDF reads from disk).

    Returns::

        {
            "text": str,
            "pages": int,
            "truncated": bool,
            "metadata": dict,
            "sections": list[dict],   # [{title, page, level, start, end, word_count}]
        }
    """
    max_len = max_length or config.max_context_length

    doc = _open_doc(source)

    metadata = {
        "title": doc.metadata.get("title", ""),
        "author": doc.metadata.get("author", ""),
        "subject": doc.metadata.get("subject", ""),
        "pages": len(doc),
    }

    body_size = _determine_body_font_size(doc)

    full_text: list[str] = []
    heading_candidates: list[dict] = []
    accumulated_len = 0

    for page_num in range(len(doc)):
        page = doc[page_num]

        header = f"\n--- Page {page_num + 1} ---\n"
        full_text.append(header)
        accumulated_len += len(header)

        try:
            page_dict = page.get_text("dict")
        except Exception as exc:
            # Fall back to plain text if the structured API fails for this page
            logger.debug("get_text('dict') failed on page %d: %s", page_num, exc)
            plain = page.get_text("text")
            full_text.append(plain)
            accumulated_len += len(plain)
            if accumulated_len > max_len:
                break
            continue

        page_lines: list[str] = []

        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = _join_spans_with_spacing(spans, body_size)
                if not line_text.strip():
                    continue

                # A line is a heading candidate when any span is heading-sized
                # AND the full line is short (not a bold introductory paragraph).
                stripped = line_text.strip()
                if (
                    any(_is_heading_span(sp, body_size) for sp in spans)
                    and len(stripped) < 150
                    and len(stripped.split()) <= 15
                ):
                    approx_offset = accumulated_len + len("".join(page_lines))
                    heading_candidates.append(
                        {
                            "title": stripped,
                            "page": page_num + 1,
                            "level": 2,
                            "_approx_offset": approx_offset,
                        }
                    )
                elif (
                    _line_is_predominantly_italic(spans, body_size)
                    and len(stripped) < 100
                    and len(stripped.split()) <= 12
                    and not stripped.endswith(".")
                ):
                    # Italic heading at body size (e.g. Politics & Society style)
                    approx_offset = accumulated_len + len("".join(page_lines))
                    heading_candidates.append(
                        {
                            "title": stripped,
                            "page": page_num + 1,
                            "level": 2,
                            "_approx_offset": approx_offset,
                        }
                    )

                page_lines.append(line_text + "\n")

        page_text = "".join(page_lines)
        full_text.append(page_text)
        accumulated_len += len(page_text)

        if accumulated_len > max_len:
            break

    combined = "".join(full_text)

    # Clean up excessive whitespace
    combined = re.sub(r"\n{3,}", "\n\n", combined)
    combined = re.sub(r" {2,}", " ", combined)

    truncated = False
    if len(combined) > max_len:
        combined = combined[:max_len]
        combined += "\n\n[... TRUNCATED — full text exceeds context limit ...]"
        truncated = True

    sections = _locate_headings_in_text(combined, heading_candidates)

    doc.close()

    return {
        "text": combined,
        "pages": metadata["pages"],
        "truncated": truncated,
        "metadata": metadata,
        "sections": sections,
    }


def extract_text_with_sections(
    source: Path | bytes,
    max_length: int | None = None,
    body_sample_pages: int = 30,
) -> dict:
    """Extract text from a PDF with aggressive footnote/running-header filtering.

    Unlike :func:`extract_text`, this function **discards** any span whose font
    size is more than 1 pt smaller than the dominant body-text size.  That
    single rule eliminates footnotes, endnotes, page numbers, and running
    journal-name headers — all of which use a smaller type size in the vast
    majority of academic PDFs.

    Section headings are identified the same way as in :func:`extract_text` but
    are injected directly into the output text as ``§§SEC:id:level:title§§``
    markers so that character offsets can be computed exactly (no fuzzy search
    needed).

    Fall-back: if the PDF has fewer than 3 distinct font sizes (scans,
    unusual layouts) the function delegates to :func:`extract_text` unchanged.

    Returns::

        {
            "text":     str,
            "pages":    int,
            "truncated": bool,
            "metadata": dict,
            "sections": list[dict],  # [{title, level, start, end, word_count}]
            "section_detection": str,  # "pdf_font_analysis" or "text_heuristic"
        }
    """
    # Local import avoids circular dependency (content_extractor ← server → pdf_extractor)
    from . import content_extractor as _ce

    max_len = max_length or config.max_context_length

    doc = _open_doc(source)
    is_latex = _is_latex_pdf(doc)
    is_ocr = _is_ocr_pdf(doc)

    metadata = {
        "title": doc.metadata.get("title", ""),
        "author": doc.metadata.get("author", ""),
        "subject": doc.metadata.get("subject", ""),
        "pages": len(doc),
        "is_ocr": is_ocr,
    }

    body_size = _determine_body_font_size(doc, sample_pages=body_sample_pages)

    # --- Strategy 0: OCR fast-path ---
    # OCR'd PDFs have uniform font sizes (font analysis is useless) and need
    # OCR-specific footnote filtering.  Extract text and apply text heuristic
    # directly — no font analysis or marker pass needed.
    if is_ocr:
        logger.debug("OCR PDF detected — skipping font analysis for %s", getattr(source, 'name', ''))
        plain_text, plain_truncated = _extract_filtered_text(doc, body_size, max_len)
        plain_text = _clean_ocr_text(plain_text)
        doc.close()
        sections = _ce.detect_sections_from_text(plain_text, is_ocr=True)
        return {
            "text": plain_text,
            "pages": metadata["pages"],
            "truncated": plain_truncated,
            "metadata": metadata,
            "sections": sections,
            "section_detection": "text_heuristic",
        }

    # Font size diversity check — scanned/unusual PDFs with < 3 sizes fall
    # straight to text heuristic (font analysis cannot distinguish headings).
    size_counter: Counter = Counter()
    for page_num in range(min(body_sample_pages, len(doc))):
        try:
            for block in doc[page_num].get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        size_counter[round(span.get("size", 0), 1)] += 1
        except Exception:
            pass

    if len(size_counter) < 3:
        logger.debug(
            "PDF has only %d distinct font sizes — falling back to text heuristic",
            len(size_counter),
        )
        plain_text, plain_truncated = _extract_filtered_text(doc, body_size, max_len)
        doc.close()
        sections = _ce.detect_sections_from_text(plain_text)
        return {
            "text": plain_text,
            "pages": metadata["pages"],
            "truncated": plain_truncated,
            "metadata": metadata,
            "sections": sections,
            "section_detection": "text_heuristic",
        }

    # --- Strategy 1: PDF bookmark / outline (ToC) ---
    # Do a plain filtered-text pass first so we can try ToC matching without
    # the overhead of marker injection.  This pass is skipped (re-used) if
    # ToC succeeds; if it fails we proceed to the marker-injection pass below.
    plain_text, plain_truncated = _extract_filtered_text(doc, body_size, max_len)
    toc_sections = _sections_from_toc(doc, plain_text)
    if toc_sections is not None:
        toc_sections = _ce.consolidate_tiny_sections(toc_sections, plain_text)
        logger.debug("Using PDF ToC for section detection (%d sections)", len(toc_sections))
        doc.close()
        return {
            "text": plain_text,
            "pages": metadata["pages"],
            "truncated": plain_truncated,
            "metadata": metadata,
            "sections": toc_sections,
            "section_detection": "pdf_toc",
        }

    # --- Strategy 2: Font-analysis marker pass ---
    full_text: list[str] = []
    heading_counter = [0]
    heading_candidates: list[dict] = []
    accumulated_len = 0
    truncated = False

    for page_num in range(len(doc)):
        page = doc[page_num]

        header = f"\n--- Page {page_num + 1} ---\n"
        full_text.append(header)
        accumulated_len += len(header)

        try:
            page_dict = page.get_text("dict")
        except Exception as exc:
            logger.debug("get_text('dict') failed on page %d: %s", page_num, exc)
            plain = page.get_text("text")
            full_text.append(plain)
            accumulated_len += len(plain)
            if accumulated_len > max_len:
                truncated = True
                break
            continue

        page_lines: list[str] = []

        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue

                # Collect spans that are NOT footnote/running-header size
                kept_spans = []
                has_heading_span = False

                for span in spans:
                    size = round(span.get("size", 0), 1)
                    text_content = span.get("text", "")
                    if not text_content.strip():
                        continue

                    # Discard small text (footnotes, page numbers, running headers)
                    if size < body_size - 1.0:
                        continue

                    kept_spans.append(span)
                    if _is_heading_span(span, body_size):
                        has_heading_span = True

                if not kept_spans:
                    continue

                line_text = _join_spans_with_spacing(kept_spans, body_size)
                stripped = line_text.strip()
                if not stripped:
                    continue

                # Compute font metadata for level clustering
                _font_size = max(
                    (round(s.get("size", 0), 1) for s in kept_spans),
                    default=body_size,
                )
                _font_flags = max(
                    (s.get("flags", 0) for s in kept_spans),
                    default=0,
                )
                _max_span = max(
                    kept_spans,
                    key=lambda s: round(s.get("size", 0), 1),
                    default=None,
                )
                _font_name = _base_font_name(
                    _max_span.get("font", "") if _max_span else ""
                )

                # Heading candidate: any heading-sized span, short line
                is_italic_heading = (
                    not has_heading_span
                    and _line_is_predominantly_italic(kept_spans, body_size)
                    and len(stripped) < 100
                    and len(stripped.split()) <= 12
                    and not stripped.endswith(".")
                )
                if (
                    has_heading_span
                    and len(stripped) < 150
                    and len(stripped.split()) <= 15
                ) or is_italic_heading:
                    mid = heading_counter[0]
                    heading_counter[0] += 1
                    marker = f"§§SEC:{mid}:2:{stripped}§§\n"
                    heading_candidates.append({
                        "title": stripped,
                        "level": 2,
                        "page": page_num + 1,
                        "_marker_id": mid,
                        "_font_size": _font_size,
                        "_font_flags": _font_flags,
                        "_font_name": _font_name,
                    })
                    page_lines.append(marker)
                else:
                    page_lines.append(line_text + "\n")

        page_text = "".join(page_lines)
        full_text.append(page_text)
        accumulated_len += len(page_text)

        if accumulated_len > max_len:
            truncated = True
            break

    combined = "".join(full_text)

    # Clean up excessive whitespace (but preserve markers)
    combined = re.sub(r"\n{3,}", "\n\n", combined)
    combined = re.sub(r" {2,}", " ", combined)

    if truncated and len(combined) > max_len:
        combined = combined[:max_len]
        combined += "\n\n[... TRUNCATED — full text exceeds context limit ...]"

    # Parse §§SEC markers out of combined text to get exact offsets
    _MARKER_RE = re.compile(r"§§SEC:(\d+):(\d+):(.+?)§§\n?")
    sections: list[dict] = []
    section_marker_ids: list[int] = []
    clean_parts: list[str] = []
    cursor = 0

    meta_by_id = {c["_marker_id"]: c for c in heading_candidates}

    for m in _MARKER_RE.finditer(combined):
        clean_parts.append(combined[cursor : m.start()])
        clean_offset = sum(len(p) for p in clean_parts)
        mid = int(m.group(1))
        sections.append({
            "title": m.group(3).strip(),
            "level": int(m.group(2)),
            "start": clean_offset,
            "page": meta_by_id[mid]["page"] if mid in meta_by_id else 0,
        })
        section_marker_ids.append(mid)
        cursor = m.end()

    clean_parts.append(combined[cursor:])
    clean_text = "".join(clean_parts)

    # Fill in end offsets and word counts
    for i, sec in enumerate(sections):
        sec["end"] = sections[i + 1]["start"] if i + 1 < len(sections) else len(clean_text)
        sec["word_count"] = len(clean_text[sec["start"] : sec["end"]].split())

    # Re-assign heading levels based on font metadata.
    if len(sections) >= 2 and len(heading_candidates) >= 2:
        if is_latex:
            # For LaTeX PDFs use font-name heuristics for reliable level assignment.
            for sec_idx, mid in enumerate(section_marker_ids):
                meta = meta_by_id.get(mid)
                if meta:
                    level = _classify_latex_heading_level(
                        meta.get("_font_name", ""),
                        meta.get("_font_size", body_size),
                        body_size,
                    )
                    if level in (2, 3):
                        sections[sec_idx]["level"] = level
        else:
            # Non-LaTeX: group by (font_size, is_bold, is_italic) and assign
            # the largest/boldest group as level 2, the rest as level 3.
            font_groups: dict[tuple, list[int]] = {}
            for sec_idx, mid in enumerate(section_marker_ids):
                meta = meta_by_id.get(mid)
                if meta:
                    fs = meta.get("_font_size", body_size)
                    ff = meta.get("_font_flags", 0)
                    key = (fs, bool(ff & 16), bool(ff & 2))
                    font_groups.setdefault(key, []).append(sec_idx)

            if len(font_groups) >= 2:
                sorted_keys = sorted(
                    font_groups.keys(),
                    key=lambda k: (k[0], k[1], k[2]),
                    reverse=True,
                )
                for idx in font_groups[sorted_keys[0]]:
                    sections[idx]["level"] = 2
                for group_key in sorted_keys[1:]:
                    for idx in font_groups[group_key]:
                        sections[idx]["level"] = 3

    # Post-hoc size filtering: merge tiny artefact sections.
    sections = _ce.consolidate_tiny_sections(sections, clean_text)

    # Quality gate: if font analysis produced mostly junk (footnotes, author
    # lines, etc.), fall back to text heuristic on the plain-text pass.
    if _ce._majority_tiny(sections):
        logger.debug(
            "Font analysis produced majority-tiny sections — falling back to text heuristic"
        )
        doc.close()
        heuristic_sections = _ce.detect_sections_from_text(plain_text)
        return {
            "text": plain_text,
            "pages": metadata["pages"],
            "truncated": plain_truncated,
            "metadata": metadata,
            "sections": heuristic_sections,
            "section_detection": "text_heuristic",
        }

    doc.close()

    return {
        "text": clean_text,
        "pages": metadata["pages"],
        "truncated": truncated,
        "metadata": metadata,
        "sections": sections,
        "section_detection": "pdf_font_analysis",
    }


def extract_text_by_pages(
    source: Path | bytes, start_page: int = 1, end_page: int | None = None
) -> str:
    """Extract text from a specific page range.

    Accepts a file Path (preferred, zero-copy) or raw bytes.
    Pages are 1-indexed.
    """
    doc = _open_doc(source)

    start = max(0, start_page - 1)
    end = min(len(doc), end_page) if end_page else len(doc)

    parts = []
    for page_num in range(start, end):
        page = doc[page_num]
        text = page.get_text("text")
        if text.strip():
            parts.append(f"\n--- Page {page_num + 1} ---\n")
            parts.append(text)

    doc.close()
    return "".join(parts)


def _looks_like_header(line: str) -> bool:
    """Heuristic: does this line look like a section header?

    Retained for use by :func:`~academic_mcp.content_extractor.detect_sections_from_text`
    (Zotero ft-cache path).  Not used internally by :func:`extract_text` which
    now uses font-size analysis instead.
    """
    # Numbered sections: "1. Introduction", "2.1 Methods"
    if re.match(r"^\d+\.?\d*\.?\s+[A-Z]", line):
        return True

    # ALL CAPS short lines
    if line.isupper() and len(line) < 80 and len(line.split()) <= 8:
        return True

    # Common section names
    common = {
        "abstract", "introduction", "methods", "methodology", "results",
        "discussion", "conclusion", "conclusions", "references",
        "acknowledgements", "acknowledgments", "appendix", "supplementary",
        "background", "related work", "literature review", "materials and methods",
        "experimental", "data", "analysis", "findings",
    }
    if line.lower().strip().rstrip(".") in common:
        return True

    return False
