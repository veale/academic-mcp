"""PDF text extraction using PyMuPDF (fitz)."""

import logging
import re
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

from .config import config

logger = logging.getLogger(__name__)


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
                line_text = "".join(span.get("text", "") for span in spans)
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
    max_len = max_length or config.max_context_length

    doc = _open_doc(source)

    metadata = {
        "title": doc.metadata.get("title", ""),
        "author": doc.metadata.get("author", ""),
        "subject": doc.metadata.get("subject", ""),
        "pages": len(doc),
    }

    body_size = _determine_body_font_size(doc, sample_pages=body_sample_pages)

    # If the PDF doesn't have at least 3 distinct font sizes we can't reliably
    # filter — scanned PDFs or unusual layouts.  Fall back to the plain extractor.
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
            "PDF has only %d distinct font sizes — falling back to extract_text",
            len(size_counter),
        )
        result = extract_text(source, max_length)
        result["section_detection"] = "text_heuristic"
        return result

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

                line_text = "".join(s.get("text", "") for s in kept_spans)
                stripped = line_text.strip()
                if not stripped:
                    continue

                # Compute font metadata for level clustering (Issue 2)
                _font_size = max(
                    (round(s.get("size", 0), 1) for s in kept_spans),
                    default=body_size,
                )
                _font_flags = max(
                    (s.get("flags", 0) for s in kept_spans),
                    default=0,
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

    # Re-assign heading levels based on font-size clustering (Issue 2).
    # Group sections by (font_size, is_bold, is_italic).  If there are 2+
    # distinct groups, the largest/boldest group becomes level 2 (main
    # sections) and the rest become level 3 (subsections).
    if len(sections) >= 2 and len(heading_candidates) >= 2:
        font_groups: dict[tuple, list[int]] = {}
        for sec_idx, mid in enumerate(section_marker_ids):
            meta = meta_by_id.get(mid)
            if meta:
                fs = meta.get("_font_size", body_size)
                ff = meta.get("_font_flags", 0)
                key = (fs, bool(ff & 16), bool(ff & 2))
                font_groups.setdefault(key, []).append(sec_idx)

        if len(font_groups) >= 2:
            # Sort: larger size first; within same size bold > italic > plain
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
