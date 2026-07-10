"""Tests for citation-graph related-work discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp.core import discover


# Graph used throughout:
#   Seeds:      S1, S2
#   S1 cites:   FOUNDATION, OLD1
#   S2 cites:   FOUNDATION, OLD2        -> FOUNDATION shared by both seeds
#   SURVEY cites S1, S2, COCITED        -> cites both seeds (shared citer)
#   MINOR  cites S1, COCITED            -> cites one seed
_REFS = {
    "S1": ["FOUNDATION", "OLD1"],
    "S2": ["FOUNDATION", "OLD2"],
}
_CITERS = {
    "S1": [("SURVEY", ["S1", "S2", "COCITED"]), ("MINOR", ["S1", "COCITED"])],
    "S2": [("SURVEY", ["S1", "S2", "COCITED"])],
}

_TITLES = {
    "FOUNDATION": "The Foundational Paper",
    "OLD1": "An Older Work One",
    "OLD2": "An Older Work Two",
    "SURVEY": "A Survey of Everything",
    "MINOR": "A Minor Follow-up",
    "COCITED": "The Co-cited Classic",
}


@pytest.fixture
def fake_openalex(monkeypatch):
    from academic_mcp import apis, zotero

    async def _openalex_work(doi):
        mapping = {"10.1/s1": "S1", "10.1/s2": "S2"}
        wid = mapping.get(doi)
        return {"id": f"https://openalex.org/{wid}"} if wid else None

    async def _referenced_works(work_id):
        return _REFS.get(work_id, [])

    async def _citing_works_refs(work_id, sample=50):
        return _CITERS.get(work_id, [])

    async def _works_by_ids(ids, select=None):
        return [
            {
                "id": f"https://openalex.org/{i}",
                "doi": f"https://doi.org/10.1/{i.lower()}",
                "title": _TITLES.get(i, i),
                "publication_year": 2015,
                "authorships": [{"author": {"display_name": "A Person"}}],
                "cited_by_count": 10,
                "primary_location": {"source": {"display_name": "A Journal"}},
            }
            for i in ids
        ]

    async def _doi_index():
        return {}

    monkeypatch.setattr(apis, "openalex_work", _openalex_work)
    monkeypatch.setattr(apis, "openalex_referenced_works", _referenced_works)
    monkeypatch.setattr(apis, "openalex_citing_works_refs", _citing_works_refs)
    monkeypatch.setattr(apis, "openalex_works_by_ids", _works_by_ids)
    monkeypatch.setattr(zotero, "get_doi_index", _doi_index)


@pytest.mark.asyncio
async def test_requires_a_seed():
    result = await discover.discover_related([])
    assert result.error and "seed" in result.error.lower()


@pytest.mark.asyncio
async def test_unresolvable_seeds_reported(monkeypatch, fake_openalex):
    result = await discover.discover_related(["10.1/nonexistent"])
    assert result.unresolved_seeds == ["10.1/nonexistent"]
    assert result.error is not None


@pytest.mark.asyncio
async def test_shared_reference_counted_once_per_seed(fake_openalex):
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    by_id = {i.openalex_id: i for i in result.items}

    # FOUNDATION is cited by both seeds; OLD1 by only one.
    assert by_id["FOUNDATION"].shared_references == 2
    assert by_id["OLD1"].shared_references == 1


@pytest.mark.asyncio
async def test_shared_citers_counts_seeds_cited_by_the_citer(fake_openalex):
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    by_id = {i.openalex_id: i for i in result.items}

    # SURVEY cites both seeds; MINOR cites one.
    assert by_id["SURVEY"].shared_citers == 2
    assert by_id["MINOR"].shared_citers == 1


@pytest.mark.asyncio
async def test_citer_seen_via_two_seeds_is_not_double_counted(fake_openalex):
    """SURVEY is returned by both seeds' citer queries; its counts must not double."""
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    by_id = {i.openalex_id: i for i in result.items}
    assert by_id["SURVEY"].shared_citers == 2  # not 4
    # COCITED appears in SURVEY's bibliography once and MINOR's once.
    assert by_id["COCITED"].cocitations == 2


@pytest.mark.asyncio
async def test_seeds_are_never_returned_as_results(fake_openalex):
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    assert "S1" not in {i.openalex_id for i in result.items}
    assert "S2" not in {i.openalex_id for i in result.items}


@pytest.mark.asyncio
async def test_foundation_outranks_a_single_seed_reference(fake_openalex):
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    ranked = [i.openalex_id for i in result.items]
    assert ranked.index("FOUNDATION") < ranked.index("OLD1")


@pytest.mark.asyncio
async def test_exclude_dois_filters_results(fake_openalex):
    result = await discover.discover_related(
        ["10.1/s1", "10.1/s2"], exclude_dois=["10.1/foundation"]
    )
    assert "FOUNDATION" not in {i.openalex_id for i in result.items}


@pytest.mark.asyncio
async def test_limit_is_respected(fake_openalex):
    result = await discover.discover_related(["10.1/s1", "10.1/s2"], limit=2)
    assert len(result.items) == 2


@pytest.mark.asyncio
async def test_in_zotero_flagged(monkeypatch, fake_openalex):
    from academic_mcp import zotero

    async def _doi_index():
        return {"10.1/foundation": {"item_key": "AAAA1111"}}

    monkeypatch.setattr(zotero, "get_doi_index", _doi_index)
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    by_id = {i.openalex_id: i for i in result.items}
    assert by_id["FOUNDATION"].in_zotero is True
    assert by_id["OLD1"].in_zotero is False


@pytest.mark.asyncio
async def test_reasons_are_human_readable(fake_openalex):
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    by_id = {i.openalex_id: i for i in result.items}
    assert "cited by 2 seed(s)" in by_id["FOUNDATION"].reasons()
    assert "cites 2 seed(s)" in by_id["SURVEY"].reasons()
    assert any("co-cited" in r for r in by_id["COCITED"].reasons())


@pytest.mark.asyncio
async def test_api_failure_on_one_seed_does_not_abort(monkeypatch, fake_openalex):
    from academic_mcp import apis

    async def _boom(work_id):
        if work_id == "S2":
            raise RuntimeError("openalex down")
        return _REFS.get(work_id, [])

    monkeypatch.setattr(apis, "openalex_referenced_works", _boom)
    result = await discover.discover_related(["10.1/s1", "10.1/s2"])
    assert result.error is None
    assert result.items  # S1's signals still came through
