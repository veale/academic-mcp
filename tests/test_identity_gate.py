"""Identity verification: wrong documents must never be returned, cached or
imported under a requested identifier.  Cases are drawn from feedback reports
(fb_3d4e8e21a47c, fb_1b855919be12, fb_0a9cc94b931c, fb_6ef79c2ac501,
fb_1c5fef4ce4d3, fb_53cef54daf86, fb_bc5450fe3fce)."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from academic_mcp import identity, text_cache, zotero_import, zotero_sqlite
from academic_mcp.core import fetch
from academic_mcp.core.types import ArticleId

KAEDING = "Determinants of Transposition Delay in the European Union"
GREENER = "Performative Compliance and the State-Corporate Structuring of Neglect"
STODDEN = "An empirical analysis of journal policy effectiveness for computational reproducibility"

BODY = " ".join(["Regulatory compliance in organisations is studied here at length."] * 400)


def _paper(title: str, author: str, prefix: str = "") -> str:
    return f"{prefix}{title}\n{author}\nUniversity of Somewhere\n\nAbstract\nThis article examines {BODY}"


# ---------------------------------------------------------------------------
# check_identity
# ---------------------------------------------------------------------------

def test_title_on_first_page_passes():
    check = identity.check_identity(_paper(KAEDING, "Michael Kaeding"), KAEDING, ["Kaeding"])
    assert check.status == identity.PASS


def test_line_broken_hyphenated_title_passes():
    text = "Determinants of Trans-\nposition Delay in the Euro-\npean Union\nMICHAEL KAEDING\n" + BODY
    assert identity.check_identity(text, KAEDING, ["Kaeding"]).status == identity.PASS


def test_cover_sheet_then_title_passes():
    cover = ("This content downloaded from 128.40.0.1 on Mon, 01 Jan 2024\n"
             "All use subject to https://about.jstor.org/terms\n")
    check = identity.check_identity(_paper(KAEDING, "Michael Kaeding", prefix=cover), KAEDING, ["Kaeding"])
    assert check.status == identity.PASS


def test_different_paper_citing_requested_one_is_mismatch():
    # König & Luetgert returned for Kaeding's DOI (fb_3d4e8e21a47c).
    text = _paper(
        "Troubles with Transposition? Explaining Trends in Member-State Notification "
        "and the Delayed Transposition of EU Directives", "Thomas König and Brooke Luetgert",
    ) + "\n\nReferences\nKaeding, M. (2006) Determinants of Transposition Delay in the European Union."
    check = identity.check_identity(text, KAEDING, ["Kaeding"])
    assert check.status == identity.MISMATCH
    assert "Troubles with Transposition" in check.found_title


def test_same_author_different_paper_is_mismatch():
    # Greener 2019 returned a different Greener paper (fb_1c5fef4ce4d3).
    text = _paper("COVID-19 and the Necrocapitalist Production of Death", "Joe Greener")
    assert identity.check_identity(text, GREENER, ["Greener"]).status == identity.MISMATCH


def test_unrelated_paper_is_mismatch():
    # Miske et al. 2026 returned for Stodden 2018 (fb_53cef54daf86).
    text = _paper("Investigating the reproducibility of the social and behavioural sciences", "Olivia Miske")
    assert identity.check_identity(text, STODDEN, ["Stodden", "Seiler", "Ma"]).status == identity.MISMATCH


def test_title_only_in_late_footnote_is_not_a_pass():
    text = ("On Further Reflection: How Professional Self-Regulation Can Promote Compliance\n"
            "Ted Schneyer\n" + "x " * 1500 +
            f"\n1 Parker, Gordon & Mark, {KAEDING}, 37 J. Law & Soc. 466 (2010).\n" + BODY)
    assert identity.check_identity(text, KAEDING, ["Parker"]).status != identity.PASS


def test_cv_is_mismatch():
    text = f"CURRICULUM VITAE\nSelected publications\n{KAEDING}. J Public Policy 2006.\n" + BODY
    assert identity.check_identity(text, KAEDING, ["Kaeding"]).status == identity.MISMATCH


def test_pdf_title_metadata_passes():
    check = identity.check_identity(BODY, KAEDING, ["Kaeding"], pdf_title=KAEDING)
    assert check.status == identity.PASS


def test_hyphenation_variant_title_passes():
    # LSE repository copy of Power (2021): Crossref says "Micro-Foundations".
    text = ("Power, Michael (2021) Modelling the microfoundations of the audit society: "
            "organizations and the logic of the audit trail. Academy of Management Review.\n" + BODY)
    expected = ("Modelling the Micro-Foundations of the Audit Society: "
                "Organizations and the Logic of the Audit Trail")
    assert identity.check_identity(text, expected, ["Power"]).status == identity.PASS


def test_retitled_version_by_same_authors_is_uncertain_not_mismatch():
    text = ("PENTESTGPT: Evaluating and Harnessing Large Language Models for Automated "
            "Penetration Testing\nGelei Deng, Yi Liu\n" + BODY)
    check = identity.check_identity(
        text, "PentestGPT: An LLM-empowered Automatic Penetration Testing Tool", ["Deng", "Liu"])
    assert check.status == identity.UNCERTAIN


def test_html_body_without_title_is_judged_by_subject():
    expected = "Testosterone dose-response relationships in healthy young men"
    body = ("We administered graded testosterone doses to young men and measured responses. "
            "Dose-response relationships were healthy and clear. ") * 50
    assert identity.check_identity(body, expected, html=True).status != identity.MISMATCH
    other = "Similar content being viewed by others. Rice cultivation in Amazonian lakes. " * 50
    assert identity.check_identity(other, expected, html=True).status == identity.MISMATCH


def test_page_title_metadata_decides_html():
    check = identity.check_identity("Body without the title. " * 100, KAEDING, html=True,
                                    page_title="Determinants of Transposition Delay in the European Union")
    assert check.status == identity.PASS
    check = identity.check_identity("Body. " * 100, KAEDING, html=True,
                                    page_title="Troubles with Transposition? Explaining Trends")
    assert check.status == identity.MISMATCH


def test_short_expected_title_is_not_checkable():
    assert identity.check_identity(BODY, "Compliance", []).status == identity.NOT_CHECKABLE


def test_cover_sheet_only_and_no_text_layer_warnings():
    cover = "SAGE Social Science Collections. All Rights Reserved."
    assert any(w.startswith("cover_sheet_only") for w in identity.assess_content(cover))
    assert any(w.startswith("no_text_layer") for w in identity.assess_content("", pages=320))
    assert identity.assess_content(BODY) == []


# ---------------------------------------------------------------------------
# Fetch pipeline gate
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.config, "pdf_cache_dir", tmp_path)
    monkeypatch.setattr(text_cache.config, "pdf_cache_dir", tmp_path)
    monkeypatch.setattr(fetch.config, "use_pymupdf4llm", True)
    return tmp_path


def _extract_returning(text: str, pdf_title: str = ""):
    return AsyncMock(return_value={
        "text": text, "sections": [], "section_detection": "text_heuristic",
        "metadata": {"title": pdf_title, "pages": 12}, "pages": 12,
    })


async def test_wrong_web_pdf_is_rejected_and_not_cached(cache_dir, monkeypatch):
    pdf = cache_dir / "abc123.pdf"
    pdf.write_bytes(b"%PDF-1.4 wrong")
    monkeypatch.setattr(fetch.pdf_work, "extract", _extract_returning(
        _paper("Investigating the reproducibility of the social and behavioural sciences", "Olivia Miske")))
    enqueue = AsyncMock()
    monkeypatch.setattr(fetch.zotero_import, "enqueue_zotero_import", enqueue)
    token = fetch._ctx.set(fetch._FetchContext())
    try:
        fetch._set_expected({"title": STODDEN, "authors": ["Stodden"]})
        result = await fetch._cache_pdf_and_return(
            pdf, "10.1073/pnas.1708290115", "web_search (serper)", None, "sections", None, None, None)
        rejections = fetch._current_ctx().rejections
    finally:
        fetch._ctx.reset(token)
    assert result is None
    assert text_cache.get_cached("10.1073/pnas.1708290115") is None
    assert not pdf.exists(), "the wrong download must not be reused from the URL cache"
    enqueue.assert_not_called()
    assert rejections and "reproducibility of the social" in rejections[0]["found_title"]


async def test_right_pdf_is_cached_with_verified_identity(cache_dir, monkeypatch):
    pdf = cache_dir / "ok.pdf"
    pdf.write_bytes(b"%PDF-1.4 ok")
    monkeypatch.setattr(fetch.pdf_work, "extract", _extract_returning(
        _paper(STODDEN, "Victoria Stodden"), pdf_title="Microsoft Word - final.docx"))
    token = fetch._ctx.set(fetch._FetchContext())
    try:
        fetch._set_expected({"title": STODDEN, "authors": ["Stodden"], "year": "2018"})
        result = await fetch._cache_pdf_and_return(
            pdf, "10.1073/pnas.1708290115", "unpaywall (direct)", None, "sections", None, None, None)
    finally:
        fetch._ctx.reset(token)
    assert result is not None and result.identity["status"] == identity.PASS
    cached = text_cache.get_cached("10.1073/pnas.1708290115")
    assert cached.metadata["identity"]["status"] == identity.PASS
    # Citation shows the requested record, not the PDF's own metadata.
    assert cached.metadata["title"] == STODDEN


async def test_search_result_needs_positive_match(cache_dir, monkeypatch):
    pdf = cache_dir / "ambiguous.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    text = "Working paper\n" + "x " * 2000 + f"\n{KAEDING}\nMichael Kaeding\n" + BODY
    monkeypatch.setattr(fetch.pdf_work, "extract", _extract_returning(text))
    token = fetch._ctx.set(fetch._FetchContext())
    try:
        fetch._set_expected({"title": KAEDING, "authors": ["Kaeding"]})
        result = await fetch._cache_pdf_and_return(
            pdf, "10.1017/s0143814x06000547", "web_search (brave)", None, "sections", None, None, None)
    finally:
        fetch._ctx.reset(token)
    assert result is None


async def test_users_own_zotero_attachment_is_kept_with_warning(cache_dir, monkeypatch):
    monkeypatch.setattr(fetch.pdf_work, "extract", _extract_returning(
        _paper("Building Better Compliance", "Cary Coglianese")))
    token = fetch._ctx.set(fetch._FetchContext())
    try:
        fetch._set_expected({"title": "Compliance Management Systems: Do They Make a Difference?"})
        result = await fetch._cache_pdf_and_return(
            Path("/library/storage/ABCD2345/x.pdf"), "10.1017/9781108759458.039",
            "sqlite_local_pdf", None, "sections", None, None, None)
    finally:
        fetch._ctx.reset(token)
    assert result is not None
    # The user's own file is kept and flagged for checking, never rejected.
    assert result.identity["status"] == identity.UNCERTAIN
    assert "check the attachment" in result.identity["reason"]


async def test_auto_imported_zotero_attachment_is_rejected(cache_dir, monkeypatch):
    monkeypatch.setattr(fetch.pdf_work, "extract", _extract_returning(
        _paper("Building Better Compliance", "Cary Coglianese")))
    token = fetch._ctx.set(fetch._FetchContext())
    try:
        fetch._current_ctx().zotero_untrusted = True
        fetch._set_expected({"title": "Compliance Management Systems: Do They Make a Difference?"})
        result = await fetch._cache_pdf_and_return(
            Path("/library/storage/ABCD2345/x.pdf"), "10.1017/9781108759458.039",
            "sqlite_local_pdf", None, "sections", None, None, None)
    finally:
        fetch._ctx.reset(token)
    assert result is None


async def test_legacy_wrong_cache_entry_is_purged_on_read(cache_dir, monkeypatch):
    doi = "10.1016/j.ssci.2020.104772"
    text_cache.put_cached(doi, _paper("Kesenian Wayang Orang di Surakarta", "Dwi Retno Sulanjari"),
                          "web_search (brave)", sections=[])
    monkeypatch.setattr(fetch, "_expected_for", AsyncMock(return_value={
        "title": "Safety and reliability in compliance: the role of safety clutter",
        "authors": ["Størkersen"]}))
    assert await fetch.verified_cached(doi) is None
    assert text_cache.get_cached(doi) is None


async def test_legacy_correct_cache_entry_is_annotated(cache_dir, monkeypatch):
    doi = "10.1073/pnas.1708290115"
    text_cache.put_cached(doi, _paper(STODDEN, "Victoria Stodden"), "unpaywall (direct)", sections=[])
    monkeypatch.setattr(fetch, "_expected_for", AsyncMock(return_value={"title": STODDEN, "authors": ["Stodden"]}))
    kept = await fetch.verified_cached(doi)
    assert kept is not None and kept.metadata["identity"]["status"] == identity.PASS
    # Second read does not look the record up again.
    fetch._expected_for.reset_mock()
    await fetch.verified_cached(doi)
    fetch._expected_for.assert_not_awaited()


async def test_range_beyond_text_is_an_explicit_error(cache_dir):
    doi = "10.1/range"
    text_cache.put_cached(doi, "short text " * 10, "html_extraction (x)", sections=[],
                          metadata={"identity": {"status": "pass"}})
    result = await fetch.fetch_article(ArticleId(doi=doi), mode="range", range_start=270000)
    assert result.error and "range_out_of_bounds" in result.error and "110" in result.error


# ---------------------------------------------------------------------------
# Zotero auto-import gate
# ---------------------------------------------------------------------------

async def test_unverified_text_is_never_imported_to_zotero(monkeypatch):
    monkeypatch.setattr(zotero_import.config, "auto_import_to_zotero", True)
    monkeypatch.setattr(zotero_import.zot_config, "api_key", "key")
    monkeypatch.setattr(zotero_import.zot_config, "user_id", "123")
    create = AsyncMock(return_value=("NEWITEM1", "", 200))
    monkeypatch.setattr(zotero_import, "_create_zotero_item", create)
    monkeypatch.setattr(zotero_import, "_doi_exists_in_zotero", AsyncMock(return_value=None))
    for verdict in (None, {"status": "uncertain"}, {"status": "mismatch"}):
        meta = {"title": "x"} if verdict is None else {"title": "x", "identity": verdict}
        article = text_cache.CachedArticle(doi="10.1234/a", text="", source="web_search (brave)",
                                           sections=[], metadata=meta)
        assert await zotero_import.import_to_zotero("10.1234/a", None, article) is True
    create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Library-first resolution: citekeys, zotero:// links, preprint URLs
# ---------------------------------------------------------------------------

@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CATALOG_ENABLED", "false")
    path = tmp_path / "zotero.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE libraries(libraryID INTEGER, type TEXT);INSERT INTO libraries VALUES(1,'user');
            CREATE TABLE groups(groupID INTEGER, libraryID INTEGER, name TEXT);
            CREATE TABLE fields(fieldID INTEGER,fieldName TEXT);
            INSERT INTO fields VALUES(1,'title'),(2,'url'),(3,'citationKey'),(4,'DOI'),(5,'extra');
            CREATE TABLE itemTypes(itemTypeID INTEGER,typeName TEXT);
            INSERT INTO itemTypes VALUES(1,'preprint'),(2,'attachment');
            CREATE TABLE items(itemID INTEGER, key TEXT, dateAdded TEXT, dateModified TEXT,
                               itemTypeID INTEGER, libraryID INTEGER);
            INSERT INTO items VALUES(1,'BAMB2009','2026-01-01','2026-01-01',1,1),
                                    (2,'SELB2021','2026-01-01','2026-01-01',1,1);
            CREATE TABLE deletedItems(itemID INTEGER);
            CREATE TABLE itemData(itemID INTEGER,fieldID INTEGER,valueID INTEGER);
            INSERT INTO itemData VALUES(1,1,1),(1,2,2),(1,3,3),(2,1,4),(2,2,5),(2,5,6);
            CREATE TABLE itemDataValues(valueID INTEGER,value TEXT);
            INSERT INTO itemDataValues VALUES
              (1,'Technologies of Compliance: Risk and Regulation in a Digital Age'),
              (2,'https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1463727'),
              (3,'bambergerTechnologiesComplianceRisk2009'),
              (4,'An Institutional View of Algorithmic Impact Assessments'),
              (5,'https://ssrn.com/abstract=3867634'),
              (6,'Citation Key: selbstInstitutionalViewAlgorithmic2021a');
            CREATE TABLE creators(creatorID INTEGER,firstName TEXT,lastName TEXT);
            CREATE TABLE creatorTypes(creatorTypeID INTEGER, creatorType TEXT);
            CREATE TABLE itemCreators(itemID INTEGER,creatorID INTEGER,creatorTypeID INTEGER,orderIndex INTEGER);
            CREATE TABLE itemTags(itemID INTEGER,tagID INTEGER);CREATE TABLE tags(tagID INTEGER,name TEXT);
        """)
    monkeypatch.setattr(zotero_sqlite.sqlite_config, "db_path", str(path))
    monkeypatch.setattr(zotero_sqlite, "_maybe_schedule_shadow_refresh", lambda *a: None)
    return path


