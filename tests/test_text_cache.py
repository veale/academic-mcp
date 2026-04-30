"""Tests for text_cache.py backwards compatibility and new path fields."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


def _make_legacy_article(tmp_path: Path) -> tuple[str, Path]:
    """Write a .article.json without pdf_path/html_path (pre-migration format)."""
    key = "aabbccdd" * 8  # 64-char hex key
    data = {
        "doi": "10.1234/test",
        "text": "some text",
        "source": "pdf",
        "sections": [],
        "section_detection": "unknown",
        "word_count": 2,
        "metadata": {},
        "cached_at": "2024-01-01T00:00:00+00:00",
    }
    path = tmp_path / f"{key}.article.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return key, path


def test_legacy_article_loads_without_error(tmp_path):
    """Old .article.json files without pdf_path/html_path must load cleanly."""
    key, _ = _make_legacy_article(tmp_path)

    from academic_mcp import text_cache as tc

    with patch.object(tc.config, "pdf_cache_dir", tmp_path):
        art = tc.load_by_cache_key(key)

    assert art is not None
    assert art.pdf_path is None
    assert art.html_path is None
    assert art.doi == "10.1234/test"


def test_update_paths_writes_fields(tmp_path):
    """update_paths should persist pdf_path and html_path into the JSON."""
    key, path = _make_legacy_article(tmp_path)
    fake_pdf = str(tmp_path / "doc.pdf")
    fake_html = str(tmp_path / "doc.html")

    from academic_mcp import text_cache as tc

    with patch.object(tc.config, "pdf_cache_dir", tmp_path):
        tc.update_paths(key, pdf_path=fake_pdf, html_path=fake_html)
        art = tc.load_by_cache_key(key)

    assert art is not None
    assert art.pdf_path == fake_pdf
    assert art.html_path == fake_html


def test_update_paths_removes_sidecar(tmp_path):
    """update_paths should delete any legacy .paths.json sidecar."""
    key, _ = _make_legacy_article(tmp_path)
    sidecar = tmp_path / f"{key}.paths.json"
    sidecar.write_text('{"pdf_path": "/old/path.pdf"}', encoding="utf-8")

    from academic_mcp import text_cache as tc

    with patch.object(tc.config, "pdf_cache_dir", tmp_path):
        tc.update_paths(key, pdf_path="/new/path.pdf")

    assert not sidecar.exists()


def test_update_paths_noop_if_not_cached(tmp_path):
    """update_paths on a missing cache key should silently do nothing."""
    from academic_mcp import text_cache as tc

    with patch.object(tc.config, "pdf_cache_dir", tmp_path):
        # Should not raise
        tc.update_paths("nonexistent" * 4, pdf_path="/some/path.pdf")
