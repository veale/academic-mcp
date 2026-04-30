"""core.citations — citation / reference graph lookups."""

from __future__ import annotations

import asyncio
import logging

from .types import CitationWorkItem, CitationsResult, CitationTreeResult, BookChaptersResult
from .search import reconstruct_abstract as _reconstruct_abstract

logger = logging.getLogger(__name__)


def normalize_exclude_dois(exclude_dois: list[str] | None) -> set[str]:
    """Normalise user-supplied DOIs for exclusion filtering."""
    if not exclude_dois:
        return set()
    from ..zotero import _normalize_doi
    return {_normalize_doi(d) for d in exclude_dois if d}


def _filter_works(works: list[dict], exclude_norm: set[str]) -> tuple[list[dict], int]:
    """Remove works whose normalised DOI is in *exclude_norm*."""
    if not exclude_norm:
        return works, 0
    from ..zotero import _normalize_doi
    kept: list[dict] = []
    dropped = 0
    for w in works:
        raw = (w.get("doi") or "").replace("https://doi.org/", "")
        if raw and _normalize_doi(raw) in exclude_norm:
            dropped += 1
        else:
            kept.append(w)
    return kept, dropped


def _work_to_item(work: dict, zot_index: set[str]) -> CitationWorkItem:
    """Convert an OpenAlex work dict to a CitationWorkItem."""
    from ..zotero import _normalize_doi

    raw_doi = (work.get("doi") or "").replace("https://doi.org/", "")
    doi_norm = _normalize_doi(raw_doi) if raw_doi else None

    authorships = work.get("authorships") or []
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in authorships[:4]
    ]

    primary_loc = work.get("primary_location") or {}
    source = primary_loc.get("source") or {}
    oa_id = (work.get("id") or "").split("/")[-1]

    abstract_inv = work.get("abstract_inverted_index")
    abstract = _reconstruct_abstract(abstract_inv) if abstract_inv else None

    return CitationWorkItem(
        title=work.get("title") or "Untitled",
        doi=raw_doi or None,
        openalex_id=oa_id or None,
        authors=authors,
        year=work.get("publication_year"),
        venue=source.get("display_name"),
        cited_by_count=work.get("cited_by_count") or 0,
        abstract=abstract,
        in_zotero=bool(doi_norm and doi_norm in zot_index),
    )


async def get_citations(
    doi: str,
    *,
    keywords: str | None = None,
    limit: int = 25,
    start_year: int | None = None,
    end_year: int | None = None,
    openalex_id: str | None = None,
    exclude_dois: list[str] | None = None,
) -> CitationsResult:
    """Fetch forward citations (papers that cite *doi*) from OpenAlex."""
    from .. import apis, zotero

    limit = min(limit, 50)
    exclude_norm = normalize_exclude_dois(exclude_dois)
    fetch_limit = min(limit + len(exclude_norm), 200) if exclude_norm else limit

    data, zot_index = await asyncio.gather(
        apis.openalex_citations(
            doi, search=keywords, limit=fetch_limit,
            start_year=start_year, end_year=end_year,
            openalex_id=openalex_id,
        ),
        zotero.get_doi_index(),
        return_exceptions=True,
    )

    if isinstance(data, Exception):
        return CitationsResult(doi=doi, direction="citations", total=0, items=[], error=str(data))
    if isinstance(zot_index, Exception):
        zot_index = set()

    works = data.get("results", [])
    works, dropped = _filter_works(works, exclude_norm)
    works = works[:limit]
    total = data.get("meta", {}).get("count", len(works))
    items = [_work_to_item(w, zot_index) for w in works]
    return CitationsResult(doi=doi, direction="citations", total=total, items=items, dropped=dropped)


async def get_references(
    doi: str,
    *,
    keywords: str | None = None,
    limit: int = 25,
    start_year: int | None = None,
    end_year: int | None = None,
    openalex_id: str | None = None,
    exclude_dois: list[str] | None = None,
) -> CitationsResult:
    """Fetch backward references (papers cited by *doi*) from OpenAlex."""
    from .. import apis, zotero

    limit = min(limit, 50)
    exclude_norm = normalize_exclude_dois(exclude_dois)
    fetch_limit = min(limit + len(exclude_norm), 200) if exclude_norm else limit

    data, zot_index = await asyncio.gather(
        apis.openalex_references(
            doi, search=keywords, limit=fetch_limit,
            start_year=start_year, end_year=end_year,
            openalex_id=openalex_id,
        ),
        zotero.get_doi_index(),
        return_exceptions=True,
    )

    if isinstance(data, Exception):
        return CitationsResult(doi=doi, direction="references", total=0, items=[], error=str(data))
    if isinstance(zot_index, Exception):
        zot_index = set()

    works = data.get("results", [])
    works, dropped = _filter_works(works, exclude_norm)
    works = works[:limit]
    total = data.get("meta", {}).get("count", len(works))
    items = [_work_to_item(w, zot_index) for w in works]
    return CitationsResult(doi=doi, direction="references", total=total, items=items, dropped=dropped)


