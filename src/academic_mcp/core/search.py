"""Core business logic for paper search across all sources.

Exports:
  search_zotero    – lexical search of the Zotero library
  search_by_doi    – DOI lookup via SQLite or DOI index
  search_papers    – unified parallel pipeline (= former _collect_search_results)
  reconstruct_abstract – OpenAlex inverted-index helper (also used by citations)
"""

from __future__ import annotations

import asyncio
import logging

from .types import DoiSearchResult

logger = logging.getLogger(__name__)


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


async def search_zotero(query: str, limit: int = 10) -> list[dict]:
    """Lexical search over the Zotero library (user + groups)."""
    from .. import zotero
    return await zotero.search_zotero(query, limit=limit)


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
) -> list[dict]:
    """Run the unified parallel-search pipeline and return merged, reranked results.

    This is the extracted body of the former ``_collect_search_results`` helper.
    Used by search_papers (formatting) and search_and_read (pick one result).
    """
    from .. import apis, zotero, zotero_sqlite, pdf_extractor
    from ..config import config
    from ..reranker import rerank_results

    limit = min(limit, 20)

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

    async def fetch_zotero_lex() -> list[dict]:
        out: list[dict] = []
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
            out.append({
                "title": item.get("title") or "Untitled",
                "authors": author_names,
                "year": (item.get("date") or "")[:4] or None,
                "doi": doi or None,
                "zotero_key": item.get("key") or None,
                "abstract": (item.get("abstractNote") or "").strip() or None,
                "citations": None,
                "venue": item.get("publicationTitle") or None,
                "found_in": ["zotero"],
                "in_zotero": True,
                "has_oa_pdf": True,
                "s2_id": None,
                "url": (item.get("url") or "").strip() or None,
            })
        return out

    async def fetch_semantic_zotero() -> list[dict]:
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
            chunks = await idx.search(query, k=fetch_n)
        except SemanticIndexUnavailable:
            return []
        except Exception as e:
            logger.warning("Semantic Zotero search failed: %s", e)
            return []

        if not chunks:
            return []

        try:
            reranked = await _cross_rerank(query, chunks, top_k=len(chunks))
        except Exception as e:
            logger.warning("Cross-reranker failed, falling back to bi-encoder order: %s", e)
            reranked = chunks

        seen_keys: set[str] = set()
        unique_hits: list[dict] = []
        for h in reranked:
            ik = h.get("item_key") or ""
            if ik and ik not in seen_keys:
                seen_keys.add(ik)
                unique_hits.append(h)
            if len(unique_hits) >= per_source_limit:
                break

        out: list[dict] = []
        for hit in unique_hits:
            key = hit.get("item_key")
            if not key:
                continue
            item = await zotero_sqlite.search_by_key(key)
            if not item:
                continue
            author_names = []
            for c in (item.creators or []):
                nm = c.display_name.strip()
                if nm:
                    author_names.append(nm)
            score = hit.get("rerank_score", hit.get("score"))
            out.append({
                "title": item.title or hit.get("title") or "Untitled",
                "authors": author_names,
                "year": (item.date or "")[:4] or None,
                "doi": item.DOI or hit.get("doi") or None,
                "zotero_key": item.key,
                "abstract": item.abstractNote or hit.get("snippet") or None,
                "citations": None,
                "venue": item.publicationTitle or None,
                "found_in": ["semantic_zotero", "zotero"],
                "in_zotero": True,
                "has_oa_pdf": True,
                "s2_id": None,
                "_semantic_zotero_score": score,
                "url": (item.url or "").strip() or None,
            })
        return out

    async def fetch_s2() -> list[dict]:
        out: list[dict] = []
        s2 = await apis.s2_search(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year,
        )
        for paper in s2.get("data", []):
            doi = apis.extract_doi(paper)
            doi_norm = zotero._normalize_doi(doi) if doi else None
            in_zot = doi_norm in zot_index if doi_norm else False
            out.append({
                "title": paper.get("title") or "Untitled",
                "authors": [a.get("name", "") for a in (paper.get("authors") or [])[:5]],
                "year": paper.get("year"),
                "doi": doi,
                "abstract": (paper.get("abstract") or "").strip() or None,
                "citations": paper.get("citationCount"),
                "venue": paper.get("venue") or None,
                "found_in": ["semantic_scholar"],
                "in_zotero": in_zot,
                "has_oa_pdf": bool((paper.get("openAccessPdf") or {}).get("url")),
                "s2_id": paper.get("paperId"),
            })
        return out

    async def fetch_openalex() -> list[dict]:
        out: list[dict] = []
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
            out.append({
                "title": work.get("title") or "Untitled",
                "authors": authors,
                "year": work.get("publication_year"),
                "doi": doi,
                "abstract": reconstruct_abstract(work.get("abstract_inverted_index")) or None,
                "citations": work.get("cited_by_count"),
                "venue": _oa_source.get("display_name") or None,
                "found_in": ["openalex"],
                "in_zotero": in_zot,
                "has_oa_pdf": (work.get("open_access") or {}).get("is_oa", False),
                "s2_id": None,
                "work_type": _oa_type or None,
                "container_title": _oa_source.get("display_name") if _oa_type in ("book-chapter",) else None,
                "url": _oa_url,
            })
        return out

    async def fetch_primo() -> list[dict]:
        primo_results = await apis.primo_search(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year,
        )
        out: list[dict] = []
        for r in primo_results:
            doi = (r.get("doi") or "").strip()
            doi_norm = zotero._normalize_doi(doi) if doi else None
            r["in_zotero"] = doi_norm in zot_index if doi_norm else False
            out.append(r)
        return out

    async def fetch_primo_law() -> list[dict]:
        law_results = await apis.primo_search_law_reviews(
            query, limit=per_source_limit,
            start_year=start_year, end_year=end_year,
        )
        out: list[dict] = []
        for r in law_results:
            doi = (r.get("doi") or "").strip()
            doi_norm = zotero._normalize_doi(doi) if doi else None
            in_zot = doi_norm in zot_index if doi_norm else False
            if in_zot:
                r["in_zotero"] = True
                r["has_oa_pdf"] = True
            else:
                r["in_zotero"] = False
            out.append(r)
        return out

    # ── Schedule fetchers in parallel ───────────────────────────────
    tasks: dict[str, "asyncio.Future"] = {}
    if source in ("all", "zotero"):
        tasks["zotero"] = asyncio.ensure_future(fetch_zotero_lex())
    if use_semantic and source in ("all", "semantic_zotero"):
        tasks["semantic_zotero"] = asyncio.ensure_future(fetch_semantic_zotero())
    if source in ("all", "semantic_scholar"):
        tasks["semantic_scholar"] = asyncio.ensure_future(fetch_s2())
    if source in ("all", "openalex"):
        tasks["openalex"] = asyncio.ensure_future(fetch_openalex())
    if source in ("all", "primo") and (config.primo_domain and config.primo_vid):
        tasks["primo"] = asyncio.ensure_future(fetch_primo())
    if (
        domain_hint == "law"
        and source in ("all", "primo")
        and (config.primo_domain and config.primo_vid)
    ):
        tasks["primo_law"] = asyncio.ensure_future(fetch_primo_law())

    if tasks:
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        by_source: dict[str, list[dict]] = {}
        for src_name, res in zip(tasks.keys(), gathered):
            if isinstance(res, Exception):
                logger.warning("%s search failed: %s", src_name, res)
                by_source[src_name] = []
            else:
                by_source[src_name] = res
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

    results: list[dict] = []
    seen_dois: set[str] = set()
    seen_zot_keys: set[str] = set()

    def _find_existing(rec: dict) -> dict | None:
        d = rec.get("doi")
        dn = zotero._normalize_doi(d) if d else None
        zk = rec.get("zotero_key")
        if dn and dn in seen_dois:
            for r in results:
                if r.get("doi") and zotero._normalize_doi(r["doi"]) == dn:
                    return r
        if zk and zk in seen_zot_keys:
            for r in results:
                if r.get("zotero_key") == zk:
                    return r
        return None

    def _merge_into(existing: dict, rec: dict) -> None:
        for s in rec.get("found_in", []):
            if s not in existing["found_in"]:
                existing["found_in"].append(s)
        if not existing.get("citations") and rec.get("citations"):
            existing["citations"] = rec["citations"]
        if not existing.get("abstract") and rec.get("abstract"):
            existing["abstract"] = rec["abstract"]
        if not existing.get("s2_id") and rec.get("s2_id"):
            existing["s2_id"] = rec["s2_id"]
        if not existing.get("url") and rec.get("url"):
            existing["url"] = rec["url"]
        if not existing.get("venue") and rec.get("venue"):
            existing["venue"] = rec["venue"]
        if not existing.get("work_type") and rec.get("work_type"):
            existing["work_type"] = rec["work_type"]
        if not existing.get("container_title") and rec.get("container_title"):
            existing["container_title"] = rec["container_title"]
        if rec.get("_primo_proxy_url") and not existing.get("_primo_proxy_url"):
            existing["_primo_proxy_url"] = rec["_primo_proxy_url"]
        if rec.get("_primo_oa_url") and not existing.get("_primo_oa_url"):
            existing["_primo_oa_url"] = rec["_primo_oa_url"]
            existing["has_oa_pdf"] = existing.get("has_oa_pdf") or rec.get("has_oa_pdf", False)
        if rec.get("_semantic_zotero_score") is not None and existing.get("_semantic_zotero_score") is None:
            existing["_semantic_zotero_score"] = rec["_semantic_zotero_score"]
        if rec.get("in_zotero") and not existing.get("in_zotero"):
            existing["in_zotero"] = True

    for src_name in priority:
        for rec in by_source.get(src_name, []):
            existing = _find_existing(rec)
            if existing is not None:
                _merge_into(existing, rec)
                continue
            d = rec.get("doi")
            dn = zotero._normalize_doi(d) if d else None
            if dn:
                seen_dois.add(dn)
            zk = rec.get("zotero_key")
            if zk:
                seen_zot_keys.add(zk)
            results.append(rec)

    # ── For Zotero items without abstracts, try getting a preview ──
    for r in results:
        if r["in_zotero"] and not r["abstract"] and r.get("doi"):
            try:
                zot_result = await zotero.get_paper_from_zotero(r["doi"])
                if zot_result and zot_result.get("text"):
                    preview = zot_result["text"][:600].strip()
                    last_period = preview.rfind(".")
                    if last_period > 300:
                        preview = preview[:last_period + 1]
                    r["abstract"] = f"[Preview from Zotero fulltext]: {preview}"
                elif zot_result and zot_result.get("pdf_path"):
                    page1 = pdf_extractor.extract_text_by_pages(zot_result["pdf_path"], 1, 1)
                    if page1.strip():
                        preview = page1.strip()[:600]
                        last_period = preview.rfind(".")
                        if last_period > 200:
                            preview = preview[:last_period + 1]
                        r["abstract"] = f"[Preview from PDF page 1]: {preview}"
            except Exception as e:
                logger.debug("Preview extraction failed for %s: %s", r["doi"], e)

    # ── Semantic re-ranking ─────────────────────────────────────────
    results = await rerank_results(query, results)

    # ── Optional Scite enrichment + retraction-aware re-sort ────────
    if include_scite:
        from .. import scite as scite_module

        dois = [zotero._normalize_doi(r["doi"]) for r in results if r.get("doi")]
        if dois:
            tallies_by_doi = await scite_module.get_scite_tallies_batch(dois)
            papers_by_doi = await scite_module.get_scite_papers_batch(dois)

            for r in results:
                _doi = r.get("doi")
                if not _doi:
                    continue
                doi_norm = zotero._normalize_doi(_doi)
                tally = tallies_by_doi.get(doi_norm)
                paper = papers_by_doi.get(doi_norm) or papers_by_doi.get(_doi)
                is_retracted = scite_module.paper_has_retraction_notice(paper)
                if tally:
                    tally = dict(tally)
                    tally["retracted"] = is_retracted
                    r["scite"] = tally
                elif is_retracted:
                    r["scite"] = {
                        "supporting": 0,
                        "contrasting": 0,
                        "mentioning": 0,
                        "citing": 0,
                        "total": 0,
                        "retracted": True,
                    }

            def _scite_adjust(rr: dict) -> float:
                s = rr.get("scite") or {}
                if not s:
                    return 0.0
                if s.get("retracted"):
                    return -0.25
                citing = max(1, int(s.get("citing") or 0))
                supporting = int(s.get("supporting") or 0)
                return min(0.08, (supporting / citing) * 0.08)

            for r in results:
                r["_scite_adjust"] = _scite_adjust(r)

            results.sort(
                key=lambda r: (
                    0 if (r.get("scite") or {}).get("retracted") else 1,
                    1 if r.get("in_zotero") else 0,
                    (r.get("_semantic_similarity") or 0.0) + (r.get("_scite_adjust") or 0.0),
                    r.get("citations") or 0,
                ),
                reverse=True,
            )

    return results
