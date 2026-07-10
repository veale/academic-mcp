"""Integration tests for search_papers' cross-source merge and dedup.

All network sources are stubbed; this exercises the merge policy, not the APIs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp.core import search as core_search

_TITLE = "Understanding Accountability in Algorithmic Supply Chains"


@pytest.fixture
def stub_sources(monkeypatch):
    """Neutralise every source; individual tests re-stub what they need."""
    from academic_mcp import apis, lexical_index, zotero
    from academic_mcp.config import config
    from academic_mcp.core import background

    monkeypatch.setattr(config, "semantic_default_on", False)
    monkeypatch.setattr(config, "prewarm_enabled", False)
    monkeypatch.setattr(config, "reranker_primary", "none")
    monkeypatch.setattr(config, "reranker_fallback", "none")
    monkeypatch.setattr(config, "primo_domain", "")
    monkeypatch.setattr(config, "primo_vid", "")

    monkeypatch.setattr(lexical_index, "available", lambda: False)
    monkeypatch.setattr(background, "prewarm_articles", lambda hits: None)

    async def _no_zotero(query, limit=10, start_year=None, end_year=None):
        return []

    async def _empty_index():
        return {}

    async def _no_s2(query, limit=10, **kw):
        return {"data": []}

    async def _no_oa(query, limit=10, **kw):
        return {"results": []}

    monkeypatch.setattr(zotero, "search_zotero", _no_zotero)
    monkeypatch.setattr(zotero, "get_doi_index", _empty_index)
    monkeypatch.setattr(apis, "s2_search", _no_s2)
    monkeypatch.setattr(apis, "openalex_search", _no_oa)


def _s2_paper(title, doi=None, **kw):
    paper = {"title": title, "authors": [], "abstract": None, "citationCount": 1}
    if doi:
        paper["externalIds"] = {"DOI": doi}
    paper.update(kw)
    return paper


def _oa_work(title, doi=None, **kw):
    work = {
        "title": title, "authorships": [], "cited_by_count": 2,
        "publication_year": 2023, "primary_location": {},
    }
    if doi:
        work["doi"] = f"https://doi.org/{doi}"
    work.update(kw)
    return work


@pytest.mark.asyncio
async def test_same_doi_from_two_sources_merges(stub_sources, monkeypatch):
    from academic_mcp import apis

    async def _s2(query, limit=10, **kw):
        return {"data": [_s2_paper(_TITLE, "10.1145/x")]}

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work(_TITLE, "10.1145/x")]}

    monkeypatch.setattr(apis, "s2_search", _s2)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    results = await core_search.search_papers("q", limit=5)
    assert len(results) == 1
    assert set(results[0].found_in) == {"semantic_scholar", "openalex"}


@pytest.mark.asyncio
async def test_preprint_and_published_doi_merge_keeping_published(stub_sources, monkeypatch):
    """The dominant real duplicate: arXiv/SSRN preprint vs version of record."""
    from academic_mcp import apis

    async def _s2(query, limit=10, **kw):
        return {"data": [_s2_paper(_TITLE, "10.48550/arXiv.2307.16787")]}

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work(_TITLE, "10.1145/3593013.3594073")]}

    monkeypatch.setattr(apis, "s2_search", _s2)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    results = await core_search.search_papers("q", limit=5)
    assert len(results) == 1
    assert results[0].doi == "10.1145/3593013.3594073"
    assert set(results[0].found_in) == {"semantic_scholar", "openalex"}


@pytest.mark.asyncio
async def test_two_distinct_published_dois_same_title_stay_separate(stub_sources, monkeypatch):
    """An erratum or reprint shares a title but is a distinct work."""
    from academic_mcp import apis

    async def _s2(query, limit=10, **kw):
        return {"data": [_s2_paper(_TITLE, "10.1145/original")]}

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work(_TITLE, "10.1145/erratum")]}

    monkeypatch.setattr(apis, "s2_search", _s2)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    results = await core_search.search_papers("q", limit=5)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_doiless_duplicates_merge_on_title(stub_sources, monkeypatch):
    from academic_mcp import apis

    async def _s2(query, limit=10, **kw):
        return {"data": [_s2_paper(_TITLE)]}

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work(_TITLE)]}

    monkeypatch.setattr(apis, "s2_search", _s2)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    results = await core_search.search_papers("q", limit=5)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_generic_short_titles_never_merge(stub_sources, monkeypatch):
    from academic_mcp import apis

    async def _s2(query, limit=10, **kw):
        return {"data": [_s2_paper("Introduction")]}

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work("Introduction")]}

    monkeypatch.setattr(apis, "s2_search", _s2)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    results = await core_search.search_papers("q", limit=5)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_source_failure_is_isolated(stub_sources, monkeypatch):
    from academic_mcp import apis

    async def _boom(query, limit=10, **kw):
        raise RuntimeError("semantic scholar is down")

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work(_TITLE, "10.1145/x")]}

    monkeypatch.setattr(apis, "s2_search", _boom)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    diag: dict = {}
    results = await core_search.search_papers("q", limit=5, diagnostics=diag)
    assert len(results) == 1
    assert diag["counts"]["semantic_scholar"] == -1  # failure marker
    assert diag["counts"]["openalex"] == 1


@pytest.mark.asyncio
async def test_diagnostics_populated(stub_sources, monkeypatch):
    from academic_mcp import apis

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work(_TITLE, "10.1145/x")]}

    monkeypatch.setattr(apis, "openalex_search", _oa)

    diag: dict = {}
    await core_search.search_papers("q", limit=5, diagnostics=diag)

    assert "total" in diag["timings"]
    assert "openalex" in diag["timings"]
    assert diag["merged"] == 1
    # Total is wall clock, so it cannot be less than the slowest single source.
    assert diag["timings"]["total"] >= diag["timings"]["openalex"]


@pytest.mark.asyncio
async def test_exclude_local_drops_in_zotero_hits(stub_sources, monkeypatch):
    from academic_mcp import apis, zotero

    async def _index():
        return {"10.1145/local": {"item_key": "AAAA1111"}}

    async def _oa(query, limit=10, **kw):
        return {"results": [
            _oa_work("A Locally Held Distinctive Paper", "10.1145/local"),
            _oa_work("An External Distinctive Paper Title", "10.1145/external"),
        ]}

    monkeypatch.setattr(zotero, "get_doi_index", _index)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    results = await core_search.search_papers("q", limit=5, exclude_local=True)
    assert [r.doi for r in results] == ["10.1145/external"]


@pytest.mark.asyncio
async def test_external_hit_flagged_in_zotero(stub_sources, monkeypatch):
    from academic_mcp import apis, zotero

    async def _index():
        return {"10.1145/local": {"item_key": "AAAA1111"}}

    async def _oa(query, limit=10, **kw):
        return {"results": [_oa_work("A Locally Held Distinctive Paper", "10.1145/local")]}

    monkeypatch.setattr(zotero, "get_doi_index", _index)
    monkeypatch.setattr(apis, "openalex_search", _oa)

    results = await core_search.search_papers("q", limit=5)
    assert results[0].in_zotero is True
