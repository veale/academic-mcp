"""Tests for ranked passage retrieval across many cached articles."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import text_cache
from academic_mcp.config import config
from academic_mcp.core import in_corpus


_ON_TOPIC = (
    "We evaluate how fairness metrics trade off against predictive accuracy. "
    "Enforcing demographic parity reduces accuracy on the minority subgroup, "
    "and equalised odds imposes a similar cost. This accuracy-fairness "
    "trade-off is fundamental and cannot be engineered away. " * 6
)
_PASSING_MENTION = (
    "This paper concerns compiler optimisation for vector hardware. "
    "Loop unrolling and vectorisation dominate the runtime profile. " * 12
    + "Fairness is mentioned once here. "
    + "Back to compilers and instruction scheduling and register allocation. " * 12
)
_UNRELATED = "Honeybee foraging patterns respond to geomagnetic cues. " * 30


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "pdfs"
    d.mkdir(parents=True)
    monkeypatch.setattr(config, "pdf_cache_dir", d)
    return d


def _cache(doi: str, text: str, title: str = "", sections=None):
    return text_cache.put_cached(
        doi=doi,
        text=text,
        source="test",
        sections=sections or [],
        metadata={"title": title} if title else {},
    )


@pytest.mark.asyncio
async def test_empty_dois_returns_empty(cache_dir):
    result = await in_corpus.search_in_corpus("anything", [])
    assert result.papers == []
    assert result.searched == 0


@pytest.mark.asyncio
async def test_uncached_dois_reported_not_fetched(cache_dir):
    result = await in_corpus.search_in_corpus("fairness", ["10.1/missing"])
    assert result.not_cached == ["10.1/missing"]
    assert result.papers == []
    assert result.searched == 0


@pytest.mark.asyncio
async def test_ranks_focused_paper_above_passing_mention(cache_dir):
    _cache("10.1/ontopic", _ON_TOPIC, title="Fairness Trade-offs")
    _cache("10.1/passing", _PASSING_MENTION, title="Compiler Optimisation")

    result = await in_corpus.search_in_corpus(
        "how fairness metrics trade off against accuracy",
        ["10.1/ontopic", "10.1/passing"],
    )
    assert [p.doi for p in result.papers][0] == "10.1/ontopic"
    assert result.searched == 2


@pytest.mark.asyncio
async def test_unrelated_paper_scores_zero_and_is_dropped(cache_dir):
    _cache("10.1/ontopic", _ON_TOPIC)
    _cache("10.1/unrelated", _UNRELATED)

    result = await in_corpus.search_in_corpus(
        "fairness accuracy trade-off", ["10.1/ontopic", "10.1/unrelated"]
    )
    dois = [p.doi for p in result.papers]
    assert "10.1/ontopic" in dois
    assert "10.1/unrelated" not in dois


@pytest.mark.asyncio
async def test_passages_carry_offsets_and_snippets(cache_dir):
    _cache("10.1/ontopic", _ON_TOPIC)
    result = await in_corpus.search_in_corpus("demographic parity", ["10.1/ontopic"])

    paper = result.papers[0]
    assert paper.passages
    passage = paper.passages[0]
    assert passage.char_start < passage.char_end <= len(_ON_TOPIC)
    assert passage.snippet
    assert passage.score > 0
    # Offsets must actually address the snippet in the source text.
    assert _ON_TOPIC[passage.char_start:passage.char_end] == passage.snippet


@pytest.mark.asyncio
async def test_max_passages_respected(cache_dir):
    _cache("10.1/ontopic", _ON_TOPIC)
    result = await in_corpus.search_in_corpus(
        "fairness accuracy", ["10.1/ontopic"], max_passages=1
    )
    assert len(result.papers[0].passages) == 1


@pytest.mark.asyncio
async def test_limit_caps_papers(cache_dir):
    for i in range(5):
        _cache(f"10.1/p{i}", _ON_TOPIC)
    result = await in_corpus.search_in_corpus(
        "fairness accuracy", [f"10.1/p{i}" for i in range(5)], limit=2
    )
    assert len(result.papers) == 2
    assert result.searched == 5


@pytest.mark.asyncio
async def test_paper_score_is_its_best_passage_score(cache_dir):
    _cache("10.1/ontopic", _ON_TOPIC)
    result = await in_corpus.search_in_corpus("fairness accuracy", ["10.1/ontopic"])
    paper = result.papers[0]
    assert paper.score == paper.passages[0].score
    assert paper.score == max(p.score for p in paper.passages)


@pytest.mark.asyncio
async def test_section_attributed_when_sections_present(cache_dir):
    sections = [
        {"title": "Introduction", "start": 0, "end": 100, "keywords": []},
        {"title": "Results", "start": 100, "end": len(_ON_TOPIC), "keywords": []},
    ]
    _cache("10.1/sec", _ON_TOPIC, sections=sections)
    result = await in_corpus.search_in_corpus("demographic parity", ["10.1/sec"])
    assert result.papers[0].passages[0].section in ("Introduction", "Results")


@pytest.mark.asyncio
async def test_total_hits_counts_literal_occurrences(cache_dir):
    _cache("10.1/ontopic", _ON_TOPIC)
    result = await in_corpus.search_in_corpus("fairness", ["10.1/ontopic"])
    assert result.papers[0].total_hits == _ON_TOPIC.lower().count("fairness")


@pytest.mark.asyncio
async def test_mixed_cached_and_uncached(cache_dir):
    _cache("10.1/ontopic", _ON_TOPIC)
    result = await in_corpus.search_in_corpus(
        "fairness", ["10.1/ontopic", "10.1/missing"]
    )
    assert result.searched == 1
    assert result.not_cached == ["10.1/missing"]
    assert len(result.papers) == 1