async def test_citekey_native_field_resolves(library):
    item, via = await zotero_sqlite.resolve_reference("bambergerTechnologiesComplianceRisk2009")
    assert item.key == "BAMB2009" and via == "citekey"


async def test_citekey_in_extra_resolves(library):
    item, via = await zotero_sqlite.resolve_reference("selbstInstitutionalViewAlgorithmic2021a")
    assert item.key == "SELB2021"


async def test_zotero_select_link_resolves(library):
    item, via = await zotero_sqlite.resolve_reference("zotero://select/library/items/BAMB2009")
    assert item.key == "BAMB2009" and via == "item_key"


@pytest.mark.parametrize("url", [
    "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3867634",
    "https://ssrn.com/abstract=3867634",
    "http://dx.doi.org/10.2139/ssrn.3867634",
])
async def test_ssrn_url_spellings_resolve_to_library_item(library, url):
    item, via = await zotero_sqlite.resolve_reference(url)
    assert item is not None and item.key == "SELB2021"


async def test_unknown_citekey_is_reported_as_unknown(library):
    result = await fetch.fetch_article(ArticleId(zotero_key="nobodyNothing2020"), mode="sections")
    assert result.error.startswith("unknown_reference")
    assert "PDF attachment" not in result.error


async def test_search_result_exposes_key_and_citekey(library):
    item = await zotero_sqlite.search_by_key("BAMB2009")
    result = item.to_search_result()
    assert result["key"] == "BAMB2009"
    assert result["citationKey"] == "bambergerTechnologiesComplianceRisk2009"


