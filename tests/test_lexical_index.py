"""Tests for the FTS5 lexical index over the Zotero library."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import lexical_index as lx
from academic_mcp.config import config


def _item(key, title, abstract="", authors=(), tags=(), venue="", modified="2024-01-01"):
    return {
        "item_key": key,
        "title": title,
        "abstract": abstract,
        "venue": venue,
        "authors": list(authors),
        "tags": list(tags),
        "doi": "",
        "item_type": "journalArticle",
        "dateModified": modified,
        "attachment_key": "",
    }


@pytest.fixture
def fts_db(tmp_path, monkeypatch):
    """Point the index at a temp dir. ``_db_path`` derives from pdf_cache_dir.parent."""
    cache_dir = tmp_path / "cache" / "pdfs"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "pdf_cache_dir", cache_dir)
    monkeypatch.setattr(config, "lexical_index_enabled", True)
    monkeypatch.setattr(config, "fts_fulltext_max_chars", 0)
    return tmp_path / "cache" / "zotero-fts.sqlite"


# ---------------------------------------------------------------------------
# Query translation
# ---------------------------------------------------------------------------

def test_build_match_query_ands_bare_terms():
    assert lx.build_match_query("gdpr privacy") == '"gdpr" AND "privacy"'


def test_build_match_query_maps_field_prefixes():
    assert lx.build_match_query("author:Veale") == 'authors:"Veale"'
    assert lx.build_match_query("creator:Veale") == 'authors:"Veale"'
    assert lx.build_match_query("title:algorithms") == 'title:"algorithms"'
    assert lx.build_match_query("tag:law") == 'tags:"law"'


def test_build_match_query_preserves_phrases():
    assert lx.build_match_query('"right to explanation"') == '"right to explanation"'


def test_build_match_query_strips_fts_operators():
    """A stray operator must not be able to change the query's meaning."""
    out = lx.build_match_query("foo* (bar) ^baz")
    assert "*" not in out and "(" not in out and "^" not in out
    assert out == '"foo" AND "bar" AND "baz"'


def test_build_match_query_empty():
    assert lx.build_match_query("") == ""
    assert lx.build_match_query("   ") == ""


def test_unknown_prefix_is_not_treated_as_a_column():
    # 'doi:' is not an index column. The colon is FTS5 column syntax, so it is
    # stripped and both halves are searched as one phrase rather than being
    # passed through to become `doi:...` (which FTS5 would reject).
    assert lx.build_match_query("doi:10.1000/x") == '"doi 10.1000/x"'


# ---------------------------------------------------------------------------
# Sync + search
# ---------------------------------------------------------------------------

def test_sync_and_search_roundtrip(fts_db):
    items = [
        _item("AAAA1111", "Algorithmic Accountability and the GDPR",
              abstract="automated decision making under data protection law",
              authors=["Michael Veale"], tags=["gdpr"], venue="Modern Law Review"),
        _item("BBBB2222", "Honeybee Magnetoreception During Navigation",
              abstract="geomagnetic cues guide foraging bees",
              authors=["Ada Lovelace"], venue="J Exp Biol"),
    ]
    result = lx._sync_blocking(items, force_rebuild=True)
    assert result["count"] == 2
    assert result["indexed"] == 2

    assert [k for k, _ in lx._search_blocking("magnetoreception", 5)] == ["BBBB2222"]
    assert [k for k, _ in lx._search_blocking("author:Veale", 5)] == ["AAAA1111"]
    assert [k for k, _ in lx._search_blocking("tag:gdpr", 5)] == ["AAAA1111"]
    assert lx._search_blocking("nonexistentword", 5) == []


def test_search_ranks_title_matches_above_abstract_matches(fts_db):
    items = [
        _item("TITLE001", "Federated Learning at Scale", abstract="unrelated body text"),
        _item("ABSTR001", "Something Else Entirely",
              abstract="a passing mention of federated learning in the middle"),
    ]
    # Pad the corpus so BM25's IDF term isn't degenerate on 2 documents.
    items += [_item(f"PAD{i:05d}", f"Padding paper {i}", abstract="filler") for i in range(30)]
    lx._sync_blocking(items, force_rebuild=True)

    hits = lx._search_blocking("federated learning", 5)
    assert hits[0][0] == "TITLE001"
    assert hits[0][1] > hits[1][1]


