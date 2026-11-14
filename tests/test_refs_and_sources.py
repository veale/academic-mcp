"""One-parameter references, duplicate library items, and the remaining
source gaps from feedback fb_e6d992ff1a36 (ref=, title lookup, duplicates),
fb_681eeb810fdc (ACM DL HTML) and fb_501920afa08f (repository handles)."""

import sqlite3

import pytest

from academic_mcp import content_extractor, pdf_fetcher, pdf_work, zotero_sqlite
from academic_mcp.core import fetch, refs
from academic_mcp.core.types import ArticleId
from academic_mcp.identity import _fold

# conftest swaps pdf_work.extract for an inline version; the OCR step lives
# in the real one.
_REAL_EXTRACT = pdf_work.extract

SANDHOLTZ = "Making Standards Stick: A Theory of Coupled vs. Decoupled Compliance"
RICE = "How Effective Is Internal Control Reporting under SOX 404? Determinants of the (Non-)Disclosure of Existing Material Weaknesses"


@pytest.mark.parametrize("ref,expected", [
    ("10.1111/j.1475-679X.2011.00434.x", ("doi", "10.1111/j.1475-679X.2011.00434.x")),
    ("https://doi.org/10.1017/S0143814X06000547", ("doi", "10.1017/S0143814X06000547")),
    ("doi:10.1073/pnas.1708290115", ("doi", "10.1073/pnas.1708290115")),
    ("arXiv:2501.01234v2", ("doi", "10.48550/arxiv.2501.01234")),
    ("2501.01234", ("doi", "10.48550/arxiv.2501.01234")),
    ("SSRN 3867634", ("doi", "10.2139/ssrn.3867634")),
    ("ssrn:448221", ("doi", "10.2139/ssrn.448221")),
    ("ABCD2345", ("zotero", "ABCD2345")),
    ("zotero://select/library/items/ABCD2345", ("zotero", "zotero://select/library/items/ABCD2345")),
    ("selbstInstitutionalViewAlgorithmic2021a", ("zotero", "selbstInstitutionalViewAlgorithmic2021a")),
    ("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3867634",
     ("url", "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3867634")),
    ("Making Standards Stick Sandholtz 2012", ("title", "Making Standards Stick Sandholtz 2012")),
])
def test_classify(ref, expected):
    assert refs.classify(ref) == expected


@pytest.mark.parametrize("ref", [
    SANDHOLTZ,
    "Making Standards Stick: A Theory of Coupled vs. Decoupled Compliance (Sandholtz 2012)",
    "Sandholtz, Making Standards Stick",
    "making standards stick a theory of coupled vs decoupled compliance",
])
def test_title_matches(ref):
    assert refs.match_title(_fold(ref), _fold(SANDHOLTZ), ["sandholtz"], "2012",
                            refs.main_title(SANDHOLTZ)) is not None


SLAVE = "Slave to the Algorithm? Why a 'Right to an Explanation' Is Probably Not the Remedy You Are Looking For"


def test_short_main_title_needs_an_author():
    main = refs.main_title(SLAVE)
    assert refs.match_title(_fold("Edwards and Veale, Slave to the Algorithm"), _fold(SLAVE),
                            ["edwards", "veale"], "2017", main) is not None
    assert refs.match_title(_fold("Slave to the Algorithm"), _fold(SLAVE), ["edwards", "veale"], "2017", main) is None


@pytest.mark.parametrize("ref", [
    "Making Standards Stick Krawiec",          # extra words are not an author
    "Standards",                               # too little to identify a work
    "Decoupled compliance in organisations",   # shares words, not the title
])
def test_title_non_matches(ref):
    assert refs.match_title(_fold(ref), _fold(SANDHOLTZ), ["sandholtz"], "2012",
                            refs.main_title(SANDHOLTZ)) is None