async def test_publisher_page_declaring_the_doi_passes(cache_dir):
    token = fetch._ctx.set(fetch._FetchContext())
    try:
        fetch._set_expected({"title": KAEDING})
        cached = fetch._accept_text(
            "10.1017/s0143814x06000547", "Introduction. " * 300, "html_extraction (cambridge.org)",
            sections=[], section_detection="html_headings",
            page_doi="10.1017/S0143814X06000547")
    finally:
        fetch._ctx.reset(token)
    assert cached is not None and cached.metadata["identity"]["status"] == identity.PASS


def test_crossref_markup_in_expected_title_is_ignored():
    expected = "The judgment of the CJEU in\n   <i>Inspektor (Purposes of processing)</i> &amp; beyond"
    text = "The judgment of the CJEU in Inspektor (Purposes of processing) & beyond\nA. Author\n" + BODY
    assert identity.check_identity(text, expected).status == identity.PASS


def test_any_known_title_of_the_work_passes():
    text = ("Local AI Governance: Addressing Model Safety and Policy Challenges Posed by "
            "Decentralized AI\n" + BODY)
    check = identity.check_identity_any(text, [
        "AI Goes Off the Grid: The Rise of Local AI Demands Rethinking AI Governance",
        "Local AI Governance: Addressing Model Safety and Policy Challenges Posed by Decentralized AI",
    ])
    assert check.status == identity.PASS


