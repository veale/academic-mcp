"""core.paper — metadata lookup for a single paper identifier."""

from __future__ import annotations

import asyncio
import logging

from .types import PaperInfo, PdfUrlInfo
from .search import reconstruct_abstract as _reconstruct_abstract_local

logger = logging.getLogger(__name__)


async def get_paper(identifier: str) -> PaperInfo:
    """Fetch paper metadata from S2, OpenAlex, and Unpaywall in parallel.

    *identifier* may be a DOI (with or without ``https://doi.org/`` prefix),
    a Semantic Scholar paper ID, or an ArXiv ID.

    Returns a :class:`PaperInfo` with all available fields populated.  All
    API errors are swallowed — empty fields indicate an unavailable source.
    """
    from .. import apis
    from ..zotero import _normalize_doi

    async def _s2() -> dict | None:
        try:
            return await apis.s2_paper(f"DOI:{identifier}")
        except Exception:
            try:
                return await apis.s2_paper(identifier)
            except Exception as e:
                logger.debug("S2 lookup failed: %s", e)
                return None

    async def _oa() -> dict | None:
        try:
            return await apis.openalex_work(identifier)
        except Exception as e:
            logger.debug("OpenAlex lookup failed: %s", e)
            return None

    s2_paper, oa_paper = await asyncio.gather(_s2(), _oa())

    # Resolve canonical DOI from API results, fall back to the raw identifier.
    doi: str | None = None
    if s2_paper:
        doi = apis.extract_doi(s2_paper)
    if not doi and oa_paper:
        doi = apis.extract_doi(oa_paper)
    if not doi:
        doi = identifier

    unpaywall_data: dict | None = None
    try:
        unpaywall_data = await apis.unpaywall_lookup(doi)
    except Exception as e:
        logger.debug("Unpaywall lookup failed: %s", e)

    # Build PaperInfo from whichever source responded.
    if s2_paper:
        authors = [a.get("name", "") for a in (s2_paper.get("authors") or [])]
        tldr_obj = s2_paper.get("tldr")
        tldr = tldr_obj.get("text", "") if tldr_obj else None
        title = s2_paper.get("title") or (oa_paper or {}).get("title") or "Unknown"
        info = PaperInfo(
            identifier=identifier,
            title=title,
            doi=doi,
            s2_id=s2_paper.get("paperId"),
            authors=authors,
            year=s2_paper.get("year"),
            venue=s2_paper.get("venue"),
            citation_count=s2_paper.get("citationCount"),
            reference_count=s2_paper.get("referenceCount"),
            abstract=s2_paper.get("abstract"),
            tldr=tldr,
            is_oa=bool((unpaywall_data or {}).get("is_oa", False)),
        )
    elif oa_paper:
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in (oa_paper.get("authorships") or [])[:10]
        ]
        abstract = _reconstruct_abstract_local(oa_paper.get("abstract_inverted_index"))
        info = PaperInfo(
            identifier=identifier,
            title=oa_paper.get("title") or "Unknown",
            doi=doi,
            authors=authors,
            year=oa_paper.get("publication_year"),
            citation_count=oa_paper.get("cited_by_count"),
            abstract=abstract,
            is_oa=bool((unpaywall_data or {}).get("is_oa", False)),
        )
    else:
        info = PaperInfo(
            identifier=identifier,
            title="Unknown",
            doi=doi,
            is_oa=bool((unpaywall_data or {}).get("is_oa", False)),
        )

    # OpenAlex type / container (for book-chapter hints in the handler).
    if oa_paper:
        info.oa_type = (oa_paper.get("type") or "").lower()
        primary_loc = oa_paper.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        info.oa_container = source.get("display_name")

    # PDF URLs.
    pdf_raw = apis.collect_pdf_urls(s2_paper, oa_paper, unpaywall_data)
    info.pdf_urls = [PdfUrlInfo(url=p["url"], source=p["source"]) for p in pdf_raw]

    return info


# ---------------------------------------------------------------------------
# PDF URL discovery (lightweight — skips full metadata assembly)
# ---------------------------------------------------------------------------

class PdfUrlResult:
    """Result from :func:`find_pdf_urls`."""
    __slots__ = ("doi", "candidates", "is_oa", "oa_status", "journal_is_oa")

    def __init__(
        self,
        doi: str,
        candidates: list[PdfUrlInfo],
        is_oa: bool = False,
        oa_status: str = "",
        journal_is_oa: bool = False,
    ) -> None:
        self.doi = doi
        self.candidates = candidates
        self.is_oa = is_oa
        self.oa_status = oa_status
        self.journal_is_oa = journal_is_oa


async def find_pdf_urls(doi: str) -> PdfUrlResult:
    """Discover open-access PDF URL candidates for *doi*.

    Queries S2, OpenAlex, and Unpaywall in parallel and returns a
    :class:`PdfUrlResult` with the aggregated candidates.
    """
    from .. import apis

    async def _s2() -> dict | None:
        try:
            return await apis.s2_paper(f"DOI:{doi}")
        except Exception:
            return None

    async def _oa() -> dict | None:
        try:
            return await apis.openalex_work(doi)
        except Exception:
            return None

    async def _unpaywall() -> dict | None:
        try:
            return await apis.unpaywall_lookup(doi)
        except Exception:
            return None

    s2_paper, oa_paper, unpaywall_data = await asyncio.gather(_s2(), _oa(), _unpaywall())

    pdf_raw = apis.collect_pdf_urls(s2_paper, oa_paper, unpaywall_data)
    candidates = [PdfUrlInfo(url=p["url"], source=p["source"]) for p in pdf_raw]

    is_oa = bool((unpaywall_data or {}).get("is_oa", False))
    oa_status = (unpaywall_data or {}).get("oa_status", "")
    journal_is_oa = bool((unpaywall_data or {}).get("journal_is_oa", False))

    return PdfUrlResult(doi, candidates, is_oa, oa_status, journal_is_oa)

