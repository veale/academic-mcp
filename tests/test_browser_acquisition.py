"""Durability, ownership, authentication and content integrity of browser handoff."""
import asyncio
import json

import httpx
import pytest
from starlette.applications import Starlette

from academic_mcp import browser_acquisition as acquisition, content_extractor, text_cache, zotero_import


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_ACQUISITION_DIR", str(tmp_path / "browser"))
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "test-worker-secret")
    monkeypatch.setattr(acquisition.config, "pdf_cache_dir", tmp_path / "pdfs")
    monkeypatch.setattr(acquisition.config, "auto_import_to_zotero", False)
    acquisition.config.pdf_cache_dir.mkdir()


async def job():
    return await acquisition.request_acquisition("10.1234/test", "https://publisher.example/article")


async def test_parallel_requests_coalesce_and_reopen_durably():
    jobs = await asyncio.gather(*(job() for _ in range(6)))
    assert len({j["id"] for j in jobs}) == 1
    reread = await acquisition.status(jobs[0]["id"])
    assert reread["state"] == "queued"
    assert "lease" not in reread


async def test_pause_blocks_other_work_and_local_resume_retains_lease():
    first = await job()
    await acquisition.request_acquisition("10.1234/second")
    claimed = await acquisition.claim("imac")
    assert claimed["id"] == first["id"]
    await acquisition.report(first["id"], claimed["lease"], "needs_login", "Complete sign-in")
    assert await acquisition.claim("other") is None
    recovered = await acquisition.claim("imac")
    assert recovered["state"] == "needs_login"
    assert recovered["lease"] == claimed["lease"]
    await acquisition.report(first["id"], claimed["lease"], "claimed")
    assert (await acquisition.status(first["id"]))["state"] == "claimed"


async def test_cancel_invalidates_upload_and_report():
    j = await job()
    c = await acquisition.claim("imac")
    await acquisition.control(j["id"], "cancel")
    with pytest.raises(ValueError, match="lease"):
        await acquisition.report(j["id"], c["lease"], "complete")
    assert (await acquisition.status(j["id"]))["state"] == "cancelled"


async def test_expired_lease_is_reassigned_and_old_worker_rejected():
    j = await job()
    c = await acquisition.claim("imac")
    conn = await acquisition.db()
    await conn.execute("UPDATE jobs SET expires=0 WHERE id=?", (j["id"],))
    await conn.commit()
    await conn.close()
    replacement = await acquisition.claim("replacement")
    assert replacement["lease"] != c["lease"]
    with pytest.raises(ValueError, match="lease"):
        await acquisition.report(j["id"], c["lease"], "needs_login")


async def test_cannot_report_complete_without_content():
    j = await job()
    c = await acquisition.claim("imac")
    with pytest.raises(ValueError, match="before content"):
        await acquisition.report(j["id"], c["lease"], "complete")


async def test_mismatched_article_and_fake_pdf_are_rejected():
    j = await job()
    c = await acquisition.claim("imac")
    with pytest.raises(ValueError, match="different article"):
        await acquisition.ingest(j["id"], c["lease"], "html", b"<p>Wrong article</p>", {"doi": "10.1234/wrong"})
    with pytest.raises(ValueError, match="not a PDF"):
        await acquisition.ingest(j["id"], c["lease"], "pdf", b"<html>Login</html>", {})


async def test_html_available_before_pdf_and_import_is_durable(monkeypatch):
    from unittest.mock import AsyncMock
    j = await job()
    c = await acquisition.claim("imac")
    monkeypatch.setattr(content_extractor, "extract_article_with_sections", AsyncMock(return_value={
        "text": "Full article body", "sections": [{"title": "Body", "start": 0, "end": 17}],
        "section_detection": "html_headings"}))
    monkeypatch.setattr(acquisition.config, "auto_import_to_zotero", True)
    persist = AsyncMock()
    monkeypatch.setattr(zotero_import, "_enqueue_job", persist)
    monkeypatch.setattr(zotero_import, "ensure_auto_import_initialized", AsyncMock())
    monkeypatch.setattr(zotero_import, "_ensure_worker_running", lambda: None)
    result = await acquisition.ingest(j["id"], c["lease"], "html", b"<article>Body</article>", {"doi": j["doi"]})
    assert result["content_ready"]
    assert result["html_received"]
    assert not result.get("pdf_received")
    assert text_cache.get_cached(j["doi"]).text == "Full article body"
    persist.assert_awaited_once()
    assert persist.call_args.args[0].pdf_path is None


async def test_worker_http_requires_separate_token_and_lease():
    app = Starlette(routes=acquisition.routes())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        response = await client.post("/acquisition/claim", json={"worker": "imac"})
        assert response.status_code == 401
        await job()
        response = await client.post("/acquisition/claim", json={"worker": "imac"},
                                     headers={"Authorization": "Bearer test-worker-secret"})
        assert response.status_code == 200
        lease = response.json()["lease"]
        assert lease
        response = await client.post("/acquisition/upload?id=" + response.json()["id"] + "&kind=pdf",
                                     content=b"%PDF-1.4", headers={"Authorization": "Bearer test-worker-secret"})
        assert response.status_code == 409


async def test_real_pdf_is_parsed_and_retained_without_a_library_write(monkeypatch):
    import fitz
    j = await job()
    c = await acquisition.claim("imac")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 50), "\n".join(["A complete sentence about research methods and useful results."] * 12))
    data = document.tobytes()
    document.close()
    monkeypatch.setattr(acquisition.config, "use_pymupdf4llm", False)
    result = await acquisition.ingest(j["id"], c["lease"], "pdf", data, {"doi": j["doi"]})
    assert result["pdf_received"] and result["content_ready"]
    cached = text_cache.get_cached(j["doi"])
    assert cached.word_count > 30
    assert cached.pdf_path
    assert result["zotero"].startswith("not queued")


async def test_failed_normal_fetch_gets_durable_desktop_handoff(monkeypatch):
    from unittest.mock import AsyncMock
    from academic_mcp.core import fetch
    from academic_mcp.core.types import ArticleId, FetchedArticle

    monkeypatch.setattr(fetch, "_fetch_article", AsyncMock(return_value=FetchedArticle(
        doi="10.1234/test", error="Full text unavailable", source="")))
    result = await fetch.fetch_article(ArticleId(doi="10.1234/test"))
    assert result.browser_acquisition["state"] == "queued"
    assert "browser_acquisition" in result.failure_hints[0]
    assert (await acquisition.status(result.browser_acquisition["id"]))["state"] == "queued"


async def test_remote_resume_and_retry_are_bounded():
    j = await job()
    c = await acquisition.claim("imac")
    await acquisition.report(j["id"], c["lease"], "needs_review")
    await acquisition.control(j["id"], "resume")
    assert (await acquisition.status(j["id"]))["state"] == "queued"
    for _ in range(4):
        c = await acquisition.claim("imac")
        state = await acquisition.report(j["id"], c["lease"], "retry", "temporary network failure")
        conn = await acquisition.db()
        await conn.execute("UPDATE jobs SET retry_at=0 WHERE id=?", (j["id"],))
        await conn.commit()
        await conn.close()
        if state["state"] == "failed":
            break
    assert state["state"] == "failed"