async def test_cached_wrong_auto_imported_library_copy_is_purged(cache_dir, monkeypatch):
    doi = "10.1177/0170840612443623"
    text_cache.put_cached(doi, _paper("Building Information Standards", "Beemsterboer"),
                          "zotero_webdav_pdf", sections=[])
    monkeypatch.setattr(fetch, "_expected_for", AsyncMock(return_value={
        "title": "Making Standards Stick: A Theory of Coupled vs. Decoupled Compliance",
        "authors": ["Sandholtz"]}))
    monkeypatch.setattr(fetch, "_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(fetch, "_is_auto_imported", AsyncMock(return_value=True))
    assert await fetch.verified_cached(doi) is None


async def test_cached_users_own_library_copy_is_kept(cache_dir, monkeypatch):
    doi = "10.1/own"
    text_cache.put_cached(doi, _paper("EXPLANATORY MEMORANDUM on the proposal", "Commission"),
                          "zotero_webdav_pdf", sections=[])
    monkeypatch.setattr(fetch, "_expected_for", AsyncMock(return_value={"title": "Digital Fairness Act proposal draft"}))
    monkeypatch.setattr(fetch, "_published_titles", AsyncMock(return_value=[]))
    monkeypatch.setattr(fetch, "_is_auto_imported", AsyncMock(return_value=False))
    kept = await fetch.verified_cached(doi)
    assert kept is not None and kept.metadata["identity"]["status"] == identity.UNCERTAIN
    assert kept.metadata["auto_import_checked_v2"] is True


async def test_legacy_search_hit_must_positively_match(cache_dir, monkeypatch):
    doi = "10.1007/s10612-019-09467-3"
    text_cache.put_cached(doi, _paper("COVID-19 and the Necrocapitalist Production of Death in "
                                      "Residential Care Homes", "Joe Greener"),
                          "web_search (brave)", sections=[],
                          metadata={"identity": {"status": "uncertain", "reason": "an author matches"}})
    monkeypatch.setattr(fetch, "_expected_for", AsyncMock(return_value={
        "title": "Performative Compliance and the State-Corporate Structuring of Neglect in a "
                 "Residential Care Home for Older People", "authors": ["Greener"]}))
    monkeypatch.setattr(fetch, "_published_titles", AsyncMock(return_value=[]))
    assert await fetch.verified_cached(doi) is None


def test_silently_truncated_markdown_extraction_is_detected(tmp_path, monkeypatch):
    pdf = tmp_path / "thesis.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(fetch.config, "max_context_length", 100000)
    cut = text_cache.CachedArticle(doi="url:x", text="a" * 100000, source="url_direct_pdf",
                                   pdf_path=str(pdf))
    assert fetch._looks_truncated(cut)
    whole = text_cache.CachedArticle(doi="url:x", text="a" * 100000, source="url_direct_pdf",
                                     pdf_path=str(pdf), metadata={"full_extraction": True})
    assert not fetch._looks_truncated(whole)


async def test_ssrn_remap_to_a_different_work_is_ignored(monkeypatch):
    from academic_mcp import apis
    monkeypatch.setattr(apis, "_resolve_ssrn_doi_unverified", AsyncMock(return_value={
        "published_doi": "10.1017/9781108770941.003",
        # The merged aggregator record reports the other work's title.
        "title": "Measuring Compliance in the Age of Governance",
        "oa_pdf_urls": [], "all_dois": ["10.2139/ssrn.448221", "10.1017/9781108770941.003"]}))
    async def crossref(doi):
        if "ssrn" in doi:
            return {"title": ["Cosmetic Compliance and the Failure of Negotiated Governance"]}
        return {"title": ["Measuring Compliance in the Age of Governance"]}
    monkeypatch.setattr(apis, "crossref_work", crossref)
    result = await apis.resolve_ssrn_doi("10.2139/ssrn.448221")
    assert result["published_doi"] is None


async def test_auto_imported_same_author_paper_is_rejected(cache_dir, monkeypatch):
    monkeypatch.setattr(fetch.pdf_work, "extract", _extract_returning(
        _paper("COVID-19 and the Necrocapitalist Production of Death in Residential Care Homes",
               "Joe Greener")))
    token = fetch._ctx.set(fetch._FetchContext())
    try:
        fetch._current_ctx().zotero_untrusted = True
        fetch._set_expected({"title": GREENER + " in a Residential Care Home", "authors": ["Greener"]})
        result = await fetch._cache_pdf_and_return(
            Path("/library/storage/NFJC7R5K/x.pdf"), "10.1007/s10612-019-09467-3",
            "zotero_webdav_pdf", None, "sections", None, None, None)
    finally:
        fetch._ctx.reset(token)
    assert result is None


async def test_ssrn_title_search_remap_must_be_the_same_work(monkeypatch):
    monkeypatch.setattr(fetch, "_published_titles", AsyncMock(return_value=[
        "Measuring Compliance in the Age of Governance: How the Governance Turn Has Impacted "
        "Compliance Measurement by the State"]))
    assert not await fetch._same_work(
        "Cosmetic Compliance and the Failure of Negotiated Governance", "10.1017/9781108770941.003")
    monkeypatch.setattr(fetch, "_published_titles", AsyncMock(return_value=[
        "Cosmetic Compliance and the Failure of Negotiated Governance"]))
    assert await fetch._same_work(
        "Cosmetic compliance and the failure of negotiated governance", "10.1/x")
