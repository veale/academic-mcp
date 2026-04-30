"""Tests for PDF highlight charmap — build_charmap_bytes and offsets_to_pdf_rects.

A synthetic two-page PDF is created in-memory with PyMuPDF so that the exact
character positions are known.  The tests verify:

1. ``build_charmap_bytes`` produces one record per character in the extracted
   text and assigns non-null rects to real word characters.
2. ``offsets_to_pdf_rects`` loads the persisted charmap and returns merged
   per-page rects that enclose the expected words.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import fitz  # PyMuPDF

from academic_mcp.pdf_extractor import (
    _CHARMAP_FMT,
    _CHARMAP_RECORD_SIZE,
    build_charmap_bytes,
    extract_text_with_sections,
)
from academic_mcp.core.highlights import offsets_to_pdf_rects
from academic_mcp.text_cache import charmap_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_pdf() -> bytes:
    """Return bytes of a minimal two-page PDF with known word positions."""
    doc = fitz.open()

    p0 = doc.new_page(width=595, height=842)   # A4
    p0.insert_text((72, 100), "Abstract", fontsize=14)
    p0.insert_text((72, 130), "Hello World this is page one.", fontsize=11)

    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 100), "Introduction", fontsize=14)
    p1.insert_text((72, 130), "Second page content here.", fontsize=11)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_charmap_length_matches_text():
    """Charmap must have exactly one record per character in the extracted text."""
    pdf_bytes = _make_test_pdf()
    result = extract_text_with_sections(pdf_bytes)
    text = result["text"]

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    charmap = build_charmap_bytes(pdf_bytes, text)
    doc.close()

    assert len(charmap) == len(text) * _CHARMAP_RECORD_SIZE


def test_charmap_word_chars_have_nonzero_rects():
    """Word characters must have non-null rects; whitespace must be null."""
    pdf_bytes = _make_test_pdf()
    result = extract_text_with_sections(pdf_bytes)
    text = result["text"]

    charmap = build_charmap_bytes(pdf_bytes, text)

    # Find "Hello" in the extracted text
    idx = text.find("Hello")
    assert idx != -1, "Expected 'Hello' in extracted text"

    for i in range(idx, idx + len("Hello")):
        page, x0, y0, x1, y1 = struct.unpack_from(_CHARMAP_FMT, charmap, i * _CHARMAP_RECORD_SIZE)
        assert x1 > x0, f"Zero-width rect for char {text[i]!r} at pos {i}"
        assert y1 > y0, f"Zero-height rect for char {text[i]!r} at pos {i}"


def test_charmap_page_header_chars_are_null():
    """Characters inside '--- Page N ---' page headers must be null records."""
    pdf_bytes = _make_test_pdf()
    result = extract_text_with_sections(pdf_bytes)
    text = result["text"]

    charmap = build_charmap_bytes(pdf_bytes, text)

    hdr = "\n--- Page 1 ---\n"
    idx = text.find(hdr)
    assert idx != -1, "Expected page header in extracted text"

    null_record = bytes(_CHARMAP_RECORD_SIZE)
    for i in range(idx, idx + len(hdr)):
        record = charmap[i * _CHARMAP_RECORD_SIZE : (i + 1) * _CHARMAP_RECORD_SIZE]
        assert record == null_record, f"Expected null record for header char {text[i]!r}"


def test_charmap_page_assignment():
    """'Introduction' on page 2 must be mapped to page index 1."""
    pdf_bytes = _make_test_pdf()
    result = extract_text_with_sections(pdf_bytes)
    text = result["text"]

    charmap = build_charmap_bytes(pdf_bytes, text)

    idx = text.find("Introduction")
    assert idx != -1, "Expected 'Introduction' in extracted text"

    pages_seen = set()
    for i in range(idx, idx + len("Introduction")):
        page, *_ = struct.unpack_from(_CHARMAP_FMT, charmap, i * _CHARMAP_RECORD_SIZE)
        pages_seen.add(page)

    assert pages_seen == {1}, f"'Introduction' should be on page 1 (0-indexed), got {pages_seen}"


def test_offsets_to_pdf_rects_returns_rects(tmp_path, monkeypatch):
    """offsets_to_pdf_rects must return at least one PageRects for a valid range."""
    from academic_mcp import config as cfg_module

    pdf_bytes = _make_test_pdf()
    result = extract_text_with_sections(pdf_bytes)
    text = result["text"]
    charmap = build_charmap_bytes(pdf_bytes, text)

    # Redirect cache dir to a temp directory
    monkeypatch.setattr(cfg_module.config, "pdf_cache_dir", tmp_path)

    cache_key = "test_key_abc123"
    cm_path = tmp_path / f"{cache_key}.charmap.bin"
    cm_path.write_bytes(charmap)

    idx = text.find("Hello")
    assert idx != -1

    page_rects = offsets_to_pdf_rects(cache_key, [(idx, idx + len("Hello World"))])
    assert len(page_rects) >= 1
    assert page_rects[0].page == 0
    assert len(page_rects[0].rects) >= 1
    r = page_rects[0].rects[0]
    assert r.x1 > r.x0 and r.y1 > r.y0


def test_offsets_to_pdf_rects_missing_charmap(tmp_path, monkeypatch):
    """offsets_to_pdf_rects must return [] when no charmap file exists."""
    from academic_mcp import config as cfg_module

    monkeypatch.setattr(cfg_module.config, "pdf_cache_dir", tmp_path)
    result = offsets_to_pdf_rects("no_such_key", [(0, 10)])
    assert result == []


def test_offsets_to_pdf_rects_merges_line(tmp_path, monkeypatch):
    """Characters on the same line must be merged into a single Rect."""
    from academic_mcp import config as cfg_module

    pdf_bytes = _make_test_pdf()
    result = extract_text_with_sections(pdf_bytes)
    text = result["text"]
    charmap = build_charmap_bytes(pdf_bytes, text)

    monkeypatch.setattr(cfg_module.config, "pdf_cache_dir", tmp_path)

    cache_key = "merge_test_key"
    (tmp_path / f"{cache_key}.charmap.bin").write_bytes(charmap)

    phrase = "Hello World"
    idx = text.find(phrase)
    assert idx != -1

    page_rects = offsets_to_pdf_rects(cache_key, [(idx, idx + len(phrase))])
    assert page_rects, "Expected at least one PageRects"
    # "Hello World" is one line — must merge to a single rect
    assert len(page_rects[0].rects) == 1, (
        f"Expected one merged rect for '{phrase}', got {len(page_rects[0].rects)}"
    )
