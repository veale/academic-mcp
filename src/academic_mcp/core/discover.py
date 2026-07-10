"""core.discover — related-work discovery from a set of seed papers.

Keyword search finds papers that *say* what you asked for. The citation graph
finds papers that *sit where* your seeds sit, including the ones whose authors
chose different vocabulary. Given a handful of seeds, three graph signals do
most of the work:

* **Shared references** (bibliographic coupling). A paper cited by several of
  your seeds is foundational to whatever the seeds have in common. Cheap: one
  lookup per seed.
* **Shared citers**. A paper that cites several of your seeds is doing the
  same synthesis you are — usually a survey or a direct successor.
* **Co-citation**. Take the papers citing your seeds and look at *their*
  bibliographies: works that keep appearing alongside your seeds are read as
  part of the same conversation, even when they share no words with them.

Each signal is a count over the seed set, normalised and blended. Papers
already in the Zotero library are flagged, not filtered — knowing you already
own the obvious foundational work is itself the useful answer.

Cost: two OpenAlex requests per seed plus a batch hydrate, so ~9 requests for
4 seeds. All of them go through the TTL cache.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# How many citing works we sample per seed when computing co-citation. Sorted
# by citation count, so the sample is the influential end of the citing set.
_CITER_SAMPLE = 50

# Signal weights. Shared references are the highest-precision signal (an
# explicit act of citation by your own seeds); co-citation is the noisiest,
# since it is mediated by third parties, but it is the one that surfaces work
# keyword search would never reach.
_W_COUPLING = 1.0
_W_SHARED_CITERS = 0.8
_W_COCITATION = 0.5


class RelatedWork(BaseModel):
    openalex_id: str
    title: str
    doi: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    cited_by_count: int = 0
    abstract: str | None = None
    in_zotero: bool = False

    # Why this surfaced — the counts are over the seed set, so "3" with four
    # seeds means three of your four seeds cite it.
    shared_references: int = 0
    shared_citers: int = 0
    cocitations: int = 0
    score: float = 0.0

    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.shared_references:
            out.append(f"cited by {self.shared_references} seed(s)")
        if self.shared_citers:
            out.append(f"cites {self.shared_citers} seed(s)")
        if self.cocitations:
            out.append(f"co-cited {self.cocitations}×")
        return out


# `shared_citers` counts seeds cited by this work; `shared_references` counts
# seeds citing it. A work can be both (a mid-period paper that builds on one
# seed and is built on by another).


class DiscoverResult(BaseModel):
    seeds: list[str] = Field(default_factory=list)
    unresolved_seeds: list[str] = Field(default_factory=list)
    items: list[RelatedWork] = Field(default_factory=list)
    error: str | None = None


async def _resolve_seed(doi: str) -> tuple[str, str | None]:
    """Map a seed DOI to its OpenAlex Work ID. Returns ``(doi, work_id|None)``."""
    from .. import apis

    try:
        work = await apis.openalex_work(doi)
    except Exception as e:
        logger.debug("Seed %s did not resolve in OpenAlex: %s", doi, e)
        return doi, None
    if not work or not work.get("id"):
        return doi, None
    return doi, work["id"].split("/")[-1]


async def discover_related(
    seed_dois: list[str],
    limit: int = 25,
    exclude_dois: list[str] | None = None,
    citer_sample: int = _CITER_SAMPLE,
) -> DiscoverResult:
    """Rank works related to *seed_dois* by their position in the citation graph."""
    from .. import apis, zotero
    from .search import reconstruct_abstract

    seeds = [d.strip() for d in seed_dois if d and d.strip()]
    if not seeds:
        return DiscoverResult(error="discover_related needs at least one seed DOI.")

    resolved = await asyncio.gather(*(_resolve_seed(d) for d in seeds))
    seed_ids = {work_id: doi for doi, work_id in resolved if work_id}
    unresolved = [doi for doi, work_id in resolved if not work_id]

    if not seed_ids:
        return DiscoverResult(
            seeds=seeds,
            unresolved_seeds=unresolved,
            error="None of the seed DOIs could be resolved in OpenAlex.",
        )

    # Fan out: references and citing-work bibliographies for every seed.
    refs_task = asyncio.gather(
        *(apis.openalex_referenced_works(wid) for wid in seed_ids),
        return_exceptions=True,
    )
    citers_task = asyncio.gather(
        *(apis.openalex_citing_works_refs(wid, sample=citer_sample) for wid in seed_ids),
        return_exceptions=True,
    )
    ref_lists, citer_ref_lists = await asyncio.gather(refs_task, citers_task)

    seed_id_set = set(seed_ids)
    shared_references: dict[str, int] = {}
    shared_citers: dict[str, int] = {}
    cocitations: dict[str, int] = {}

    for result in ref_lists:
        if isinstance(result, Exception):
            logger.debug("Seed reference lookup failed: %s", result)
            continue
        # One increment per seed, even if a seed lists the same work twice.
        for work_id in set(result):
            shared_references[work_id] = shared_references.get(work_id, 0) + 1

    # The same citer comes back once per seed it cites, so dedupe on the citer
    # ID; its bibliography tells us how many seeds it actually cites.
    seen_citers: set[str] = set()
    for result in citer_ref_lists:
        if isinstance(result, Exception):
            logger.debug("Seed citer lookup failed: %s", result)
            continue
        for citer_id, citer_refs in result:
            if not citer_id or citer_id in seen_citers:
                continue
            seen_citers.add(citer_id)
            citer_set = set(citer_refs)

            # How many of our seeds this citer cites. It came back from a
            # `cites:<seed>` query, so this is at least 1 — but ≥2 is the
            # signal that it is synthesising the seed set rather than
            # happening to touch one member of it.
            n_cited = len(citer_set & seed_id_set)
            if n_cited and citer_id not in seed_id_set:
                shared_citers[citer_id] = n_cited

            # Everything else in that bibliography is co-cited with the seed.
            for work_id in citer_set - seed_id_set:
                cocitations[work_id] = cocitations.get(work_id, 0) + 1

    candidates = set(shared_references) | set(shared_citers) | set(cocitations)
    candidates -= seed_id_set
    if not candidates:
        return DiscoverResult(seeds=seeds, unresolved_seeds=unresolved)

    n_seeds = len(seed_ids)
    max_cocite = max(cocitations.values()) if cocitations else 1

    def _score(work_id: str) -> float:
        # Each component lands in [0, 1] so the weights mean what they say.
        coupling = shared_references.get(work_id, 0) / n_seeds
        citers = shared_citers.get(work_id, 0) / n_seeds
        cocite = cocitations.get(work_id, 0) / max_cocite
        return (
            _W_COUPLING * coupling
            + _W_SHARED_CITERS * citers
            + _W_COCITATION * cocite
        )

    # Hydrate only what we'll return — metadata for thousands of candidates is
    # the expensive part, and the ranking above needs no metadata at all.
    top = sorted(candidates, key=_score, reverse=True)[: max(limit * 2, limit)]
    works = await apis.openalex_works_by_ids(tuple(top))

    exclude_norm = {
        zotero._normalize_doi(d) for d in (exclude_dois or []) if d
    }
    zot_index = await zotero.get_doi_index()

    items: list[RelatedWork] = []
    for work in works:
        work_id = (work.get("id") or "").split("/")[-1]
        if not work_id:
            continue
        raw_doi = (work.get("doi") or "").replace("https://doi.org/", "") or None
        doi_norm = zotero._normalize_doi(raw_doi) if raw_doi else None
        if doi_norm and doi_norm in exclude_norm:
            continue

        source = (work.get("primary_location") or {}).get("source") or {}
        items.append(RelatedWork(
            openalex_id=work_id,
            title=work.get("title") or "Untitled",
            doi=raw_doi,
            authors=[
                (a.get("author") or {}).get("display_name", "")
                for a in (work.get("authorships") or [])[:4]
            ],
            year=work.get("publication_year"),
            venue=source.get("display_name"),
            cited_by_count=work.get("cited_by_count") or 0,
            abstract=reconstruct_abstract(work.get("abstract_inverted_index")) or None,
            in_zotero=bool(doi_norm and doi_norm in zot_index),
            shared_references=shared_references.get(work_id, 0),
            shared_citers=shared_citers.get(work_id, 0),
            cocitations=cocitations.get(work_id, 0),
            score=_score(work_id),
        ))

    items.sort(key=lambda i: i.score, reverse=True)
    return DiscoverResult(
        seeds=seeds,
        unresolved_seeds=unresolved,
        items=items[:limit],
    )
