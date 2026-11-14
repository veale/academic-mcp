"""fetch_document: download a file at a URL, convert it, navigate it."""

import io
import sys
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp.config import config  # noqa: E402
from academic_mcp.core import documents  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "pdf_cache_dir", tmp_path)
    monkeypatch.setattr(config, "use_stealth_browser", False)
    monkeypatch.setattr(config, "gost_proxy_url", "")
    monkeypatch.setattr(config, "institutional_proxy_auto", False)
    from academic_mcp import zotero_import

    def _no_import(*args, **kwargs):
        raise AssertionError("fetch_document must not queue a Zotero auto-import")

    monkeypatch.setattr(zotero_import, "enqueue_zotero_import", _no_import)


def _serve(monkeypatch, routes: dict[str, tuple[int, str, bytes]]):
    """Route documents' httpx clients to canned responses."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        status, ctype, body = routes.get(str(request.url), (404, "text/plain", b"missing"))
        return httpx.Response(status, headers={"content-type": ctype}, content=body)

    def client(**kwargs):
        kwargs.pop("proxy", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(documents, "_http_client", client)
    return seen


def _pdf_bytes(pages: list[tuple[str, str]]) -> bytes:
    import fitz

    doc = fitz.open()
    for heading, body in pages:
        page = doc.new_page()
        page.insert_text((72, 72), heading, fontsize=18)
        y = 110
        for line in body.split("\n"):
            page.insert_text((72, y), line, fontsize=10)
            y += 14
    data = doc.tobytes()
    doc.close()
    return data


def _zip(names: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in names.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Type sniffing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data,ctype,url,expected",
    [
        (b"%PDF-1.7\n...", "application/octet-stream", "https://x.org/file", "pdf"),
        (b"%PDF-1.4", "text/html", "https://x.org/report.aspx", "pdf"),
        (_zip({"word/document.xml": b"<w/>"}), "application/octet-stream", "https://x.org/a", "docx"),
        (_zip({"ppt/presentation.xml": b"<p/>"}), "", "https://x.org/a", "pptx"),
        (_zip({"xl/workbook.xml": b"<x/>"}), "", "https://x.org/a", "xlsx"),
        (b"\xd0\xcf\x11\xe0rest", "application/vnd.ms-excel", "https://x.org/a.xls", "xls"),
        (b"\xd0\xcf\x11\xe0rest", "application/msword", "https://x.org/a.doc", "ole"),
        (b"<!DOCTYPE html><html><body>hi</body></html>", "text/html", "https://x.org/", "html"),
        (b"a,b\n1,2\n", "text/csv", "https://x.org/data", "csv"),
        (b"\x00\x01binary", "application/octet-stream", "https://x.org/blob", None),
    ],
)
def test_sniff(tmp_path, data, ctype, url, expected):
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    assert documents._sniff(path, ctype, url) == expected


def test_document_links_resolve_relative_paths():
    html = '<a href="/files/report.pdf">r</a><a href="deck.PPTX?x=1">d</a><a href="page.html">p</a>'
    assert documents.document_links(html, "https://gov.example/consult/") == [
        "https://gov.example/files/report.pdf",
        "https://gov.example/consult/deck.PPTX",
    ]


def test_blocked_pages_are_recognised():
    assert documents._looks_blocked("<title>Just a moment...</title><div id='cf-challenge'>")
    assert not documents._looks_blocked("<html><body><h1>Annual report</h1></body></html>")


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

async def test_pdf_is_converted_cached_and_navigable(monkeypatch):
    url = "https://regulator.example/publications/annual-report"
    body = "\n".join(f"Paragraph {i} about enforcement priorities and fines." for i in range(20))
    pdf = _pdf_bytes([("Introduction", body), ("Enforcement", body), ("Outlook", body)])
    seen = _serve(monkeypatch, {url: (200, "application/pdf", pdf)})

    from academic_mcp.server import _handle_fetch_document

    first = await _handle_fetch_document({"url": url, "mode": "full"})
    text = first[0].text
    assert "Type: PDF" in text
    assert "enforcement priorities" in text
    assert "DOI:" not in text
    assert len(seen) == 1

    cached = documents.cached_document(url)
    assert cached is not None
    assert cached.source == "document_url:pdf"
    assert cached.metadata["identity"]["status"] == "not_checkable"
    assert Path(cached.pdf_path).suffix == ".pdf"

    # Second call is served from cache, no network.
    ranged = await _handle_fetch_document({"url": url, "mode": "range", "range_start": 0, "range_end": 200})
    assert "(cached)" in ranged[0].text
    assert len(seen) == 1

    sections = await _handle_fetch_document({"url": url, "mode": "sections"})
    assert f'fetch_document(url="{url}"' in sections[0].text
    assert f'search_in_article(ref="{documents.cache_key_for(url)}"' in sections[0].text


async def test_search_in_article_finds_fetched_document(monkeypatch):
    url = "https://example.org/whitepaper.pdf"
    pdf = _pdf_bytes([("Summary", "Interoperability obligations apply to gatekeepers.\n" * 30)])
    _serve(monkeypatch, {url: (200, "application/pdf", pdf)})
    from academic_mcp.server import _handle_fetch_document, _handle_search_in_article

    await _handle_fetch_document({"url": url})
    found = await _handle_search_in_article({"ref": url, "terms": ["interoperability"]})
    assert "Interoperability" in found[0].text or "interoperability" in found[0].text
    assert "not in cache" not in found[0].text


async def test_html_page_lists_document_links(monkeypatch):
    url = "https://gov.example/consultation"
    html = (
        "<html><head><title>Consultation</title></head><body><h1>Consultation on AI</h1>"
        + "<p>Background text about the consultation process. </p>" * 30
        + '<a href="/docs/response.pdf">Response</a></body></html>'
    ).encode()
    _serve(monkeypatch, {url: (200, "text/html; charset=utf-8", html)})
    from academic_mcp.server import _handle_fetch_document

    out = await _handle_fetch_document({"url": url})
    text = out[0].text
    assert "web page, not a document file" in text
    assert "https://gov.example/docs/response.pdf" in text


async def test_failure_reports_attempts(monkeypatch):
    url = "https://blocked.example/report.pdf"
    _serve(monkeypatch, {url: (403, "text/html", b"<html>Access denied</html>")})
    from academic_mcp.server import _handle_fetch_document

    out = await _handle_fetch_document({"url": url})
    assert out.isError
    assert "HTTP 403" in out.content[0].text


async def test_rejects_non_http_urls():
    result = await documents.fetch_document("file:///etc/passwd")
    assert result.error


async def test_zotero_is_untouched_unless_asked(monkeypatch):
    url = "https://example.org/brief.pdf"
    _serve(monkeypatch, {url: (200, "application/pdf", _pdf_bytes([("Brief", "Some text here.\n" * 20)]))})
    save = AsyncMock(return_value="Zotero: saved as document item ABCD2345 with the PDF attached.")
    monkeypatch.setattr(documents, "save_to_zotero", save)

    plain = await documents.fetch_document(url)
    assert not plain.zotero
    save.assert_not_awaited()

    saved = await documents.fetch_document(url, save=True)
    save.assert_awaited_once()
    assert "ABCD2345" in saved.zotero


async def test_save_to_zotero_skips_urls_already_in_library(monkeypatch):
    from academic_mcp import zotero_import, zotero_sqlite
    from academic_mcp.text_cache import CachedArticle

    existing = type("Item", (), {"key": "WXYZ7890", "title": "Existing report"})()
    monkeypatch.setattr(zotero_sqlite, "resolve_reference", AsyncMock(return_value=(existing, "url")))
    create = AsyncMock()
    monkeypatch.setattr(zotero_import, "_create_zotero_item", create)

    cached = CachedArticle(doi="url:0", text="t", source="document_url:pdf",
                           metadata={"url": "https://example.org/r.pdf", "document_kind": "pdf"})
    message = await documents.save_to_zotero(cached)
    assert "WXYZ7890" in message
    create.assert_not_awaited()


async def test_save_to_zotero_creates_document_item_and_attaches_pdf(monkeypatch, tmp_path):
    from academic_mcp import zotero, zotero_import, zotero_sqlite
    from academic_mcp.text_cache import CachedArticle

    monkeypatch.setattr(zotero_sqlite, "resolve_reference", AsyncMock(return_value=(None, "")))
    monkeypatch.setattr(zotero.zot_config, "local_enabled", True, raising=False)
    create = AsyncMock(return_value=("NEWK1234", "", 200))
    attach = AsyncMock(return_value=True)
    monkeypatch.setattr(zotero_import, "_create_zotero_item", create)
    monkeypatch.setattr(zotero_import, "_attach_pdf_to_item", attach)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    cached = CachedArticle(doi="url:0", text="t", source="document_url:pdf", pdf_path=str(pdf),
                           metadata={"url": "https://ico.example/guidance.pdf", "document_kind": "pdf",
                                     "title": "Guidance on AI"})
    message = await documents.save_to_zotero(cached)
    assert "NEWK1234" in message and "attached" in message
    payload = create.await_args.args[0]
    assert payload["itemType"] == "document"
    assert payload["url"] == "https://ico.example/guidance.pdf"
    assert payload["title"] == "Guidance on AI"
    assert {"tag": "fetched-document"} in payload["tags"]
    attach.assert_awaited_once()


# ---------------------------------------------------------------------------
# markitdown conversions (need the 'documents' extra)
# ---------------------------------------------------------------------------

async def test_xlsx_sheets_become_sections(monkeypatch):
    pytest.importorskip("markitdown")
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.title = "Budget"
    workbook.active.append(["Item", "Cost"])
    workbook.active.append(["Servers", 1200])
    other = workbook.create_sheet("Staff")
    other.append(["Role", "FTE"])
    other.append(["Engineer", 3])
    buffer = io.BytesIO()
    workbook.save(buffer)

    url = "https://data.example/budget.xlsx"
    _serve(monkeypatch, {url: (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               buffer.getvalue())})
    result = await documents.fetch_document(url, mode="sections")
    titles = [s.title for s in result.article.available_sections]
    assert "Budget" in titles and "Staff" in titles
    assert "Servers" in result.cached.text


async def test_pptx_slides_become_sections(monkeypatch):
    pytest.importorskip("markitdown")
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    for title in ("Opening", "Findings", "Next steps"):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = f"Details for {title.lower()}"
    buffer = io.BytesIO()
    deck.save(buffer)

    url = "https://conf.example/talk.pptx"
    _serve(monkeypatch, {url: (200, "application/octet-stream", buffer.getvalue())})
    result = await documents.fetch_document(url, mode="section", section="Findings")
    assert result.cached.section_detection == "slides"
    assert [s["title"] for s in result.cached.sections][1].startswith("Slide 2")
    assert "Details for findings" in result.article.text


async def test_long_single_section_documents_get_keyword_chunks(monkeypatch):
    url = "https://data.example/big.csv"
    rows = "\n".join(f"{i},region {i % 7},value {i * 3}" for i in range(4000))
    _serve(monkeypatch, {url: (200, "text/csv", ("id,region,value\n" + rows).encode())})
    pytest.importorskip("markitdown")
    result = await documents.fetch_document(url, mode="sections")
    assert len(result.cached.text) > documents._AUTO_FULL_CHARS
    assert len(result.article.available_sections) > 3
    assert all(s.is_infill for s in result.article.available_sections)
