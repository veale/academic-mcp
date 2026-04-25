"""Tests for the chunking module."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import chunking
from academic_mcp.chunking import (
    _CHUNK_CHARS,
    _MAX_FT_CHARS,
    _OVERLAP_CHARS,
    _STRIDE_CHARS,
    Chunk,
    chunk_item,
    _build_context_header,
    _sliding_chunks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    title="Test Paper",
    abstract="An abstract.",
    attachment_key="",
    doi="10.1/test",
):
    return {
        "item_key": "ABCD1234",
        "title": title,
        "abstract": abstract,
        "doi": doi,
        "dateModified": "2025-01-01",
        "attachment_key": attachment_key,
    }


def _write_ft_cache(storage_path: Path, attachment_key: str, content: str) -> None:
    """Write a fake .zotero-ft-cache file."""
    att_dir = storage_path / attachment_key
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / ".zotero-ft-cache").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Abstract-only path
# ---------------------------------------------------------------------------

def test_abstract_only_item_returns_single_chunk():
    item = _make_item(title="My Paper", abstract="Some abstract text here.")
    chunks = chunk_item(item)
    assert len(chunks) == 1
    assert chunks[0].source == "abstract"
    assert "My Paper" in chunks[0].text
    assert "Some abstract text here" in chunks[0].text


def test_empty_item_returns_no_chunks():
    item = _make_item(title="", abstract="")
    chunks = chunk_item(item)
    assert chunks == []


def test_title_only_returns_single_chunk():
    item = _make_item(title="Just a title", abstract="")
    chunks = chunk_item(item)
    assert len(chunks) == 1
    assert chunks[0].text == "Just a title"


def test_abstract_only_offsets_match_text():
    item = _make_item(title="T", abstract="A" * 100)
    chunks = chunk_item(item)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.char_start == 0
    assert c.char_end == len(c.text)


# ---------------------------------------------------------------------------
# ft-cache path
# ---------------------------------------------------------------------------

def test_ft_cache_chunks_are_overlapping(tmp_path, monkeypatch):
    """Consecutive ft-cache chunks should share ~_OVERLAP_CHARS characters."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        chunking.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=str(storage)),
    )

    # Write a ft-cache longer than two chunks
    ft_text = "X" * (_CHUNK_CHARS * 3)
    _write_ft_cache(storage, "ATT1", ft_text)

    item = _make_item(title="Paper", attachment_key="ATT1")
    chunks = chunk_item(item)

    assert len(chunks) >= 3

    # Check overlap between consecutive chunks (offsets only; text has title prefix)
    for i in range(len(chunks) - 1):
        a, b = chunks[i], chunks[i + 1]
        # b should start before a ends
        assert b.char_start < a.char_end
        overlap = a.char_end - b.char_start
        assert overlap == _OVERLAP_CHARS


def test_ft_cache_chunks_prepend_title(tmp_path, monkeypatch):
    """Each ft-cache chunk's embedded text should start with the paper title."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        chunking.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=str(storage)),
    )

    ft_text = "Body " * 500  # ~2500 chars, ≥ 2 chunks
    _write_ft_cache(storage, "ATT1", ft_text)

    item = _make_item(title="My Great Paper", attachment_key="ATT1")
    chunks = chunk_item(item)

    for c in chunks:
        assert c.text.startswith("My Great Paper\n\n")


def test_ft_cache_offsets_point_to_raw_ft_text(tmp_path, monkeypatch):
    """char_start/char_end should be offsets into the raw ft-cache, not into
    the title-prefixed chunk text."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        chunking.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=str(storage)),
    )

    ft_text = "A" * _CHUNK_CHARS + "B" * _CHUNK_CHARS
    _write_ft_cache(storage, "ATT1", ft_text)

    item = _make_item(title="T", attachment_key="ATT1")
    chunks = chunk_item(item)

    first = chunks[0]
    # Offset 0 in the ft-cache = the first 'A'
    assert first.char_start == 0
    assert first.char_end == _CHUNK_CHARS

    # The raw ft-cache text at those offsets should be all A's
    assert ft_text[first.char_start : first.char_end] == "A" * _CHUNK_CHARS


def test_ft_cache_respects_max_chars(tmp_path, monkeypatch):
    """A very large ft-cache should produce no chunk past _MAX_FT_CHARS."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        chunking.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=str(storage)),
    )

    oversized = "Z" * (_MAX_FT_CHARS + 10_000)
    _write_ft_cache(storage, "ATT1", oversized)

    item = _make_item(title="Big Book", attachment_key="ATT1")
    chunks = chunk_item(item)

    for c in chunks:
        assert c.char_end <= _MAX_FT_CHARS


def test_ft_cache_beyond_max_is_ignored(tmp_path, monkeypatch):
    """Characters beyond _MAX_FT_CHARS must not appear in any chunk's char_end."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        chunking.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=str(storage)),
    )

    oversized = "Q" * (_MAX_FT_CHARS + 50_000)
    _write_ft_cache(storage, "ATT1", oversized)

    item = _make_item(title="Long Book", attachment_key="ATT1")
    chunks = chunk_item(item)

    assert len(chunks) > 0
    for c in chunks:
        assert c.char_end <= _MAX_FT_CHARS, (
            f"chunk char_end={c.char_end} exceeds _MAX_FT_CHARS={_MAX_FT_CHARS}"
        )


