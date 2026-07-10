"""Tests for the search pipeline's fusion, dedup, and diagnostics helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp.core import search as core_search


# ---------------------------------------------------------------------------
# Reciprocal-rank fusion
# ---------------------------------------------------------------------------

def test_rrf_single_list_preserves_order():
    fused = core_search.rrf_fuse([["a", "b", "c"]])
    assert sorted(fused, key=lambda k: fused[k], reverse=True) == ["a", "b", "c"]


def test_rrf_rewards_agreement_across_lists():
    """An item ranked 2nd by both retrievers beats one ranked 1st by only one."""
    lexical = ["only_lexical", "agreed"]
    semantic = ["only_semantic", "agreed"]
    fused = core_search.rrf_fuse([lexical, semantic])
    ranked = sorted(fused, key=lambda k: fused[k], reverse=True)
    assert ranked[0] == "agreed"


def test_rrf_k_damps_deep_ranks():
    fused = core_search.rrf_fuse([["a"] + [f"x{i}" for i in range(100)]])
    assert fused["a"] > fused["x99"]
    # With k=60 the 100th rank still contributes something, just very little.
    assert fused["x99"] > 0


def test_rrf_empty_input():
    assert core_search.rrf_fuse([]) == {}
    assert core_search.rrf_fuse([[]]) == {}


# ---------------------------------------------------------------------------
# Title normalisation for dedup
# ---------------------------------------------------------------------------

def test_normalize_title_strips_punctuation_and_case():
    a = core_search.normalize_title("The Rise of Algorithmic Accountability: A Review!")
    b = core_search.normalize_title("the rise of algorithmic accountability - a review")
    assert a == b == "the rise of algorithmic accountability a review"


def test_normalize_title_ignores_short_generic_titles():
    """Short titles are too generic to dedup on and must return ''."""
    for generic in ("Introduction", "Discussion", "Chapter One", "Notes on Method"):
        assert core_search.normalize_title(generic) == ""


def test_normalize_title_handles_none_and_empty():
    assert core_search.normalize_title(None) == ""
    assert core_search.normalize_title("") == ""


def test_normalize_title_collapses_whitespace():
    assert core_search.normalize_title("A  Very   Long Title  Here Indeed") == (
        "a very long title here indeed"
    )


# ---------------------------------------------------------------------------
# DOI quality ranking
# ---------------------------------------------------------------------------

def test_doi_rank_orders_published_above_preprint_above_shortdoi():
    published = core_search.doi_rank("10.1145/3593013.3594073")
    preprint = core_search.doi_rank("10.31235/osf.io/p4sey")
    short = core_search.doi_rank("https://doi.org/gsb98p")
    none = core_search.doi_rank(None)
    assert published > preprint > short > none


@pytest.mark.parametrize("doi", [
    "10.48550/arXiv.2307.16787",
    "10.2139/ssrn.2972855",
    "10.1101/2020.01.01.123456",
    "10.31234/osf.io/abcde",
])
def test_preprint_dois_recognised(doi):
    assert core_search._is_preprint_doi(doi)
    assert core_search.doi_rank(doi) == 2


def test_doi_rank_handles_url_prefixed_published_doi():
    assert core_search.doi_rank("https://doi.org/10.1145/3593013") == 3


# ---------------------------------------------------------------------------
# Library duplicate collapsing
# ---------------------------------------------------------------------------

def _hit(title, doi=None, zkey=None):
    from academic_mcp.core.types import SearchHit
    return SearchHit(title=title, doi=doi, zotero_key=zkey, in_zotero=True)


_TITLE = "Understanding Accountability in Algorithmic Supply Chains"


def test_dedupe_collapses_same_paper_filed_under_three_dois():
    """The real case: one paper, a preprint DOI, a shortDOI, a published DOI."""
    hits = [
        _hit(_TITLE, "10.31235/osf.io/p4sey", "AAAA1111"),
        _hit(_TITLE.lower(), "https://doi.org/gsb98p", "BBBB2222"),
        _hit(_TITLE.lower(), "10.1145/3593013.3594073", "CCCC3333"),
    ]
    out = core_search.dedupe_library_hits(hits)
    assert len(out) == 1
    # The published DOI is the handle worth keeping.
    assert out[0].doi == "10.1145/3593013.3594073"
    assert out[0].zotero_key == "CCCC3333"


def test_dedupe_keeps_first_rank_position():
    hits = [
        _hit("A Wholly Different Paper Title Here", "10.1/a"),
        _hit(_TITLE, "10.31235/osf.io/p4sey"),
        _hit(_TITLE, "10.1145/x"),
    ]
    out = core_search.dedupe_library_hits(hits)
    assert [h.title for h in out] == ["A Wholly Different Paper Title Here", _TITLE]
    assert out[1].doi == "10.1145/x"  # best DOI, but still in position 1


def test_dedupe_prefers_any_doi_over_none():
    hits = [_hit(_TITLE), _hit(_TITLE, "10.1145/x")]
    out = core_search.dedupe_library_hits(hits)
    assert len(out) == 1 and out[0].doi == "10.1145/x"


def test_dedupe_keeps_distinct_papers():
    hits = [
        _hit("The First Distinctive Paper Title", "10.1/a"),
        _hit("The Second Distinctive Paper Title", "10.1/b"),
    ]
    assert len(core_search.dedupe_library_hits(hits)) == 2


def test_dedupe_passes_through_generic_short_titles():
    """Titles too short to be distinctive must never be merged."""
    hits = [_hit("Introduction", "10.1/a"), _hit("Introduction", "10.1/b")]
    assert len(core_search.dedupe_library_hits(hits)) == 2


def test_dedupe_empty():
    assert core_search.dedupe_library_hits([]) == []