def test_scores_are_positive_and_descending(fts_db):
    items = [_item(f"K{i:05d}", f"Paper {i}", abstract="common filler text") for i in range(30)]
    items.append(_item("RARE0001", "Xylophone Quokka Zeppelin",
                       abstract="xylophone quokka zeppelin xylophone"))
    lx._sync_blocking(items, force_rebuild=True)

    hits = lx._search_blocking("xylophone", 5)
    assert hits[0][0] == "RARE0001"
    assert all(score > 0 for _, score in hits)
    assert hits == sorted(hits, key=lambda h: h[1], reverse=True)


def test_incremental_sync_skips_unchanged(fts_db):
    items = [_item("AAAA1111", "Stable Title", modified="2024-01-01")]
    lx._sync_blocking(items, force_rebuild=True)

    again = lx._sync_blocking(items, force_rebuild=False)
    assert again["indexed"] == 0
    assert again["deleted"] == 0
    assert again["count"] == 1


def test_incremental_sync_reindexes_changed_item(fts_db):
    items = [_item("AAAA1111", "Original Title", modified="2024-01-01")]
    lx._sync_blocking(items, force_rebuild=True)

    items[0]["title"] = "Rewritten Title"
    items[0]["dateModified"] = "2025-06-01"
    result = lx._sync_blocking(items, force_rebuild=False)

    assert result["indexed"] == 1
    assert result["count"] == 1
    assert [k for k, _ in lx._search_blocking("rewritten", 5)] == ["AAAA1111"]
    assert lx._search_blocking("original", 5) == []


def test_incremental_sync_removes_deleted_items(fts_db):
    items = [_item("AAAA1111", "Kept Paper"), _item("BBBB2222", "Deleted Paper")]
    lx._sync_blocking(items, force_rebuild=True)

    result = lx._sync_blocking(items[:1], force_rebuild=False)
    assert result["stale_removed"] == 1
    assert result["count"] == 1
    assert lx._search_blocking("deleted", 5) == []


@pytest.mark.parametrize("query", ['quotation " marks', '"', '" ', "AND OR NOT", "*", "((("])
def test_malformed_query_never_raises(fts_db, query):
    """User queries reach MATCH; a stray operator must not be a hard error."""
    lx._sync_blocking([_item("AAAA1111", "Quotation Marks in Titles")], force_rebuild=True)
    result = lx._search_blocking(query, 5)
    assert isinstance(result, list)


def test_unbalanced_quote_still_matches_its_terms(fts_db):
    lx._sync_blocking([_item("AAAA1111", "Quotation Marks in Titles")], force_rebuild=True)
    assert [k for k, _ in lx._search_blocking('quotation " marks', 5)] == ["AAAA1111"]


def test_available_false_before_sync(fts_db, monkeypatch):
    assert not lx.available()
    lx._sync_blocking([_item("AAAA1111", "Some Paper")], force_rebuild=True)
    assert lx.available()

    monkeypatch.setattr(config, "lexical_index_enabled", False)
    assert not lx.available()


def test_fulltext_indexed_when_cap_allows(fts_db, tmp_path, monkeypatch):
    from academic_mcp import zotero_sqlite

    storage = tmp_path / "storage"
    (storage / "ATTACH01").mkdir(parents=True)
    (storage / "ATTACH01" / ".zotero-ft-cache").write_text(
        "the body mentions supercalifragilistic exactly once", encoding="utf-8"
    )
    monkeypatch.setattr(zotero_sqlite.sqlite_config, "storage_path", str(storage))
    monkeypatch.setattr(config, "fts_fulltext_max_chars", 10_000)

    item = _item("AAAA1111", "A Title With No Body Words")
    item["attachment_key"] = "ATTACH01"
    lx._sync_blocking([item], force_rebuild=True)

    assert [k for k, _ in lx._search_blocking("supercalifragilistic", 5)] == ["AAAA1111"]


def test_fulltext_skipped_when_cap_is_zero(fts_db, tmp_path, monkeypatch):
    from academic_mcp import zotero_sqlite

    storage = tmp_path / "storage"
    (storage / "ATTACH01").mkdir(parents=True)
    (storage / "ATTACH01" / ".zotero-ft-cache").write_text("supercalifragilistic", encoding="utf-8")
    monkeypatch.setattr(zotero_sqlite.sqlite_config, "storage_path", str(storage))
    monkeypatch.setattr(config, "fts_fulltext_max_chars", 0)

    item = _item("AAAA1111", "A Title With No Body Words")
    item["attachment_key"] = "ATTACH01"
    lx._sync_blocking([item], force_rebuild=True)

    assert lx._search_blocking("supercalifragilistic", 5) == []


@pytest.mark.asyncio
async def test_search_returns_empty_when_index_absent(fts_db):
    assert await lx.search("anything") == []
