"""PDF text extraction using PyMuPDF (fitz)."""

import logging
import re
from pathlib import Path

import fitz  # PyMuPDF

from .config import config

logger = logging.getLogger(__name__)


def _open_doc(source: Path | bytes) -> fitz.Document:
    """Open a PDF from a file path (zero-copy) or raw bytes."""
    if isinstance(source, Path):
        return fitz.open(filename=str(source))
    return fitz.open(stream=source, filetype="pdf")


def extract_text(source: Path | bytes, max_length: int | None = None) -> dict:
    """Extract text from a PDF file path or bytes.

    Prefers Path for near-zero RAM usage (PyMuPDF reads from disk).

    Returns:
        {
            "text": str,           # extracted text
            "pages": int,          # total pages
            "truncated": bool,     # whether text was truncated
            "metadata": dict,      # PDF metadata (title, author, etc.)
            "sections": list,      # detected section headers
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

    full_text = []
    sections = []
    accumulated_len = 0

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")

        if text.strip():
            header = f"\n--- Page {page_num + 1} ---\n"
            full_text.append(header)
            full_text.append(text)
            accumulated_len += len(header) + len(text)

            # Try to detect section headers (lines that are short, capitalized,
            # or match common patterns like "1. Introduction")
            for line in text.split("\n"):
                line = line.strip()
                if line and _looks_like_header(line):
                    sections.append({
                        "title": line,
                        "page": page_num + 1,
                    })

            # Early exit: stop parsing pages once we've already exceeded the
            # context limit.  This saves massive CPU/RAM on 600-page textbooks
            # where 90% of the extracted text would be thrown away immediately.
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

    doc.close()

    return {
        "text": combined,
        "pages": metadata["pages"],
        "truncated": truncated,
        "metadata": metadata,
        "sections": sections,
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
    """Heuristic: does this line look like a section header?"""
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
