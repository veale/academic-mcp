"""core.in_corpus — ranked passage retrieval across many cached articles.

``search_in_article`` answers "where in this paper is X discussed?".
``batch_search`` answers "which of these papers mention X, and how often?".
Neither answers the question a literature review actually asks: *"across these
twenty papers, show me the passages that discuss X, best first."*

This module does that. Every paper is cut into overlapping word windows and
all the windows are pooled into a **single** BM25 corpus. Pooling is the whole
trick:

* Scores become comparable across papers. Indexing each paper separately would
  give each its own IDF, so a score of 4.1 in one paper would mean something
  different from 4.1 in another, and ranking papers against each other would be
  meaningless.
* IDF stops collapsing. In a single-paper corpus a term that appears in every
  window has zero inverse document frequency and therefore zero score — the
  focused paper on your topic would score exactly as badly as one that never
  mentions it.

The result: a paper with one strong, on-topic passage outranks one that
mentions the term twenty times in passing.

Only papers already in the text cache are searched. Fetching is the caller's
job (``batch_sections`` / ``fetch_fulltext``); silently pulling twenty PDFs
because someone ran a search would be a nasty surprise.
"""

from __future__ import annotations

import asyncio
import logging
import re

from pydantic import BaseModel, Field

from .in_article import build_windows, _clamp_to_word_boundary, _section_for_offset

logger = logging.getLogger(__name__)

_MAX_PASSAGES_PER_PAPER = 3
_SNIPPET_CHARS = 700

# Ceiling on pooled windows. At ~150-word stride a 10k-word paper yields ~65
# windows, so this comfortably holds a few hundred papers before we start
# dropping the tail of the corpus.
_MAX_WINDOWS = 20_000

_TOKEN_SPLIT = re.compile(r"\W+")


class CorpusPassage(BaseModel):
    char_start: int
    char_end: int
    snippet: str
    section: str | None = None
    score: float = 0.0


class CorpusPaper(BaseModel):
    doi: str
    title: str | None = None
    word_count: int = 0
    # Best passage score in this paper — what papers are ranked on.
    score: float = 0.0
    total_hits: int = 0  # literal occurrences of any query term
    passages: list[CorpusPassage] = Field(default_factory=list)


class InCorpusResult(BaseModel):
    query: str
    papers: list[CorpusPaper] = Field(default_factory=list)
    searched: int = 0
    not_cached: list[str] = Field(default_factory=list)
    truncated: bool = False


def _tokenize(query: str) -> list[str]:
    return [t.lower() for t in _TOKEN_SPLIT.split(query) if t]


def _rank_blocking(
    articles: list[tuple[str, object]],
    query: str,
    max_passages: int,
) -> tuple[list[CorpusPaper], bool]:
    """Pool every article's windows into one BM25 corpus and rank.

    Blocking (BM25 indexing is CPU-bound); call via ``asyncio.to_thread``.
    Returns ``(papers, truncated)``.
    """
    tokens = _tokenize(query)
    if not tokens:
        return [], False

    pooled_tokens: list[list[str]] = []
    # Parallel to pooled_tokens: which paper each window came from, and where.
    origins: list[tuple[str, dict]] = []
    truncated = False

    for doi, cached in articles:
        windows = build_windows(cached.text)
        if not windows:
            continue
        for win in windows:
            if len(pooled_tokens) >= _MAX_WINDOWS:
                truncated = True
                break
            pooled_tokens.append(win["tokens"])
            origins.append((doi, win))
        if truncated:
            break

    if not pooled_tokens:
        return [], truncated

    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        logger.warning("rank_bm25 is not installed; search_in_corpus unavailable")
        return [], truncated

    scores = list(BM25Okapi(pooled_tokens).get_scores(tokens))

    if max(scores, default=0.0) <= 0:
        # Every window contains the terms (or none does). IDF has collapsed, so
        # fall back to raw term frequency — still one scale across all papers.
        scores = [
            float(sum(win_tokens.count(t) for t in tokens))
            for win_tokens in pooled_tokens
        ]
        if max(scores, default=0.0) <= 0:
            return [], truncated  # the terms are genuinely absent everywhere

    by_doi: dict[str, list[tuple[float, dict]]] = {}
    for score, (doi, win) in zip(scores, origins):
        if score > 0:
            by_doi.setdefault(doi, []).append((score, win))

    cached_by_doi = dict(articles)
    papers: list[CorpusPaper] = []

    for doi, scored_windows in by_doi.items():
        cached = cached_by_doi[doi]
        text = cached.text
        sections = cached.sections or []
        scored_windows.sort(key=lambda sw: sw[0], reverse=True)

        passages: list[CorpusPassage] = []
        for score, win in scored_windows[:max_passages]:
            centre = (win["start"] + win["end"]) // 2
            start = max(0, centre - _SNIPPET_CHARS // 2)
            end = min(len(text), centre + _SNIPPET_CHARS // 2)
            start, end = _clamp_to_word_boundary(text, start, end)
            passages.append(CorpusPassage(
                char_start=start,
                char_end=end,
                snippet=text[start:end],
                section=_section_for_offset(sections, start),
                score=score,
            ))

        lowered = text.lower()
        papers.append(CorpusPaper(
            doi=doi,
            title=(cached.metadata or {}).get("title") or None,
            word_count=cached.word_count,
            score=passages[0].score,
            total_hits=sum(lowered.count(t) for t in tokens),
            passages=passages,
        ))

    papers.sort(key=lambda p: p.score, reverse=True)
    return papers, truncated


async def search_in_corpus(
    query: str,
    dois: list[str],
    limit: int = 10,
    max_passages: int = _MAX_PASSAGES_PER_PAPER,
) -> InCorpusResult:
    """Rank *dois* by how well their best passages answer *query*.

    Papers that aren't in the text cache are reported in ``not_cached`` rather
    than fetched.
    """
    from .. import text_cache

    wanted = [d.strip() for d in dois if d and d.strip()]
    if not wanted:
        return InCorpusResult(query=query)

    articles: list[tuple[str, object]] = []
    missing: list[str] = []
    for doi in wanted:
        cached = text_cache.get_cached(doi)
        if cached and (cached.text or "").strip():
            articles.append((doi, cached))
        else:
            missing.append(doi)

    if not articles:
        return InCorpusResult(query=query, not_cached=missing)

    try:
        papers, truncated = await asyncio.to_thread(
            _rank_blocking, articles, query, max_passages
        )
    except Exception as e:
        logger.warning("Corpus ranking failed: %s", e)
        papers, truncated = [], False

    return InCorpusResult(
        query=query,
        papers=papers[:limit],
        searched=len(articles),
        not_cached=missing,
        truncated=truncated,
    )