async def get_citation_tree(
    doi: str,
    *,
    keywords: str | None = None,
    limit: int = 10,
    start_year: int | None = None,
    end_year: int | None = None,
    openalex_id: str | None = None,
    exclude_dois: list[str] | None = None,
) -> CitationTreeResult:
    """Fetch both citations and references concurrently."""
    from .. import apis, zotero

    limit = min(limit, 25)
    exclude_norm = normalize_exclude_dois(exclude_dois)
    fetch_limit = min(limit + len(exclude_norm), 200) if exclude_norm else limit

    # Resolve OpenAlex ID once to avoid two separate openalex_work() calls.
    if not openalex_id:
        try:
            resolved_id = await apis._resolve_openalex_filter_id(doi)
        except Exception:
            resolved_id = None
    else:
        resolved_id = openalex_id

    cit_data, ref_data, zot_index = await asyncio.gather(
        apis.openalex_citations(
            doi, search=keywords, limit=fetch_limit,
            start_year=start_year, end_year=end_year,
            openalex_id=resolved_id,
        ),
        apis.openalex_references(
            doi, search=keywords, limit=fetch_limit,
            start_year=start_year, end_year=end_year,
            openalex_id=resolved_id,
        ),
        zotero.get_doi_index(),
        return_exceptions=True,
    )
    if isinstance(zot_index, Exception):
        zot_index = set()

    def _to_result(data: dict | Exception, direction: str) -> CitationsResult:
        if isinstance(data, Exception):
            return CitationsResult(doi=doi, direction=direction, total=0, items=[], error=str(data))
        works = data.get("results", [])
        works, dropped = _filter_works(works, exclude_norm)
        works = works[:limit]
        total = data.get("meta", {}).get("count", len(works))
        return CitationsResult(
            doi=doi, direction=direction, total=total, dropped=dropped,
            items=[_work_to_item(w, zot_index) for w in works],
        )

    return CitationTreeResult(
        doi=doi,
        citations=_to_result(cit_data, "citations"),
        references=_to_result(ref_data, "references"),
    )


# ---------------------------------------------------------------------------
# Book chapters
# ---------------------------------------------------------------------------

BOOK_TYPES: frozenset[str] = frozenset({"book", "edited-book", "monograph", "reference-book"})
CHAPTER_TYPES: frozenset[str] = frozenset({"book-chapter", "reference-entry", "book-part", "book-section"})


def _page_sort_key(item: dict) -> tuple:
    """Sort chapters by first page number when available; otherwise by title."""
    page = item.get("page") or ""
    first = page.split("-")[0].strip()
    try:
        return (0, int(first))
    except (TypeError, ValueError):
        return (1, (item.get("title") or [""])[0].lower())


async def get_book_chapters(
    doi: str,
    isbn: str,
    keywords: str | None,
    limit: int,
) -> BookChaptersResult:
    """Resolve book chapters from Crossref given a seed DOI or ISBN."""
    from .. import apis
    from ..zotero import _normalize_doi

    raw_doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "") or None
    seed: dict | None = None
    seed_type: str = ""
    isbns: list[str] = []
    container_title: str | None = None

    if raw_doi:
        try:
            seed = await apis.crossref_work(raw_doi)
        except Exception as e:
            logger.debug("Crossref lookup failed for %s: %s", raw_doi, e)
        if seed:
            seed_type = (seed.get("type") or "").lower()
            isbns = [apis._normalize_isbn(i) for i in (seed.get("ISBN") or []) if i]
            ct_list = seed.get("container-title") or []
            if ct_list:
                container_title = ct_list[0]
            elif seed_type in BOOK_TYPES:
                title_list = seed.get("title") or []
                container_title = title_list[0] if title_list else None

    if isbn:
        isbns.insert(0, apis._normalize_isbn(isbn))
    isbns = list(dict.fromkeys([i for i in isbns if i]))

    if not isbns and not container_title:
        return BookChaptersResult(
            seed_doi=raw_doi or "",
            error=(
                f"Could not resolve a book identifier from {doi or isbn}. "
                "Crossref returned no ISBN or container-title. Try passing the ISBN directly "
                "via the 'isbn' parameter."
            ),
        )

    items: list[dict] = []
    seen: set[str] = set()
    for _isbn in isbns:
        try:
            batch = await apis.crossref_book_chapters(isbn=_isbn, keywords=keywords, limit=limit)
        except Exception as e:
            logger.warning("Crossref chapter query failed for ISBN %s: %s", _isbn, e)
            continue
        for it in batch:
            d = (it.get("DOI") or "").lower()
            if d and d not in seen:
                seen.add(d)
                items.append(it)

    if not items and container_title:
        try:
            batch = await apis.crossref_book_chapters(
                container_title=container_title, keywords=keywords, limit=limit,
            )
        except Exception as e:
            logger.warning("Crossref chapter query failed for title %r: %s", container_title, e)
            batch = []
        for it in batch:
            d = (it.get("DOI") or "").lower()
            if d and d not in seen:
                seen.add(d)
                items.append(it)

    items.sort(key=_page_sort_key)
    items = items[:limit]

    book_title = container_title or ""
    if seed and seed_type in BOOK_TYPES and not book_title:
        title_list = seed.get("title") or []
        book_title = title_list[0] if title_list else ""

    return BookChaptersResult(
        items=items,
        book_title=book_title,
        isbns=isbns,
        seed_type=seed_type,
        seed_doi=raw_doi or "",
    )