def test_ft_cache_missing_gives_abstract_fallback(tmp_path, monkeypatch):
    """If the attachment key is set but the .zotero-ft-cache doesn't exist,
    fall back to title + abstract."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        chunking.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=str(storage)),
    )

    item = _make_item(title="T", abstract="An abstract.", attachment_key="NONEXISTENT")
    chunks = chunk_item(item)

    assert len(chunks) == 1
    assert chunks[0].source == "abstract"


# ---------------------------------------------------------------------------
# _sliding_chunks internals
# ---------------------------------------------------------------------------

def test_sliding_chunks_empty_returns_empty():
    assert _sliding_chunks("") == []


def test_sliding_chunks_short_text_single_chunk():
    text = "hello world"
    chunks = _sliding_chunks(text)
    assert len(chunks) == 1
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)
    assert chunks[0].text == text


def test_sliding_chunks_last_chunk_covers_end():
    text = "A" * (_CHUNK_CHARS + _STRIDE_CHARS // 2)
    chunks = _sliding_chunks(text)
    assert chunks[-1].char_end == len(text)


# ---------------------------------------------------------------------------
# _build_context_header
# ---------------------------------------------------------------------------

def test_context_header_title_only():
    """Title alone → no venue or author lines."""
    h = _build_context_header({"title": "My Paper"})
    assert h == "My Paper"
    assert "In:" not in h
    assert "Authors:" not in h


def test_context_header_book_section_uses_book_title():
    """bookTitle should appear as the In: venue line."""
    h = _build_context_header({
        "title": "Article 25: Logging",
        "bookTitle": "The EU LED Commentary",
        "authors": ["Veale", "Kosta"],
    })
    assert h.startswith("Article 25: Logging")
    assert "In: The EU LED Commentary" in h
    assert "Authors: Veale, Kosta" in h


def test_context_header_journal_uses_publication_title():
    """publicationTitle should appear when bookTitle is absent."""
    h = _build_context_header({
        "title": "Neural Scaling Laws",
        "publicationTitle": "Nature Machine Intelligence",
        "authors": ["Hoffmann"],
    })
    assert "In: Nature Machine Intelligence" in h
    assert "Authors: Hoffmann" in h
    # Venue should not appear twice
    assert h.count("Nature Machine Intelligence") == 1


def test_context_header_no_venue_shows_publisher():
    """When there is no venue, publisher should appear instead."""
    h = _build_context_header({
        "title": "Some Report",
        "publisher": "ENISA",
    })
    assert "Publisher: ENISA" in h


def test_context_header_venue_hides_publisher():
    """When a venue is present, publisher should be suppressed to avoid redundancy."""
    h = _build_context_header({
        "title": "Some Chapter",
        "bookTitle": "Big Handbook",
        "publisher": "Springer",
    })
    assert "Publisher:" not in h


def test_context_header_six_authors_truncated_with_et_al():
    """Six authors should be capped at 3 with 'et al.' appended."""
    h = _build_context_header({
        "title": "Multi-author Work",
        "authors": ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"],
    })
    assert "Authors: Alpha, Beta, Gamma et al." in h
    assert "Delta" not in h


# ---------------------------------------------------------------------------
# chunk_item context-header integration
# ---------------------------------------------------------------------------

def test_ft_cache_chunks_include_full_context_header(tmp_path, monkeypatch):
    """Each ft-cache chunk should start with title + venue + authors."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(
        chunking.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=str(storage)),
    )

    ft_text = "Body " * 500
    _write_ft_cache(storage, "ATT2", ft_text)

    item = {
        "item_key": "X",
        "title": "Article 25: Logging",
        "abstract": "",
        "doi": "",
        "dateModified": "2025-01-01",
        "attachment_key": "ATT2",
        "bookTitle": "The EU LED Commentary",
        "publicationTitle": "",
        "publisher": "",
        "authors": ["Veale", "Kosta", "Boehm"],
    }
    chunks = chunk_item(item)
    assert len(chunks) >= 1
    expected_prefix = (
        "Article 25: Logging\nIn: The EU LED Commentary\nAuthors: Veale, Kosta, Boehm\n\n"
    )
    for c in chunks:
        assert c.text.startswith(expected_prefix)


def test_abstract_chunk_includes_context_header():
    """Abstract-only items should also get the context header."""
    item = {
        "item_key": "Y",
        "title": "Neural Scaling Laws",
        "abstract": "Abstract text here.",
        "doi": "",
        "dateModified": "2025-01-01",
        "attachment_key": "",
        "bookTitle": "",
        "publicationTitle": "Nature Machine Intelligence",
        "publisher": "",
        "authors": ["Hoffmann"],
    }
    chunks = chunk_item(item)
    assert len(chunks) == 1
    assert "In: Nature Machine Intelligence" in chunks[0].text
    assert "Authors: Hoffmann" in chunks[0].text
    assert "Abstract text here." in chunks[0].text
