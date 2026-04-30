"""core.in_article — BM25 keyword search within a cached article."""

from __future__ import annotations

import re as _re
import logging

from .types import InArticleResult, TermMatch, TermResult

logger = logging.getLogger(__name__)

# Cache BM25 indexes keyed by "{doi}:{text_length}" to avoid rebuilding on
# repeated queries to the same article.
_bm25_cache: dict[str, tuple] = {}


def _build_bm25_index(text: str, window_words: int = 300, stride_words: int = 150):
    """Build a BM25 index over overlapping word-windows of *text*.

    Returns ``(index, windows)`` where ``windows`` is a list of
    ``{"start": int, "end": int, "tokens": list[str]}`` dicts.
    """
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return None, []

    word_spans = [(m.start(), m.end()) for m in _re.finditer(r"\S+", text)]
    if not word_spans:
        return None, []

    windows = []
    i = 0
    while i < len(word_spans):
        end_idx = min(i + window_words, len(word_spans))
        w_start = word_spans[i][0]
        w_end = word_spans[end_idx - 1][1]
        chunk = text[w_start:w_end]
        tokens = [t.lower() for t in _re.split(r"\W+", chunk) if t]
        windows.append({"start": w_start, "end": w_end, "tokens": tokens})
        if end_idx >= len(word_spans):
            break
        i += stride_words

    if not windows:
        return None, []

    corpus = [w["tokens"] for w in windows]
    index = BM25Okapi(corpus)
    return index, windows


def _section_for_offset(sections: list[dict], offset: int) -> str | None:
    for sec in reversed(sections):
        if sec["start"] <= offset:
            return sec.get("title") or (
                ", ".join(sec["keywords"][:3]) if sec.get("keywords") else None
            )
    return None


def _clamp_to_word_boundary(s: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and not s[start - 1].isspace():
        start -= 1
    while end < len(s) and not s[end].isspace():
        end += 1
    return start, end


async def search_in_article(
    doi: str,
    terms: list[str],
    context_chars: int = 500,
    max_matches: int = 3,
) -> InArticleResult:
    """Search for *terms* in a cached article and return structured hit data.

    Raises:
        LookupError: if the article is not in the text cache.
    """
    from .. import text_cache, apis
    from ..config import config

    cached = text_cache.get_cached(doi)

    # SSRN remapping: preprint DOIs sometimes resolve to published DOIs.
    if not cached and doi.startswith("10.2139/ssrn."):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                remap = await apis.resolve_ssrn_doi(doi, client)
            if remap.get("published_doi"):
                cached = text_cache.get_cached(remap["published_doi"])
        except Exception:
            pass

    if not cached:
        raise LookupError(doi)

    text = cached.text
    sections = cached.sections or []

    # Build or retrieve BM25 index.
    cache_key = f"{doi}:{len(text)}"
    if cache_key not in _bm25_cache:
        bm25_index, bm25_windows = _build_bm25_index(text)
        _bm25_cache[cache_key] = (bm25_index, bm25_windows)
        if len(_bm25_cache) > 20:
            oldest = next(iter(_bm25_cache))
            del _bm25_cache[oldest]
    else:
        bm25_index, bm25_windows = _bm25_cache[cache_key]

    n_segments = 10
    seg_len = max(len(text) // n_segments, 1)
    segment_texts = [text[i * seg_len : (i + 1) * seg_len] for i in range(n_segments)]

    max_total_chars = config.max_context_length // 2
    total_chars = 0

    term_results: list[TermResult] = []
    for term in terms[:5]:
        if not term.strip():
            continue

        pat = _re.compile(_re.escape(term), _re.IGNORECASE)
        all_matches_re = list(pat.finditer(text))
        total_hits = len(all_matches_re)

        segment_counts = [len(pat.findall(seg)) for seg in segment_texts]

        matches: list[TermMatch] = []

        if not all_matches_re and bm25_index is not None:
            # BM25 fallback.
            query_tokens = [t.lower() for t in _re.split(r"\W+", term) if t]
            scores = bm25_index.get_scores(query_tokens)
            top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:max_matches]
            best_windows = [(bm25_windows[i], scores[i]) for i in top_indices if scores[i] > 0]
            for win, score in best_windows:
                w_center = (win["start"] + win["end"]) // 2
                ctx_start = max(0, w_center - context_chars)
                ctx_end = min(len(text), w_center + context_chars)
                ctx_start, ctx_end = _clamp_to_word_boundary(text, ctx_start, ctx_end)
                snippet = text[ctx_start:ctx_end]
                section = _section_for_offset(sections, ctx_start)
                matches.append(TermMatch(
                    char_start=ctx_start,
                    char_end=ctx_end,
                    snippet=snippet,
                    section=section,
                    bm25_score=float(score),
                    is_bm25=True,
                ))
                total_chars += len(snippet)
                if total_chars > max_total_chars:
                    break
        else:
            shown = 0
            for m in all_matches_re:
                if shown >= max_matches or total_chars > max_total_chars:
                    break
                match_start, match_end = m.start(), m.end()
                ctx_start = max(0, match_start - context_chars)
                ctx_end = min(len(text), match_end + context_chars)
                ctx_start, ctx_end = _clamp_to_word_boundary(text, ctx_start, ctx_end)
                snippet = text[ctx_start:ctx_end]
                rel_start = match_start - ctx_start
                rel_end = match_end - ctx_start
                section = _section_for_offset(sections, match_start)
                matches.append(TermMatch(
                    char_start=match_start,
                    char_end=match_end,
                    snippet=snippet,
                    match_start=rel_start,
                    match_end=rel_end,
                    section=section,
                ))
                total_chars += len(snippet)
                shown += 1

        term_results.append(TermResult(
            term=term,
            total_hits=total_hits,
            matches=matches,
            segment_counts=segment_counts,
        ))

        if total_chars > max_total_chars:
            break

    return InArticleResult(
        doi=doi,
        segment_length=seg_len,
        sections=sections,
        term_results=term_results,
    )
