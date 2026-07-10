"""Core business logic for paper search across all sources.

Exports:
  search_zotero    – hybrid (BM25 + semantic) search of the Zotero library
  search_by_doi    – DOI lookup via SQLite or DOI index
  search_papers    – unified parallel pipeline (= former _collect_search_results)
  reconstruct_abstract – OpenAlex inverted-index helper (also used by citations)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from .types import DoiSearchResult, ScitePayload, SearchHit

logger = logging.getLogger(__name__)

# Reciprocal-rank-fusion constant. 60 is the value from Cormack et al. (2009)
# and the one every subsequent hybrid-retrieval paper reuses; it damps the
# contribution of deep ranks without erasing them.
_RRF_K = 60

# Zotero previews are expensive (a Zotero lookup, sometimes a PDF page parse).
# Only the results a caller is plausibly going to read get one.
_MAX_PREVIEWS = 8
_PREVIEW_CONCURRENCY = 4


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """OpenAlex stores abstracts as inverted indexes — reconstruct them."""
    if not inverted_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def rrf_fuse(ranked_lists: list[list[str]], k: int = _RRF_K) -> dict[str, float]:
    """Reciprocal-rank fusion over lists of identifiers, best-first.

    Returns ``{identifier: fused_score}``. An item ranked highly by more than
    one retriever beats an item that only one retriever loved.
    """
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, ident in enumerate(ranked, start=1):
            fused[ident] = fused.get(ident, 0.0) + 1.0 / (k + rank)
    return fused


# DOI prefixes that identify a preprint/working-paper version of a work.
# When the same paper is filed twice, the published DOI is the more useful
# handle: it is what get_citations, Unpaywall, and the publisher's site key on.
_PREPRINT_DOI_PREFIXES = (
    "10.31235/",   # SocArXiv
    "10.31234/",   # PsyArXiv
    "10.48550/",   # arXiv
    "10.2139/",    # SSRN
    "10.1101/",    # bioRxiv / medRxiv
)

_TITLE_NOISE = re.compile(r"[^a-z0-9 ]+")


def normalize_title(title: str | None) -> str:
    """Collapse a title to a comparable key for dedup.

    Lowercase, strip punctuation and collapse whitespace. Titles shorter than
    ~5 words after normalisation are too generic to dedup on ("Introduction",
    "Discussion"), so they return '' and are never treated as duplicates.
    """
    if not title:
        return ""
    norm = _TITLE_NOISE.sub(" ", title.lower())
    norm = " ".join(norm.split())
    return norm if len(norm.split()) >= 5 else ""


def _bare_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return (
        doi.lower()
        .replace("https://doi.org/", "")
        .replace("http://doi.org/", "")
        .strip()
    )


def _is_preprint_doi(doi: str | None) -> bool:
    return _bare_doi(doi).startswith(_PREPRINT_DOI_PREFIXES)


def doi_rank(doi: str | None) -> int:
    """How good a handle this DOI is. Higher wins.

    3 — a canonical published DOI (``10.1145/…``): what Crossref, Unpaywall
        and OpenAlex key on, and the most useful thing to show a user.
    2 — a canonical preprint DOI (``10.48550/arXiv…``): resolves, but points
        at the version of record's shadow.
    1 — anything else non-empty, e.g. a shortDOI (``gsb98p``). Resolves, but
        is opaque and not accepted by most metadata APIs.
    0 — no DOI.
    """
    bare = _bare_doi(doi)
    if not bare:
        return 0
    if not bare.startswith("10."):
        return 1
    return 2 if bare.startswith(_PREPRINT_DOI_PREFIXES) else 3


def dedupe_library_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """Collapse duplicate copies of the same paper within the Zotero library.

    Deliberately keyed on the normalised *title*, not the DOI. A personal
    library routinely holds one paper filed three times — under its preprint
    DOI, a shortDOI, and the published DOI — so DOI-keyed dedup cannot see
    them as one, and BM25 scores them identically, letting a single
    multiply-filed paper occupy every result slot.

    This is the opposite of the policy in the cross-source merge below, where
    two records with distinct DOIs really are distinct works (an erratum, a
    reprint, a translation). Here they are one work the user filed twice.

    Among duplicates the copy with the best DOI wins (see ``doi_rank``), since
    that is the handle ``get_citations`` and ``fetch_fulltext`` work best with.
    Retrieval rank breaks any remaining tie.
    """
    # marker -> (rank of the first copy seen, currently-best copy). The first
    # copy's rank is what the group keeps, so deduping never reorders results.
    best: dict[str, tuple[int, SearchHit]] = {}
    kept: list[tuple[int, SearchHit]] = []

    for rank, hit in enumerate(hits):
        marker = normalize_title(hit.title)
        if not marker:
            # Too generic a title to merge on; keep as-is.
            kept.append((rank, hit))
            continue

        incumbent = best.get(marker)
        if incumbent is None:
            best[marker] = (rank, hit)
            continue

        first_rank, current = incumbent
        if doi_rank(hit.doi) > doi_rank(current.doi):
            best[marker] = (first_rank, hit)

    kept.extend(best.values())
    kept.sort(key=lambda pair: pair[0])
    return [hit for _, hit in kept]


async def search_zotero(
    query: str,
    limit: int = 10,
    semantic: bool | None = None,
) -> list[dict]:
    """Hybrid search over the Zotero library (user + groups).

    Fuses two retrievers with RRF:
      * **lexical**  — BM25 over the FTS5 mirror (:mod:`lexical_index`), which
        falls back to the legacy LIKE scan when the mirror isn't built yet.
      * **semantic** — the chunk-level embedding index, cross-encoder reranked.

    Lexical finds the paper you can name; semantic finds the paper you can only
    describe. Fusing them means a query needs to satisfy only one to surface.
    """
    from .. import zotero, zotero_sqlite
    from ..config import config

    use_semantic = config.semantic_default_on if semantic is None else bool(semantic)

    lexical_keys, semantic_keys = await asyncio.gather(
        _lexical_zotero_keys(query, limit * 3),
        _semantic_zotero_keys(query, limit * 3) if use_semantic else _empty_keys(),
    )

    # No FTS mirror and no semantic index: fall back to the legacy path so the
    # tool keeps working on a fresh install.
    if not lexical_keys and not semantic_keys:
        results = await zotero.search_zotero(query, limit=limit)
        warm_semantic_for_results(results)
        return results

    ranked_lists = [lst for lst in (lexical_keys, semantic_keys) if lst]
    fused = rrf_fuse(ranked_lists)
    ordered = sorted(fused, key=lambda k: fused[k], reverse=True)

    # Hydrate more than we need: real libraries hold duplicate copies of the
    # same paper, and BM25 scores them identically, so without deduping a
    # single multiply-filed paper can fill every slot.
    candidates = ordered[: limit * 6]
    items = await zotero_sqlite.search_by_keys(candidates)

    # Title-first, unlike the cross-source merge in search_papers. Out there a
    # distinct DOI means a distinct record and must be respected. In here, the
    # duplicates are one paper filed several times — typically the preprint
    # DOI, a shortDOI, and the published DOI — so DOI-keyed dedup cannot
    # collapse them, while the (identical, distinctive) title can.
    # normalize_title() returns '' for titles under five words, which are too
    # generic to merge on; those fall back to the DOI.
    results: list[dict] = []
    seen: set[str] = set()
    for key in candidates:
        item = items.get(key)
        if item is None:
            continue
        marker = normalize_title(item.title)
        if not marker and item.DOI:
            marker = zotero._normalize_doi(item.DOI)
        if marker:
            if marker in seen:
                continue
            seen.add(marker)
        results.append(item.to_search_result())
        if len(results) >= limit:
            break

    warm_semantic_for_results(results)
    return results


async def _empty_keys() -> list[str]:
    return []


async def _lexical_zotero_keys(query: str, limit: int) -> list[str]:
    """Item keys from the FTS5 mirror, best first. Empty when unavailable."""
    from .. import lexical_index
    from .background import ensure_lexical_background_sync

    try:
        ensure_lexical_background_sync()
    except Exception:
        pass

    try:
        if not lexical_index.available():
            return []
        return [key for key, _score in await lexical_index.search(query, limit=limit)]
    except Exception as e:
        logger.warning("Lexical Zotero search failed: %s", e)
        return []


async def _semantic_zotero_keys(query: str, limit: int) -> list[str]:
    """Item keys from the semantic index, best first, deduped by item."""
    from ..semantic_index import SemanticIndexUnavailable, get_semantic_index
    from .background import _ensure_semantic_background_sync

    try:
        _ensure_semantic_background_sync()
    except Exception:
        pass

    try:
        idx = get_semantic_index()
        chunks = await idx.search(query, k=limit)
    except SemanticIndexUnavailable:
        return []
    except Exception as e:
        logger.warning("Semantic Zotero search failed: %s", e)
        return []

    seen: set[str] = set()
    keys: list[str] = []
    for chunk in chunks:
        key = chunk.get("item_key") or ""
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def warm_semantic_for_results(results: list[dict], cap: int = 5) -> None:
    """Best-effort hot-path embedding while a background sync is in progress.

    During an active semantic index build, any item the user just looked at is
    pre-embedded so it surfaces immediately in ``semantic_search_zotero``
    without waiting for the background sync to reach it.

    Fire-and-forget: the Chroma lookups and the embedding call are both
    blocking, and none of this is worth making a user's search wait for.

    Zotero raw items use ``key``; post-fetch_zotero_lex records use
    ``zotero_key`` — this helper handles both.
    """
    keys = [
        (r.get("key") or r.get("zotero_key") or "")
        for r in results[:cap]
    ]
    keys = [k for k in keys if k]
    if not keys:
        return

    async def _runner() -> None:
        try:
            from ..semantic_index import get_semantic_index
            idx = get_semantic_index()
            st = await asyncio.to_thread(idx._load_status)
            if not st.get("in_progress"):
                return
            col = await asyncio.to_thread(idx._get_chroma_collection)
            for key in keys:
                existing = await asyncio.to_thread(
                    lambda k=key: col.get(where={"item_key": k}, include=[])
                )
                if existing.get("ids"):
                    continue
                try:
                    await idx.embed_item_now(key)
                    logger.debug("hot-path embed completed for %s", key)
                except Exception as e:
                    logger.debug("hot-path embed failed for %s: %s", key, e)
        except Exception:
            pass  # never let opportunistic embedding break the caller

    try:
        task = asyncio.create_task(_runner())
    except RuntimeError:
        return  # no running loop (sync caller) — skip warming
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)


_warm_tasks: set[asyncio.Task] = set()


async def search_by_doi(doi: str) -> DoiSearchResult | None:
    """Look up a DOI in Zotero.  Returns None when not found anywhere."""
    from .. import zotero_sqlite, zotero

    # SQLite fast path
    if zotero_sqlite.sqlite_config.available:
        result = await zotero_sqlite.search_by_doi(doi)
        if result:
            authors = (
                [c.display_name for c in result.creators[:5]]
                if result.creators else []
            )
            return DoiSearchResult(
                found=True,
                source="sqlite",
                title=result.title,
                doi=result.DOI,
                library_name=result.libraryName,
                library_type=result.libraryType,
                item_type=result.itemType,
                date=result.date,
                authors=[a for a in authors if a],
                abstract=result.abstractNote,
                key=result.key,
                url=result.url or None,
            )

    # Fallback to DOI index
    item = await zotero.find_item_by_doi(doi)
    if item:
        data = item.get("data", item)
        return DoiSearchResult(
            found=True,
            source="doi_index",
            title=data.get("title"),
            doi=data.get("DOI", doi),
        )

    return None


async def search_papers(
    query: str,
    limit: int = 5,
    source: str = "all",
    start_year: int | None = None,
    end_year: int | None = None,
    venue: str | None = None,
    domain_hint: str = "general",
    include_scite: bool = False,
    semantic: bool | None = None,
    semantic_query: str | list[str] | None = None,
    exclude_local: bool = False,
    diagnostics: dict | None = None,
) -> list[SearchHit]:
    """Run the unified parallel-search pipeline and return merged, reranked results.

    This is the extracted body of the former ``_collect_search_results`` helper.
    Used by search_papers (formatting) and search_and_read (pick one result).

    ``exclude_local`` drops everything in the local Zotero library: the Zotero
    lexical and semantic fetchers are skipped, and any external hit whose DOI is
    already in the library is removed (deduped by DOI). This yields an
    external-only list so locally-held papers can't crowd out material that only
    exists elsewhere. The caller (search_papers handler) splits the default
    (``exclude_local=False``) result into parallel local/external lists using the
    ``in_zotero`` flag carried on each hit.

    ``query`` is the *lexical* recall query (keywords) sent to the external
    APIs and the Zotero lexical index. ``semantic_query`` is the
    natural-language statement of intent that drives the local embedding
    search and the cross-encoder reranker; it may be a single string or a
    list of paraphrases / sub-questions (multi-query fan-out over the local
    index, fused via reciprocal-rank fusion). When omitted it falls back to
    ``query`` so the single-string call still works.

    ``diagnostics``, when a dict is passed, is filled in place with per-stage
    timings and hit counts. Nothing else reads it, so it costs nothing when
    omitted.
    """
    from .. import apis, zotero, zotero_sqlite, pdf_extractor
    from ..config import config
    from ..reranker import rerank_results

    limit = min(limit, 20)
    _t_start = time.perf_counter()
    _timings: dict[str, float] = {}
    _counts: dict[str, int] = {}

    # Normalise the semantic query into a list of non-empty strings. The
    # lexical query stays a single keyword string; only the local semantic
    # pipeline fans out across multiple formulations.
    if semantic_query is None:
        semantic_queries = [query]
    elif isinstance(semantic_query, str):
        semantic_queries = [semantic_query] if semantic_query.strip() else [query]
    else:
        semantic_queries = [q for q in semantic_query if q and q.strip()] or [query]

    # When a reranker is configured, over-fetch from each source so the
    # reranker has a wider candidate pool.
    _rerank_on = (
        (config.reranker_primary or "none").lower() not in ("none", "off", "disabled")
        or (config.reranker_fallback or "none").lower() not in ("none", "off", "disabled")
    )
    if _rerank_on:
        per_source_limit = min(limit * config.reranker_overfetch, config.reranker_overfetch_cap)
    else:
        per_source_limit = limit

    # `semantic` defaults to config.semantic_default_on; explicit per-call wins.
    if semantic is not None:
        use_semantic = bool(semantic)
    else:
        use_semantic = config.semantic_default_on

    # Pre-fetch DOI index once (used to flag Zotero membership in API results).
    zot_index = await zotero.get_doi_index()

    # ── Per-source fetchers ─────────────────────────────────────────

    def _year_in_range(year: str | None) -> bool:
        """Undated items are never filtered out — see zotero_sqlite.search_items."""
        if not (start_year or end_year):
            return True
        if not year or not year.isdigit():
            return True
        y = int(year)
        if start_year and y < start_year:
            return False
        if end_year and y > end_year:
            return False
        return True

    async def fetch_zotero_lex() -> list[SearchHit]:
        """BM25 hits from the FTS5 mirror, falling back to the legacy LIKE scan."""
        from .. import lexical_index

        scored: list[tuple[str, float]] = []
        if lexical_index.available():
            # Over-fetch: the year filter is applied after ranking, since the
            # FTS mirror doesn't carry dates.
            fetch_n = per_source_limit * (3 if (start_year or end_year) else 1)
            scored = await lexical_index.search(query, limit=fetch_n)

        if scored:
            items = await zotero_sqlite.search_by_keys([k for k, _ in scored])
            out: list[SearchHit] = []
            for key, score in scored:
                item = items.get(key)
                if item is None:
                    continue
                year = (item.date or "")[:4] or None
                if not _year_in_range(year):
                    continue
                if len(out) >= per_source_limit * 3:
                    break  # room for dedup to discard copies
                out.append(SearchHit(
                    title=item.title or "Untitled",
                    authors=[c.display_name for c in (item.creators or []) if c.display_name],
                    year=year,
                    doi=(item.DOI or "").strip() or None,
                    zotero_key=item.key,
                    abstract=(item.abstractNote or "").strip() or None,
                    citations=None,
                    venue=item.publicationTitle or None,
                    found_in=["zotero"],
                    in_zotero=True,
                    has_oa_pdf=True,
                    s2_id=None,
                    lexical_score=score,
                    url=(item.url or "").strip() or None,
                    work_type=item.itemType or None,
                    reference_note=item.reference_note or None,
                    zotero_library_type=item.libraryType or None,
                    zotero_group_id=item.groupID,
                ))
            return dedupe_library_hits(out)[:per_source_limit]

        # Legacy path: no FTS mirror yet (first run, or LEXICAL_INDEX_ENABLED=false).
        out = []
        zot_results = await zotero.search_zotero(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year,
        )
        for item in zot_results:
            creators = item.get("creators", [])
            author_names = []
            for c in (creators if isinstance(creators, list) else []):
                if isinstance(c, dict):
                    name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                    if name:
                        author_names.append(name)
            doi = (item.get("DOI") or "").strip()
            out.append(SearchHit(
                title=item.get("title") or "Untitled",
                authors=author_names,
                year=(item.get("date") or "")[:4] or None,
                doi=doi or None,
                zotero_key=item.get("key") or None,
                abstract=(item.get("abstractNote") or "").strip() or None,
                citations=None,
                venue=item.get("publicationTitle") or None,
                found_in=["zotero"],
                in_zotero=True,
                has_oa_pdf=True,
                s2_id=None,
                url=(item.get("url") or "").strip() or None,
                work_type=item.get("itemType") or None,
                reference_note=(item.get("referenceNote") or "").strip() or None,
                zotero_library_type=item.get("libraryType") or None,
                zotero_group_id=item.get("groupID"),
            ))
        return dedupe_library_hits(out)

    async def fetch_semantic_zotero() -> list[SearchHit]:
        from ..semantic_index import SemanticIndexUnavailable, get_semantic_index
        from ..cross_reranker import rerank as _cross_rerank
        from .background import _ensure_semantic_background_sync

        try:
            _ensure_semantic_background_sync()
        except Exception:
            pass

        try:
            idx = get_semantic_index()
            try:
                _st = await idx.status()
                if int(_st.get("count") or 0) <= 0:
                    return []
            except Exception:
                pass
            fetch_n = max(per_source_limit, config.cross_reranker_fetch or 50)
            # Fan out across every semantic formulation. The local index is
            # free and fast, so multiple paraphrases cost little; each gets its
            # own bi-encoder retrieval + cross-encoder rerank against that exact
            # phrasing, then the per-query lists are fused with RRF below.
            ranked_lists: list[list[dict]] = []
            for q in semantic_queries:
                try:
                    chunks = await idx.search(q, k=fetch_n)
                except Exception as e:
                    logger.warning("Semantic Zotero search failed for %r: %s", q, e)
                    continue
                if not chunks:
                    continue
                try:
                    reranked = await _cross_rerank(q, chunks, top_k=len(chunks))
                except Exception as e:
                    logger.warning(
                        "Cross-reranker failed for %r, using bi-encoder order: %s", q, e
                    )
                    reranked = chunks
                ranked_lists.append(reranked)
        except SemanticIndexUnavailable:
            return []
        except Exception as e:
            logger.warning("Semantic Zotero search failed: %s", e)
            return []

        if not ranked_lists:
            return []

        # Reciprocal-rank fusion across the per-query ranked lists. For a
        # single query this preserves the cross-encoder order exactly; for
        # several it rewards items that surface near the top for more than one
        # formulation. The displayed score is the best cross-encoder score the
        # item earned across queries (interpretable, unlike the RRF magnitude).
        _MAX_PASSAGES = 4  # 1 preview + 3 behind "see more"
        fused_score: dict[str, float] = {}
        best_rerank: dict[str, float] = {}
        first_hit: dict[str, dict] = {}
        chunks_by_key: dict[str, list[dict]] = {}  # all chunk hits → passages
        for rl in ranked_lists:
            seen_in_list: set[str] = set()
            rank = 0
            for h in rl:
                ik = h.get("item_key") or ""
                if not ik:
                    continue
                chunks_by_key.setdefault(ik, []).append(h)
                if ik in seen_in_list:
                    continue
                seen_in_list.add(ik)
                rank += 1
                fused_score[ik] = fused_score.get(ik, 0.0) + 1.0 / (_RRF_K + rank)
                sc = h.get("rerank_score", h.get("score"))
                if sc is not None and (ik not in best_rerank or sc > best_rerank[ik]):
                    best_rerank[ik] = sc
                first_hit.setdefault(ik, h)

        def _passages_for(key: str) -> list[str]:
            """Top distinct passages for an item — already retrieved, no extra cost."""
            seen: set[str] = set()
            scored: list[tuple[float, str]] = []
            for c in chunks_by_key.get(key, []):
                snip = (c.get("snippet") or "").strip()
                if "\n\n" in snip:  # strip the "{header}\n\n{body}" chunk prefix
                    snip = snip.split("\n\n", 1)[1].strip()
                snip = snip[:280]
                if not snip:
                    continue
                dedup = snip[:60].lower()
                if dedup in seen:
                    continue
                seen.add(dedup)
                sc = c.get("rerank_score", c.get("score")) or 0.0
                scored.append((float(sc), snip))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [s for _, s in scored[:_MAX_PASSAGES]]

        ordered_keys = sorted(
            fused_score, key=lambda k: fused_score[k], reverse=True
        )[:per_source_limit]

        # One connection, one query — hydrating these one key at a time meant
        # a fresh read-only SQLite connection (and shadow-refresh probe) each.
        items_by_key = await zotero_sqlite.search_by_keys(ordered_keys)

        out: list[SearchHit] = []
        for key in ordered_keys:
            hit = first_hit.get(key, {})
            semantic_snippets = _passages_for(key)
            item = items_by_key.get(key)
            if not item:
                continue
            author_names = []
            for c in (item.creators or []):
                nm = c.display_name.strip()
                if nm:
                    author_names.append(nm)
            score = best_rerank.get(key, hit.get("score"))
            out.append(SearchHit(
                title=item.title or hit.get("title") or "Untitled",
                authors=author_names,
                year=(item.date or "")[:4] or None,
                doi=item.DOI or hit.get("doi") or None,
                zotero_key=item.key,
                abstract=item.abstractNote or hit.get("snippet") or None,
                citations=None,
                venue=item.publicationTitle or None,
                found_in=["semantic_zotero", "zotero"],
                in_zotero=True,
                has_oa_pdf=True,
                s2_id=None,
                semantic_zotero_score=score,
                semantic_snippets=semantic_snippets,
                url=(item.url or "").strip() or None,
                work_type=item.itemType or None,
                reference_note=item.reference_note or None,
                zotero_library_type=item.libraryType or None,
                zotero_group_id=item.groupID,
            ))
        return dedupe_library_hits(out)

    async def fetch_s2() -> list[SearchHit]:
        out: list[SearchHit] = []
        s2 = await apis.s2_search(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year,
        )
        for paper in s2.get("data", []):
            doi = apis.extract_doi(paper)
            doi_norm = zotero._normalize_doi(doi) if doi else None
            in_zot = doi_norm in zot_index if doi_norm else False
            out.append(SearchHit(
                title=paper.get("title") or "Untitled",
                authors=[a.get("name", "") for a in (paper.get("authors") or [])[:5]],
                year=paper.get("year"),
                doi=doi,
                abstract=(paper.get("abstract") or "").strip() or None,
                citations=paper.get("citationCount"),
                venue=paper.get("venue") or None,
                found_in=["semantic_scholar"],
                in_zotero=in_zot,
                has_oa_pdf=bool((paper.get("openAccessPdf") or {}).get("url")),
                s2_id=paper.get("paperId"),
                work_type=(paper.get("publicationTypes") or [None])[0],
            ))
        return out

    async def fetch_openalex() -> list[SearchHit]:
        out: list[SearchHit] = []
        oa = await apis.openalex_search(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year, venue=venue,
        )
        for work in oa.get("results", []):
            doi = apis.extract_doi(work)
            doi_norm = zotero._normalize_doi(doi) if doi else None
            authors: list[str] = []
            for auth in (work.get("authorships") or []):
                if not auth:
                    continue
                name = (auth.get("author") or {}).get("display_name")
                if name:
                    authors.append(name)
                if len(authors) >= 5:
                    break
            in_zot = doi_norm in zot_index if doi_norm else False
            _primary_loc = work.get("primary_location") or {}
            _oa_source = _primary_loc.get("source") or {}
            _oa_type = (work.get("type") or "").lower()
            _oa_url = _primary_loc.get("pdf_url") or _primary_loc.get("landing_page_url") or None
            out.append(SearchHit(
                title=work.get("title") or "Untitled",
                authors=authors,
                year=work.get("publication_year"),
                doi=doi,
                abstract=reconstruct_abstract(work.get("abstract_inverted_index")) or None,
                citations=work.get("cited_by_count"),
                venue=_oa_source.get("display_name") or None,
                found_in=["openalex"],
                in_zotero=in_zot,
                has_oa_pdf=(work.get("open_access") or {}).get("is_oa", False),
                s2_id=None,
                work_type=_oa_type or None,
                container_title=_oa_source.get("display_name") if _oa_type in ("book-chapter",) else None,
                url=_oa_url,
            ))
        return out

    async def fetch_primo() -> list[SearchHit]:
        primo_results = await apis.primo_search(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year,
        )
        out: list[SearchHit] = []
        for r in primo_results:
            doi = (r.get("doi") or "").strip()
            doi_norm = zotero._normalize_doi(doi) if doi else None
            in_zot = doi_norm in zot_index if doi_norm else False
            out.append(SearchHit(
                title=r.get("title") or "Untitled",
                authors=r.get("authors") or [],
                year=r.get("year"),
                doi=doi or None,
                abstract=r.get("abstract"),
                citations=None,
                venue=r.get("venue"),
                found_in=r.get("found_in") or ["primo"],
                in_zotero=in_zot,
                has_oa_pdf=bool(r.get("has_oa_pdf")),
                s2_id=None,
                primo_oa_url=r.get("_primo_oa_url"),
                primo_proxy_url=r.get("_primo_proxy_url"),
            ))
        return out

    async def fetch_primo_law() -> list[SearchHit]:
        law_results = await apis.primo_search_law_reviews(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year,
        )
        out: list[SearchHit] = []
        for r in law_results:
            doi = (r.get("doi") or "").strip()
            doi_norm = zotero._normalize_doi(doi) if doi else None
            in_zot = doi_norm in zot_index if doi_norm else False
            out.append(SearchHit(
                title=r.get("title") or "Untitled",
                authors=r.get("authors") or [],
                year=r.get("year"),
                doi=doi or None,
                abstract=r.get("abstract"),
                citations=None,
                venue=r.get("venue"),
                found_in=r.get("found_in") or ["primo_law"],
                in_zotero=in_zot,
                has_oa_pdf=bool(r.get("has_oa_pdf")) or in_zot,
                s2_id=None,
                primo_oa_url=r.get("_primo_oa_url"),
                primo_proxy_url=r.get("_primo_proxy_url"),
            ))
        return out

    # ── Schedule fetchers in parallel ───────────────────────────────
    async def _timed(name: str, coro) -> list[SearchHit]:
        """Run a fetcher, recording how long it took and what it returned."""
        started = time.perf_counter()
        try:
            return await coro
        finally:
            _timings[name] = time.perf_counter() - started

    tasks: dict[str, "asyncio.Future"] = {}
    # When excluding local results, skip the (expensive) Zotero fetchers
    # entirely — external hits are still flagged in_zotero via the DOI index,
    # which is all we need to dedupe them out below.
    if source in ("all", "zotero") and not exclude_local:
        tasks["zotero"] = asyncio.ensure_future(_timed("zotero", fetch_zotero_lex()))
    if use_semantic and source in ("all", "semantic_zotero") and not exclude_local:
        tasks["semantic_zotero"] = asyncio.ensure_future(
            _timed("semantic_zotero", fetch_semantic_zotero())
        )
    if source in ("all", "semantic_scholar"):
        tasks["semantic_scholar"] = asyncio.ensure_future(
            _timed("semantic_scholar", fetch_s2())
        )
    if source in ("all", "openalex"):
        tasks["openalex"] = asyncio.ensure_future(_timed("openalex", fetch_openalex()))
    if source in ("all", "primo") and (config.primo_domain and config.primo_vid):
        tasks["primo"] = asyncio.ensure_future(_timed("primo", fetch_primo()))
    if (
        domain_hint == "law"
        and source in ("all", "primo")
        and (config.primo_domain and config.primo_vid)
    ):
        tasks["primo_law"] = asyncio.ensure_future(_timed("primo_law", fetch_primo_law()))

    if tasks:
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        by_source: dict[str, list[SearchHit]] = {}
        for src_name, res in zip(tasks.keys(), gathered):
            if isinstance(res, Exception):
                logger.warning("%s search failed: %s", src_name, res)
                by_source[src_name] = []
                _counts[src_name] = -1  # -1 distinguishes "failed" from "no hits"
            else:
                by_source[src_name] = res
                _counts[src_name] = len(res)
    else:
        by_source = {}

    # ── Merge results in priority order ─────────────────────────────
    priority = [
        "zotero",
        "semantic_zotero",
        "semantic_scholar",
        "openalex",
        "primo",
        "primo_law",
    ]

    results: list[SearchHit] = []
    # Direct lookups rather than a linear scan of `results` per candidate —
    # with over-fetching on, the merge sees several hundred records.
    by_doi: dict[str, SearchHit] = {}
    by_zot_key: dict[str, SearchHit] = {}
    by_title: dict[str, SearchHit] = {}

    def _find_existing(rec: SearchHit) -> SearchHit | None:
        dn = zotero._normalize_doi(rec.doi) if rec.doi else None
        if dn and dn in by_doi:
            return by_doi[dn]
        if rec.zotero_key and rec.zotero_key in by_zot_key:
            return by_zot_key[rec.zotero_key]

        # Title fallback. Two records with distinct *published* DOIs and the
        # same title are distinct works (an erratum, a reprint, a translation),
        # so a bare title match is not enough to merge on. It is enough in two
        # cases:
        #   * one side carries no DOI at all — the same preprint surfacing from
        #     S2 and OpenAlex, which would otherwise be listed twice;
        #   * one side's DOI is a preprint DOI — the preprint and the version
        #     of record are the same work under two DOIs, which is the single
        #     most common duplicate in practice.
        tn = normalize_title(rec.title)
        if tn and tn in by_title:
            other = by_title[tn]
            if not dn or not other.doi:
                return other
            if _is_preprint_doi(rec.doi) or _is_preprint_doi(other.doi):
                return other
        return None

    def _index(rec: SearchHit) -> None:
        dn = zotero._normalize_doi(rec.doi) if rec.doi else None
        if dn:
            by_doi.setdefault(dn, rec)
        if rec.zotero_key:
            by_zot_key.setdefault(rec.zotero_key, rec)
        tn = normalize_title(rec.title)
        if tn:
            by_title.setdefault(tn, rec)

    def _merge_into(existing: SearchHit, rec: SearchHit) -> None:
        for s in rec.found_in:
            if s not in existing.found_in:
                existing.found_in.append(s)
        # Two records for one work can disagree about which DOI to show — a
        # shortDOI, the preprint's, the publisher's. Keep the best handle, and
        # index *both* DOIs against the surviving record so a later hit
        # carrying either one still merges here rather than starting a new row.
        if doi_rank(rec.doi) > doi_rank(existing.doi):
            existing.doi, superseded = rec.doi, existing.doi
        else:
            superseded = rec.doi
        for known in (existing.doi, superseded):
            if known:
                by_doi.setdefault(zotero._normalize_doi(known), existing)
        if not existing.citations and rec.citations:
            existing.citations = rec.citations
        if not existing.abstract and rec.abstract:
            existing.abstract = rec.abstract
        if not existing.s2_id and rec.s2_id:
            existing.s2_id = rec.s2_id
        if not existing.url and rec.url:
            existing.url = rec.url
        if not existing.venue and rec.venue:
            existing.venue = rec.venue
        if not existing.work_type and rec.work_type:
            existing.work_type = rec.work_type
        if not existing.container_title and rec.container_title:
            existing.container_title = rec.container_title
        if rec.primo_proxy_url and not existing.primo_proxy_url:
            existing.primo_proxy_url = rec.primo_proxy_url
        if rec.primo_oa_url and not existing.primo_oa_url:
            existing.primo_oa_url = rec.primo_oa_url
            existing.has_oa_pdf = existing.has_oa_pdf or rec.has_oa_pdf
        if rec.semantic_zotero_score is not None and existing.semantic_zotero_score is None:
            existing.semantic_zotero_score = rec.semantic_zotero_score
        if rec.lexical_score is not None and existing.lexical_score is None:
            existing.lexical_score = rec.lexical_score
        if rec.semantic_snippets and not existing.semantic_snippets:
            existing.semantic_snippets = rec.semantic_snippets
        if rec.reference_note and not existing.reference_note:
            existing.reference_note = rec.reference_note
        if rec.zotero_library_type and not existing.zotero_library_type:
            existing.zotero_library_type = rec.zotero_library_type
        if rec.zotero_group_id is not None and existing.zotero_group_id is None:
            existing.zotero_group_id = rec.zotero_group_id
        if rec.in_zotero and not existing.in_zotero:
            existing.in_zotero = True

    for src_name in priority:
        for rec in by_source.get(src_name, []):
            existing = _find_existing(rec)
            if existing is not None:
                _merge_into(existing, rec)
                continue
            _index(rec)
            results.append(rec)

    # ── For Zotero items without abstracts, try getting a preview ──
    # Bounded and concurrent: this was a serial await per result, and the PDF
    # page extraction ran synchronously on the event loop.
    _needs_preview = [
        r for r in results if r.in_zotero and not r.abstract and r.doi
    ][:_MAX_PREVIEWS]

    if _needs_preview:
        _t_preview = time.perf_counter()
        _sem = asyncio.Semaphore(_PREVIEW_CONCURRENCY)

        async def _add_preview(r: SearchHit) -> None:
            async with _sem:
                try:
                    zot_result = await zotero.get_paper_from_zotero(r.doi)
                    if zot_result and zot_result.get("text"):
                        preview = zot_result["text"][:600].strip()
                        last_period = preview.rfind(".")
                        if last_period > 300:
                            preview = preview[:last_period + 1]
                        r.abstract = f"[Preview from Zotero fulltext]: {preview}"
                    elif zot_result and zot_result.get("pdf_path"):
                        page1 = await asyncio.to_thread(
                            pdf_extractor.extract_text_by_pages,
                            zot_result["pdf_path"], 1, 1,
                        )
                        if page1.strip():
                            preview = page1.strip()[:600]
                            last_period = preview.rfind(".")
                            if last_period > 200:
                                preview = preview[:last_period + 1]
                            r.abstract = f"[Preview from PDF page 1]: {preview}"
                except Exception as e:
                    logger.debug("Preview extraction failed for %s: %s", r.doi, e)

        await asyncio.gather(*(_add_preview(r) for r in _needs_preview))
        _timings["previews"] = time.perf_counter() - _t_preview

    # ── Semantic re-ranking ─────────────────────────────────────────
    # The cross-encoder is the final quality gate over the merged pool, so it
    # sees the natural-language intent (semantic_queries) — never the keyword
    # recall string, which would score relevance poorly.
    _t_rerank = time.perf_counter()
    results = await rerank_results(semantic_queries, results)
    _timings["rerank"] = time.perf_counter() - _t_rerank

    # ── Optional Scite enrichment + retraction-aware re-sort ────────
    _t_scite = time.perf_counter()
    if include_scite:
        from .. import scite as scite_module

        dois = [zotero._normalize_doi(r.doi) for r in results if r.doi]
        if dois:
            tallies_by_doi = await scite_module.get_scite_tallies_batch(dois)
            papers_by_doi = await scite_module.get_scite_papers_batch(dois)

            for r in results:
                _doi = r.doi
                if not _doi:
                    continue
                doi_norm = zotero._normalize_doi(_doi)
                tally = tallies_by_doi.get(doi_norm)
                paper = papers_by_doi.get(doi_norm) or papers_by_doi.get(_doi)
                is_retracted = scite_module.paper_has_retraction_notice(paper)
                if tally:
                    tally = dict(tally)
                    tally["retracted"] = is_retracted
                    r.scite = ScitePayload(**tally)
                elif is_retracted:
                    r.scite = ScitePayload(retracted=True)

            def _scite_adjust(rr: SearchHit) -> float:
                s = rr.scite
                if not s:
                    return 0.0
                if s.retracted:
                    return -0.25
                citing = max(1, s.citing)
                supporting = s.supporting
                return min(0.08, (supporting / citing) * 0.08)

            for r in results:
                r.scite_adjust = _scite_adjust(r)

            results.sort(
                key=lambda r: (
                    0 if r.scite and r.scite.retracted else 1,
                    1 if r.in_zotero else 0,
                    (r.semantic_similarity or 0.0) + (r.scite_adjust or 0.0),
                    r.citations or 0,
                ),
                reverse=True,
            )
        _timings["scite"] = time.perf_counter() - _t_scite

    # External-only mode: drop anything already in the local library. External
    # hits that match a library DOI were flagged in_zotero via the DOI index.
    if exclude_local:
        results = [r for r in results if not r.in_zotero]

    _timings["total"] = time.perf_counter() - _t_start
    if diagnostics is not None:
        diagnostics["timings"] = dict(_timings)
        diagnostics["counts"] = dict(_counts)
        diagnostics["merged"] = len(results)
    logger.debug(
        "search_papers(%r): %d results in %.0f ms — %s",
        query, len(results), _timings["total"] * 1000,
        ", ".join(f"{k}={v * 1000:.0f}ms" for k, v in sorted(_timings.items())),
    )

    # Bet on the next call: pull full text for the top local hits into the
    # article cache while the user reads the result list.
    try:
        from .background import prewarm_articles
        prewarm_articles(results[:limit])
    except Exception as e:
        logger.debug("Prewarm scheduling failed: %s", e)

    return results


def search_in_corpus(
    query: str,
    candidates: list,
    limit: int = 25,
) -> list[SearchHit]:
    """Rank *candidates* (CitationWorkItem instances) by relevance to *query*.

    Scoring is purely lexical: query terms are matched against the title
    (weight 2) and abstract (weight 1) of each candidate.  Items already in
    Zotero receive a small boost so they appear above equal-score items that
    are not locally accessible.

    Returns a list of SearchHit objects sorted by descending score, capped at *limit*.
    """
    terms = [t.lower() for t in query.split() if t]
    if not terms:
        items = candidates[:limit]
        return [_corpus_item_to_hit(c, 0.0) for c in items]

    scored: list[tuple[float, object]] = []
    for c in candidates:
        title_lc = (c.title or "").lower()
        abstract_lc = (c.abstract or "").lower()
        score = sum(
            2.0 * title_lc.count(t) + abstract_lc.count(t)
            for t in terms
        )
        if c.in_zotero:
            score += 0.5
        scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [_corpus_item_to_hit(c, s) for s, c in scored[:limit]]


def _corpus_item_to_hit(item: object, score: float) -> SearchHit:
    """Convert a CitationWorkItem to a SearchHit."""
    from .types import CitationWorkItem
    c: CitationWorkItem = item  # type: ignore[assignment]
    return SearchHit(
        title=c.title or "Untitled",
        authors=list(c.authors),
        year=str(c.year) if c.year else None,
        doi=c.doi,
        zotero_key=None,
        abstract=c.abstract,
        citations=c.cited_by_count or None,
        venue=c.venue,
        found_in=["openalex"],
        in_zotero=c.in_zotero,
        has_oa_pdf=False,
        s2_id=None,
        url=None,
        semantic_similarity=score,
    )

