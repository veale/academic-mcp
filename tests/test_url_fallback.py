"""Tests for URL-based fallback for DOI-less items (fetch_fulltext url= support)."""

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_text_content(text: str):
    from mcp.types import TextContent
    return TextContent(type="text", text=text)


def _url_cache_key(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"url:{h}"


# ---------------------------------------------------------------------------
# Phase 2 — argument validation
# ---------------------------------------------------------------------------

class TestHandleFetchPdfValidation:
    """fetch_fulltext input validation tests."""

    @pytest.mark.asyncio
    async def test_rejects_when_all_three_missing(self):
        """No doi, no zotero_key, no url → error message."""
        from academic_mcp.server import _handle_fetch_pdf

        result = await _handle_fetch_pdf({})
        assert len(result) == 1
        assert "at least one of" in result[0].text
        assert "doi" in result[0].text
        assert "zotero_key" in result[0].text
        assert "url" in result[0].text

    @pytest.mark.asyncio
    async def test_old_error_message_is_gone(self):
        """Old 'requires either doi or zotero_key' wording must not appear."""
        from academic_mcp.server import _handle_fetch_pdf

        result = await _handle_fetch_pdf({})
        assert "requires either" not in result[0].text

    @pytest.mark.asyncio
    async def test_accepts_url_only(self):
        """url alone should not raise a validation error."""
        from academic_mcp import text_cache, config
        from academic_mcp.server import _handle_fetch_pdf

        test_url = "https://thesis.example.org/abc.pdf"
        cache_key = _url_cache_key(test_url)

        # Populate cache so the function returns immediately after validation.
        cached = MagicMock()
        cached.text = "Thesis full text"
        cached.sections = []
        cached.section_detection = "text_heuristic"
        cached.word_count = 100
        cached.metadata = {}
        cached.doi = cache_key

        with patch.object(text_cache, "get_cached", return_value=cached):
            result = await _handle_fetch_pdf({"url": test_url, "mode": "full"})

        # Should have got a result (even if just cache), not a validation error
        assert len(result) == 1
        assert "requires" not in result[0].text

    @pytest.mark.asyncio
    async def test_url_hash_cache_key_is_synthesized(self):
        """When only url is given, doi is synthesized as url:<sha256_prefix>."""
        from academic_mcp import text_cache
        from academic_mcp.server import _handle_fetch_pdf

        test_url = "https://thesis.example.org/paper.pdf"
        expected_key = _url_cache_key(test_url)

        captured_doi = []

        original_get = text_cache.get_cached

        def fake_get(doi):
            captured_doi.append(doi)
            return None  # cache miss — trigger full pipeline

        with patch.object(text_cache, "get_cached", side_effect=fake_get):
            # Short-circuit the pipeline: patch fetch_direct to immediately fail
            with patch("academic_mcp.pdf_fetcher.fetch_direct", return_value=None):
                with patch("academic_mcp.pdf_fetcher.fetch_with_scrapling",
                           return_value=(None, None, None)):
                    with patch("academic_mcp.config.config.use_stealth_browser", False):
                        await _handle_fetch_pdf({"url": test_url})

        assert any(d == expected_key for d in captured_doi), (
            f"Expected cache key {expected_key!r} but got {captured_doi}"
        )


# ---------------------------------------------------------------------------
# Phase 3 — URL tier behaviour
# ---------------------------------------------------------------------------

class TestUrlTierDirectPdf:
    """URL tier: direct PDF download path."""

    @pytest.mark.asyncio
    async def test_url_ending_in_pdf_triggers_direct_download(self, tmp_path):
        """A .pdf URL → pdf_fetcher.fetch_direct is called and result is cached."""
        from academic_mcp import text_cache, config
        from academic_mcp.server import _handle_fetch_pdf

        test_url = "https://university.example/thesis.pdf"
        cache_key = _url_cache_key(test_url)

        # Create a fake PDF file
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake content")

        cached_article = MagicMock()
        cached_article.text = "Extracted thesis text"
        cached_article.sections = [{"title": "Introduction", "start": 0, "end": 100}]
        cached_article.section_detection = "text_heuristic"
        cached_article.word_count = 3
        cached_article.metadata = {}
        cached_article.doi = cache_key

        with patch("academic_mcp.pdf_fetcher.fetch_direct", return_value=fake_pdf) as mock_fetch, \
             patch("academic_mcp.pdf_extractor.extract_text_with_sections",
                   return_value={"text": "Thesis", "sections": [], "section_detection": "text_heuristic",
                                 "metadata": {}}), \
             patch.object(text_cache, "get_cached", side_effect=[None, None, cached_article]), \
             patch.object(text_cache, "put_cached", return_value=cached_article), \
             patch("academic_mcp.config.config.use_stealth_browser", False), \
             patch("academic_mcp.config.config.use_pymupdf4llm", False):
            result = await _handle_fetch_pdf({"url": test_url, "mode": "sections"})

        # fetch_direct must have been called with our URL
        mock_fetch.assert_called_once_with(test_url)
        assert result is not None

    @pytest.mark.asyncio
    async def test_url_landing_page_falls_back_to_stealth_extraction(self):
        """Non-PDF URL with stealth browser enabled → _extract_from_landing_page called."""
        from academic_mcp import text_cache
        from academic_mcp.server import _handle_fetch_pdf
        from academic_mcp.core.fetch import _extract_from_landing_page

        test_url = "https://university.example/thesis"
        cache_key = _url_cache_key(test_url)

        cached_article = MagicMock()
        cached_article.text = "Extracted thesis text"
        cached_article.sections = []
        cached_article.section_detection = "text_heuristic"
        cached_article.word_count = 500
        cached_article.metadata = {}
        cached_article.doi = cache_key

        lp_result = {
            "text": "Extracted thesis text", "source": "html_extraction (trafilatura)",
            "pdf_path": None, "sections": [], "section_detection": "text_heuristic",
            "word_count": 500,
        }

        with patch("academic_mcp.pdf_fetcher.fetch_direct", return_value=None), \
             patch("academic_mcp.core.fetch._extract_from_landing_page",
                   new=AsyncMock(return_value=lp_result)) as mock_lp, \
             patch.object(text_cache, "get_cached", side_effect=[None, None, cached_article]), \
             patch.object(text_cache, "put_cached", return_value=cached_article), \
             patch("academic_mcp.config.config.use_stealth_browser", True):
            result = await _handle_fetch_pdf({"url": test_url, "mode": "sections"})

        mock_lp.assert_called_once_with(test_url, False)
        assert result is not None


# ---------------------------------------------------------------------------
# Phase 2.3 — Zotero URL promotion
# ---------------------------------------------------------------------------

class TestZoteroUrlPromotion:
    """When zotero_key item has a url but no attachment, URL tier fires."""

    @pytest.mark.asyncio
    async def test_zotero_key_with_url_promotes_to_url_tier(self):
        """Zotero item with url but no PDF → URL is promoted, url tier fires."""
        from academic_mcp import text_cache, zotero_sqlite
        from academic_mcp.models import ZoteroItem
        from academic_mcp.server import _handle_fetch_pdf

        thesis_url = "https://thesis.example/paper.pdf"
        zotero_key = "ZKEY1234"

        zot_item = ZoteroItem(
            itemID=1, key=zotero_key, libraryID=1, libraryName="My Library",
            libraryType="user", itemType="thesis", title="My Thesis",
            DOI="", url=thesis_url,
        )

        # Zotero returns no content (no attachment)
        zot_result_no_content = {
            "found": True, "item_key": zotero_key,
            "metadata": {"title": "My Thesis"},
            "text": None, "pdf_path": None, "source": "zotero_metadata_only",
        }

        fake_pdf = MagicMock(spec=Path)
        fake_pdf.__str__ = lambda self: "/tmp/fake.pdf"

        cached_article = MagicMock()
        cached_article.text = "Thesis content"
        cached_article.sections = []
        cached_article.section_detection = "text_heuristic"
        cached_article.word_count = 200
        cached_article.metadata = {}
        cached_article.doi = _url_cache_key(thesis_url)

        with patch("academic_mcp.zotero.get_paper_from_zotero_by_key",
                   new=AsyncMock(return_value=zot_result_no_content)), \
             patch.object(zotero_sqlite, "search_by_key",
                          new=AsyncMock(return_value=zot_item)), \
             patch("academic_mcp.pdf_fetcher.fetch_direct",
                   new=AsyncMock(return_value=fake_pdf)), \
             patch("academic_mcp.pdf_extractor.extract_text_with_sections",
                   return_value={"text": "Thesis content", "sections": [],
                                 "section_detection": "text_heuristic", "metadata": {}}), \
             patch.object(text_cache, "get_cached", side_effect=[None, None, cached_article]), \
             patch.object(text_cache, "put_cached", return_value=cached_article), \
             patch("academic_mcp.config.config.use_stealth_browser", False), \
             patch("academic_mcp.config.config.use_pymupdf4llm", False):
            result = await _handle_fetch_pdf({"zotero_key": zotero_key, "mode": "sections"})

        assert result is not None
        # Must NOT have returned the "no attachment" error
        assert not any("has no indexed fulltext" in r.text for r in result)


# ---------------------------------------------------------------------------
# Phase 1 — OpenAlex URL capture in search results
# ---------------------------------------------------------------------------

class TestOpenAlexUrlCapture:
    """OpenAlex search results should include url from primary_location."""

    @pytest.mark.asyncio
    async def test_openalex_search_result_captures_landing_page_url(self):
        """OpenAlex result with primary_location.landing_page_url → url in dict."""
        from academic_mcp.server import _handle_search

        oa_work = {
            "title": "A Thesis Without DOI",
            "doi": None,
            "publication_year": 2022,
            "cited_by_count": 5,
            "abstract_inverted_index": None,
            "authorships": [{"author": {"display_name": "Jane Doe"}}],
            "type": "dissertation",
            "open_access": {"is_oa": True},
            "primary_location": {
                "landing_page_url": "https://repo.university.edu/thesis/123",
                "pdf_url": None,
                "source": {"display_name": "University Repository"},
            },
        }

        oa_response = {"results": [oa_work], "meta": {"count": 1}}

        with patch("academic_mcp.apis.openalex_search",
                   new=AsyncMock(return_value=oa_response)), \
             patch("academic_mcp.apis.s2_search", new=AsyncMock(return_value={"data": []})), \
             patch("academic_mcp.zotero.search_zotero", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.apis.primo_search", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.zotero.get_doi_index", new=AsyncMock(return_value={})):
            result = await _handle_search({"query": "thesis test", "source": "openalex"})

        assert len(result) == 1
        assert "https://repo.university.edu/thesis/123" in result[0].text

    @pytest.mark.asyncio
    async def test_openalex_pdf_url_preferred_over_landing_page(self):
        """When both pdf_url and landing_page_url present, pdf_url wins."""
        from academic_mcp.server import _handle_search

        oa_work = {
            "title": "Direct PDF Paper",
            "doi": None,
            "publication_year": 2023,
            "cited_by_count": 0,
            "abstract_inverted_index": None,
            "authorships": [],
            "type": "report",
            "open_access": {"is_oa": True},
            "primary_location": {
                "landing_page_url": "https://repo.example.com/paper",
                "pdf_url": "https://repo.example.com/paper.pdf",
                "source": None,
            },
        }

        oa_response = {"results": [oa_work], "meta": {"count": 1}}

        with patch("academic_mcp.apis.openalex_search",
                   new=AsyncMock(return_value=oa_response)), \
             patch("academic_mcp.apis.s2_search", new=AsyncMock(return_value={"data": []})), \
             patch("academic_mcp.zotero.search_zotero", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.apis.primo_search", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.zotero.get_doi_index", new=AsyncMock(return_value={})):
            result = await _handle_search({"query": "report pdf", "source": "openalex"})

        # pdf_url takes priority
        assert "https://repo.example.com/paper.pdf" in result[0].text


# ---------------------------------------------------------------------------
# DOI wins when both DOI and URL are provided
# ---------------------------------------------------------------------------

class TestDoiWinsOverUrl:
    """When both doi and url are supplied, DOI path is taken."""

    @pytest.mark.asyncio
    async def test_doi_wins_when_both_provided(self):
        """doi + url → cache key is the DOI, not url:<hash>."""
        from academic_mcp import text_cache
        from academic_mcp.server import _handle_fetch_pdf

        test_doi = "10.1234/test.2024"
        test_url = "https://example.com/paper.pdf"

        captured_keys = []
        original_get = text_cache.get_cached

        def fake_get(doi):
            captured_keys.append(doi)
            return None

        with patch.object(text_cache, "get_cached", side_effect=fake_get), \
             patch("academic_mcp.zotero.get_paper_from_zotero",
                   new=AsyncMock(return_value=None)), \
             patch("academic_mcp.apis.s2_paper", new=AsyncMock(side_effect=Exception)), \
             patch("academic_mcp.apis.openalex_work", new=AsyncMock(side_effect=Exception)), \
             patch("academic_mcp.apis.unpaywall_lookup", new=AsyncMock(side_effect=Exception)), \
             patch("academic_mcp.apis.collect_pdf_urls", return_value=[]), \
             patch("academic_mcp.config.config.use_stealth_browser", False), \
             patch("academic_mcp.config.config.core_api_key", None), \
             patch("academic_mcp.config.config.serper_api_key", None), \
             patch("academic_mcp.config.config.brave_search_api_key", None):
            await _handle_fetch_pdf({"doi": test_doi, "url": test_url})

        # Cache key should be the real DOI, not a url: synthetic key
        assert any(k == test_doi for k in captured_keys), (
            f"Expected DOI as cache key but got: {captured_keys}"
        )
        assert not any(k.startswith("url:") for k in captured_keys), (
            f"url: synthetic key should not be used when doi is present"
        )


# ---------------------------------------------------------------------------
# Search output formatter
# ---------------------------------------------------------------------------

class TestSearchOutputFormatter:
    """Search result text output includes URL for DOI-less items."""

    @pytest.mark.asyncio
    async def test_url_shown_for_doi_less_result(self):
        """Result with url but no doi shows URL line and fetch_fulltext(url=...) hint."""
        from academic_mcp.server import _handle_search

        oa_work = {
            "title": "A Thesis",
            "doi": None,
            "publication_year": 2021,
            "cited_by_count": 0,
            "abstract_inverted_index": None,
            "authorships": [{"author": {"display_name": "Author A"}}],
            "type": "dissertation",
            "open_access": {"is_oa": False},
            "primary_location": {
                "landing_page_url": "https://etheses.example.ac.uk/thesis/99",
                "pdf_url": None,
                "source": None,
            },
        }

        oa_response = {"results": [oa_work]}

        with patch("academic_mcp.apis.openalex_search",
                   new=AsyncMock(return_value=oa_response)), \
             patch("academic_mcp.apis.s2_search", new=AsyncMock(return_value={"data": []})), \
             patch("academic_mcp.zotero.search_zotero", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.apis.primo_search", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.zotero.get_doi_index", new=AsyncMock(return_value={})):
            result = await _handle_search({"query": "thesis", "source": "openalex"})

        output = result[0].text
        # Should show the URL
        assert "https://etheses.example.ac.uk/thesis/99" in output
        # Should suggest fetch_fulltext(url=...)
        assert 'fetch_fulltext(url=' in output

    @pytest.mark.asyncio
    async def test_url_not_shown_for_doi_result(self):
        """Result with both doi and url: URL line should NOT appear (DOI is canonical)."""
        from academic_mcp.server import _handle_search

        oa_work = {
            "title": "A Journal Article",
            "doi": "https://doi.org/10.1234/test",
            "publication_year": 2020,
            "cited_by_count": 42,
            "abstract_inverted_index": None,
            "authorships": [],
            "type": "journal-article",
            "open_access": {"is_oa": True},
            "primary_location": {
                "landing_page_url": "https://example.com/article",
                "pdf_url": "https://example.com/article.pdf",
                "source": {"display_name": "Example Journal"},
            },
        }

        oa_response = {"results": [oa_work]}

        with patch("academic_mcp.apis.openalex_search",
                   new=AsyncMock(return_value=oa_response)), \
             patch("academic_mcp.apis.s2_search", new=AsyncMock(return_value={"data": []})), \
             patch("academic_mcp.zotero.search_zotero", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.apis.primo_search", new=AsyncMock(return_value=[])), \
             patch("academic_mcp.zotero.get_doi_index", new=AsyncMock(return_value={})):
            result = await _handle_search({"query": "journal article", "source": "openalex"})

        output = result[0].text
        # URL line should NOT appear when DOI is present
        assert "URL: https://example.com" not in output