def test_wrong_year_scores_lower():
    right = refs.match_title(_fold(SANDHOLTZ + " 2012"), _fold(SANDHOLTZ), ["sandholtz"], "2012")
    wrong = refs.match_title(_fold(SANDHOLTZ + " 1999"), _fold(SANDHOLTZ), ["sandholtz"], "2012")
    assert right > wrong


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CATALOG_ENABLED", "false")
    path = tmp_path / "zotero.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(f"""
            CREATE TABLE libraries(libraryID INTEGER, type TEXT);INSERT INTO libraries VALUES(1,'user');
            CREATE TABLE groups(groupID INTEGER, libraryID INTEGER, name TEXT);
            CREATE TABLE fields(fieldID INTEGER,fieldName TEXT);
            INSERT INTO fields VALUES(1,'title'),(2,'url'),(3,'citationKey'),(4,'DOI'),(5,'extra'),(6,'date');
            CREATE TABLE itemTypes(itemTypeID INTEGER,typeName TEXT);
            INSERT INTO itemTypes VALUES(1,'journalArticle'),(2,'attachment'),(3,'preprint');
            CREATE TABLE items(itemID INTEGER, key TEXT, dateAdded TEXT, dateModified TEXT,
                               itemTypeID INTEGER, libraryID INTEGER);
            INSERT INTO items VALUES
              (1,'RICEAUTO','2026-11-08 17:52:15','2026-11-08',1,1),
              (2,'RICEUSER','2026-11-08 18:01:06','2026-11-08',1,1),
              (3,'SANDHOLT','2026-01-01','2026-01-01',1,1),
              (4,'COGLIAN1','2026-01-01','2026-01-01',1,1),
              (5,'COGLIAN2','2026-01-02','2026-01-02',1,1),
              (6,'POWERPRE','2025-01-01','2025-01-01',3,1),
              (7,'POWERPUB','2025-06-01','2025-06-01',1,1),
              (13,'ATTPOW01','2025-01-01','2025-01-01',2,1),
              (14,'ATTPOW02','2025-06-01','2025-06-01',2,1),
              (10,'ATTRICE1','2026-11-08','2026-11-08',2,1),
              (11,'ATTRICE2','2026-11-08','2026-11-08',2,1),
              (12,'ATTSAND1','2026-01-01','2026-01-01',2,1);
            CREATE TABLE deletedItems(itemID INTEGER);
            INSERT INTO deletedItems VALUES(10);
            CREATE TABLE itemAttachments(itemID INTEGER, parentItemID INTEGER, contentType TEXT, path TEXT);
            INSERT INTO itemAttachments VALUES
              (10,1,'application/pdf','storage:wrong.pdf'),
              (11,2,'application/pdf','storage:Rice 2012.pdf'),
              (12,3,'application/pdf','storage:Sandholtz 2012.pdf'),
              (13,6,'application/pdf','storage:arxiv.pdf'),(14,7,'application/pdf','storage:facct.pdf');
            CREATE TABLE itemData(itemID INTEGER,fieldID INTEGER,valueID INTEGER);
            INSERT INTO itemData VALUES(1,1,1),(1,4,2),(1,6,3),(2,1,1),(2,4,4),(2,6,3),
                                       (3,1,5),(3,6,6),(4,1,7),(4,6,8),(5,1,9),(5,6,8),
                                       (6,1,10),(6,4,11),(7,1,10),(7,4,12);
            CREATE TABLE itemDataValues(valueID INTEGER,value TEXT);
            INSERT INTO itemDataValues VALUES
              (1,'{RICE}'),(2,'10.1111/j.1475-679x.2011.00434.x'),(3,'2012'),
              (4,'10.1111/j.1475-679X.2011.00434.x'),(5,'{SANDHOLTZ}'),(6,'2012'),
              (7,'Compliance Management Systems: Do They Make a Difference?'),(8,'2021'),
              (9,'Compliance Management Systems: Do They Make a Difference in Practice?'),
              (10,'Power Hungry Processing: Watts Driving the Cost of AI Deployment?'),
              (11,'10.48550/arXiv.2311.16863'),(12,'10.1145/3630106.3658542');
            CREATE TABLE creators(creatorID INTEGER,firstName TEXT,lastName TEXT);
            INSERT INTO creators VALUES(1,'Sarah','Rice'),(2,'Kristin','Sandholtz'),(3,'Cary','Coglianese'),(4,'Sasha','Luccioni');
            CREATE TABLE creatorTypes(creatorTypeID INTEGER, creatorType TEXT);
            INSERT INTO creatorTypes VALUES(1,'author');
            CREATE TABLE itemCreators(itemID INTEGER,creatorID INTEGER,creatorTypeID INTEGER,orderIndex INTEGER);
            INSERT INTO itemCreators VALUES(1,1,1,0),(2,1,1,0),(3,2,1,0),(4,3,1,0),(5,3,1,0),(6,4,1,0),(7,4,1,0);
            CREATE TABLE itemTags(itemID INTEGER,tagID INTEGER);CREATE TABLE tags(tagID INTEGER,name TEXT);
            INSERT INTO tags VALUES(1,'auto-imported');INSERT INTO itemTags VALUES(1,1);
        """)
    monkeypatch.setattr(zotero_sqlite.sqlite_config, "db_path", str(path))
    monkeypatch.setattr(zotero_sqlite, "_maybe_schedule_shadow_refresh", lambda *a: None)
    monkeypatch.setattr(zotero_sqlite, "_title_rows", (0.0, []))
    return path


async def test_doi_duplicates_prefer_the_copy_with_a_live_pdf(library):
    # The auto-imported copy's only PDF is in the trash; the user's copy has one.
    item = await zotero_sqlite.search_by_doi("10.1111/j.1475-679X.2011.00434.x")
    assert item.key == "RICEUSER"


async def test_title_with_author_resolves_to_library_item(library):
    resolved = await refs.resolve("Making Standards Stick Sandholtz 2012")
    assert resolved.zotero_key == "SANDHOLT" and resolved.via == "title" and not resolved.error


async def test_duplicates_of_one_work_pick_the_users_copy_and_list_all(library):
    resolved = await refs.resolve(RICE)
    assert resolved.zotero_key == "RICEUSER"
    assert {c["zotero_key"] for c in resolved.candidates} == {"RICEUSER", "RICEAUTO"}
    auto = next(c for c in resolved.candidates if c["zotero_key"] == "RICEAUTO")
    assert auto["auto_imported"] and not auto["has_pdf"]


async def test_preprint_and_published_version_resolve_to_the_published_copy(library):
    resolved = await refs.resolve("Power Hungry Processing Luccioni")
    assert resolved.zotero_key == "POWERPUB" and not resolved.error
    assert {c["zotero_key"] for c in resolved.candidates} == {"POWERPUB", "POWERPRE"}


async def test_different_works_with_similar_titles_are_listed_not_guessed(library):
    resolved = await refs.resolve("Compliance Management Systems Coglianese")
    assert resolved.error.startswith("ambiguous_reference")
    assert {c["zotero_key"] for c in resolved.candidates} == {"COGLIAN1", "COGLIAN2"}


async def test_fetch_reports_ambiguity_with_keys(library):
    result = await fetch.fetch_article(ArticleId(ref="Compliance Management Systems Coglianese"))
    assert result.error.startswith("ambiguous_reference")
    assert "zotero_key=COGLIAN1" in result.text and "zotero_key=COGLIAN2" in result.text


async def test_unknown_title_falls_back_to_web_then_reports(library, monkeypatch):
    async def no_web(text):
        return None
    monkeypatch.setattr(refs, "_resolve_title_web", no_web)
    resolved = await refs.resolve("An entirely unknown paper about nothing at all")
    assert resolved.error.startswith("unknown_reference")


async def test_batch_targets_use_the_cache_key_the_fetch_writes(library):
    assert await refs.cache_target("10.1111/J.1475-679X.2011.00434.X") == (
        "10.1111/j.1475-679x.2011.00434.x", {"doi": "10.1111/j.1475-679x.2011.00434.x"}, "")
    key, args, error = await refs.cache_target("Making Standards Stick Sandholtz")
    assert (key, args, error) == ("zotero:SANDHOLT", {"zotero_key": "SANDHOLT", "doi": None}, "")
    key, args, error = await refs.cache_target("nobodyNothing2020")
    assert error.startswith("unknown_reference")


def test_publisher_selectors_work_without_cssselect():
    html = (
        "<html><body><nav>Menu Login Cart</nav>"
        "<section id='summary-abstract'><h2>Abstract</h2><p>Short abstract.</p></section>"
        "<div class='recommended'>Other papers</div>"
        "<section id='bodymatter'><section><h2>1 Introduction</h2><p>Body text.</p></section></section>"
        "</body></html>"
    )
    filtered = content_extractor._pre_filter_html(html, "https://dl.acm.org/doi/10.1145/1")
    assert "Short abstract" in filtered and "Body text" in filtered
    assert "Other papers" not in filtered and "Menu Login" not in filtered
    assert content_extractor._selector_xpath("div.article__body") == (
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' article__body ')]"
    )


def test_acm_sections_survive_extraction():
    def para(n):
        return "<p>" + " ".join(
            f"Finding {n}.{k}: retrieval step {k} grounded answer {n * 31 + k} in knowledge base chunk {k * 7}."
            for k in range(30)
        ) + "</p>"
    body = "".join(
        f"<section id='sec-{i}'><h2>{i} Heading {i}</h2>{para(i)}</section>" for i in range(1, 7)
    )
    html = (
        "<html><head><title>T</title></head><body><nav>ACM is now Open Access</nav>"
        f"<section id='summary-abstract'><h2>Abstract</h2>{para(0)}</section>"
        f"<section id='bodymatter'>{body}</section></body></html>"
    )
    result = content_extractor._extract_with_sections_sync(html, "https://dl.acm.org/doi/10.1145/3704137.3704181")
    assert result is not None and result["word_count"] > 1500
    titles = [s["title"] for s in result["sections"]]
    assert "Abstract" in titles and "1 Heading 1" in titles and "6 Heading 6" in titles


def test_dspace7_item_page_yields_working_bitstream_urls():
    html = (
        '<html><head><base href="/renate/">'
        '<meta name="citation_pdf_url" content="https://oa.tib.eu/bitstreams/0a89c465-dd15-4c4c-b190-71bbfc744c96/download">'
        '</head><body><a href="/renate/bitstreams/e36995aa-0c94-4549-9ea6-5802a43c77ff/download">licence</a>'
        '</body></html>'
    )
    urls = pdf_fetcher.repository_pdf_urls(
        html, "https://oa.tib.eu/renate/items/ffd87fdf-32d1-4c35-b332-848c516917a2",
        "https://oa.tib.eu/bitstreams/0a89c465-dd15-4c4c-b190-71bbfc744c96/download",
    )
    assert urls[0] == "https://oa.tib.eu/renate/bitstreams/0a89c465-dd15-4c4c-b190-71bbfc744c96/download"
    assert "https://oa.tib.eu/renate/server/api/core/bitstreams/0a89c465-dd15-4c4c-b190-71bbfc744c96/content" in urls


def test_dspace6_and_eprints_file_links():
    html = (
        '<a href="/bitstream/handle/10044/1/123/paper.pdf?sequence=1">PDF</a>'
        '<a href="https://eprints.example.ac.uk/4567/1/thesis.pdf">thesis</a>'
    )
    urls = pdf_fetcher.repository_pdf_urls(html, "https://spiral.example.ac.uk/handle/10044/1/123")
    assert "https://spiral.example.ac.uk/bitstream/handle/10044/1/123/paper.pdf?sequence=1" in urls
    assert "https://eprints.example.ac.uk/4567/1/thesis.pdf" in urls


def test_ocr_is_needed_for_scans_and_scrambled_text_only():
    assert pdf_work.needs_ocr({"text": "", "pages": 9})
    assert pdf_work.needs_ocr({"text": "a few words " * 3, "metadata": {"pages": 33}})
    scrambled = " ".join("Xli tvsgiww sj gsqtpmergi mw wxyhmih" .split() * 60)
    assert pdf_work.needs_ocr({"text": scrambled, "pages": 2})
    normal = " ".join(["Compliance in organisations is studied here at length and in depth."] * 200)
    assert not pdf_work.needs_ocr({"text": normal, "pages": 10})


async def test_ocr_replaces_an_empty_text_layer(monkeypatch, tmp_path):
    calls = []

    async def fake_run(source, markdown, pages, task, timeout):
        calls.append(task)
        if task == "ocr":
            return {"text": "Recognised words " * 500, "sections": [], "pages": 3, "ocr": True, "ocr_pages": 3}
        return {"text": "", "sections": [], "pages": 3, "_charmap": b"x"}

    monkeypatch.setattr(pdf_work, "_run", fake_run)
    monkeypatch.setattr("academic_mcp.pdf_extractor.ocr_available", lambda: True)
    result = await _REAL_EXTRACT(tmp_path / "scan.pdf", True)
    assert calls == [None, "ocr"] and result["ocr"] and "_charmap" not in result


async def test_ocr_is_skipped_when_disabled(monkeypatch, tmp_path):
    async def fake_run(source, markdown, pages, task, timeout):
        assert task is None
        return {"text": "", "sections": [], "pages": 3}

    monkeypatch.setattr(pdf_work, "_run", fake_run)
    monkeypatch.setenv("OCR_ENABLED", "false")
    result = await _REAL_EXTRACT(tmp_path / "scan.pdf", True)
    assert not result.get("ocr")


def test_dublin_core_doi_counts_as_the_page_doi():
    html = '<meta name="dc.Identifier" scheme="doi" content="10.1145/3704137.3704181">'
    meta = content_extractor.extract_citation_meta(html, "https://dl.acm.org/doi/10.1145/3704137.3704181")
    assert meta["citation_doi"] == "10.1145/3704137.3704181"


async def test_sparse_cached_pdf_is_re_extracted_once_with_ocr(tmp_path, monkeypatch):
    from academic_mcp import text_cache
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 scan")
    old = text_cache.CachedArticle(
        doi="10.1/scan", text="from the SAGE Social Science Collections.", source="zotero_webdav_pdf",
        metadata={"zotero_key": "FDSJE5HG", "content_warnings": ["cover_sheet_only: 9 words"]},
        pdf_path=str(pdf),
    )
    monkeypatch.setattr("academic_mcp.pdf_extractor.ocr_available", lambda: True)
    assert fetch._needs_ocr_retry(old)
    old.metadata["ocr_checked"] = True
    assert not fetch._needs_ocr_retry(old)
