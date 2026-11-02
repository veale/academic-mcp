from unittest.mock import AsyncMock

from academic_mcp import zotero_fulltext as fulltext, text_cache


async def test_new_webdav_pdf_is_extracted_and_cached_for_indexing(
    tmp_path, monkeypatch
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(text_cache.config, "pdf_cache_dir", tmp_path)
    monkeypatch.setattr(
        fulltext.zotero.zot_config, "webdav_url", "https://dav.example/zotero/"
    )
    monkeypatch.setattr(
        fulltext.zotero, "get_pdf_from_webdav", AsyncMock(return_value=pdf)
    )
    monkeypatch.setattr(
        fulltext.pdf_work,
        "extract",
        AsyncMock(
            return_value={
                "text": "Newly synced full text " * 100,
                "sections": [],
                "section_detection": "unknown",
            }
        ),
    )
    item = {
        "item_key": "NEWKEY",
        "title": "New paper",
        "doi": "10.1234/new",
        "attachment_key": "PDFKEY",
        "_catalog": True,
        "library_prefix": "/users/1",
    }
    words = await fulltext._extract(item)
    cached = text_cache.get_cached("10.1234/new")
    assert words > 100
    assert cached.source == "zotero_webdav_pdf"
    assert cached.metadata["zotero_attachment_key"] == "PDFKEY"
    assert not fulltext._needs_text(item)


async def test_group_attachment_uses_its_group_api_endpoint(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(text_cache.config, "pdf_cache_dir", tmp_path)
    get = AsyncMock(return_value=pdf)
    monkeypatch.setattr(fulltext.zotero, "get_pdf_from_web_api", get)
    monkeypatch.setattr(
        fulltext.pdf_work,
        "extract",
        AsyncMock(return_value={"text": "Group paper text", "sections": []}),
    )
    item = {
        "item_key": "GROUP",
        "title": "Group paper",
        "attachment_key": "PDFKEY",
        "library_prefix": "/groups/123",
    }
    await fulltext._extract(item)
    get.assert_awaited_once_with("PDFKEY", "/groups/123")
    assert text_cache.get_cached("zotero:GROUP") is not None
