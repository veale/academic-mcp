"""Tests for cross_reranker.rerank()."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import academic_mcp.cross_reranker as cr_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk(item_key, title="", snippet="", score=0.5):
    return {
        "item_key": item_key,
        "title": title,
        "snippet": snippet,
        "char_start": 0,
        "char_end": 100,
        "chunk_source": "abstract",
        "chunk_idx": 0,
        "chunk_count": 1,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_sorts_by_rerank_score(monkeypatch):
    """rerank() should re-order chunks by cross-encoder score, descending."""

    class _FakeCE:
        def predict(self, pairs):
            # Return a score equal to the number of words in the passage.
            return [float(len(passage.split())) for _, passage in pairs]

    monkeypatch.setattr(cr_mod, "_cross_encoder", _FakeCE())
    monkeypatch.setattr(cr_mod, "_load_attempted", True)

    chunks = [
        _chunk("A", snippet="one word"),
        _chunk("B", snippet="this passage has five words here"),
        _chunk("C", snippet="three words total"),
    ]

    result = await cr_mod.rerank("test query", chunks, top_k=3)

    assert [r["item_key"] for r in result] == ["B", "C", "A"]
    assert all("rerank_score" in r for r in result)


@pytest.mark.asyncio
async def test_rerank_respects_top_k(monkeypatch):
    """rerank() should return at most top_k items."""

    class _FakeCE:
        def predict(self, pairs):
            return [float(i) for i in range(len(pairs))]

    monkeypatch.setattr(cr_mod, "_cross_encoder", _FakeCE())
    monkeypatch.setattr(cr_mod, "_load_attempted", True)

    chunks = [_chunk(str(i)) for i in range(10)]
    result = await cr_mod.rerank("query", chunks, top_k=3)

    assert len(result) == 3


@pytest.mark.asyncio
async def test_rerank_degrades_gracefully_when_model_unavailable(monkeypatch):
    """When the cross-encoder is None, rerank should fall back to bi-encoder scores."""
    monkeypatch.setattr(cr_mod, "_cross_encoder", None)
    monkeypatch.setattr(cr_mod, "_load_attempted", True)

    chunks = [
        _chunk("A", score=0.9),
        _chunk("B", score=0.3),
        _chunk("C", score=0.7),
    ]

    result = await cr_mod.rerank("query", chunks, top_k=2)

    assert len(result) == 2
    # Should be sorted by original score when no reranker is available.
    assert result[0]["item_key"] == "A"
    assert result[1]["item_key"] == "C"
    assert all("rerank_score" in r for r in result)


@pytest.mark.asyncio
async def test_rerank_empty_input_returns_empty(monkeypatch):
    """rerank() with an empty list should return an empty list immediately."""
    monkeypatch.setattr(cr_mod, "_load_attempted", True)
    result = await cr_mod.rerank("query", [], top_k=5)
    assert result == []


@pytest.mark.asyncio
async def test_passage_text_prepends_title_once(monkeypatch):
    """_passage_text should prepend title if not already present in snippet."""
    chunk_with_prefix = {
        "title": "My Paper",
        "snippet": "My Paper\n\nsome content here",
    }
    chunk_without_prefix = {
        "title": "My Paper",
        "snippet": "some content here",
    }

    text_already_has_title = cr_mod._passage_text(chunk_with_prefix)
    text_needs_title = cr_mod._passage_text(chunk_without_prefix)

    # Should not double-prefix.
    assert text_already_has_title.count("My Paper") == 1
    # Should add title.
    assert text_needs_title.startswith("My Paper\n\n")
