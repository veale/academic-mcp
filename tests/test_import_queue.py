"""Tests for the persistent Zotero auto-import queue."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import zotero_import


@pytest.fixture
def tmp_queue_db(tmp_path, monkeypatch):
    """Redirect the queue SQLite DB into a tmp path for isolation."""
    db_path = tmp_path / "import_queue.sqlite"
    monkeypatch.setattr(zotero_import, "_QUEUE_DB_PATH", db_path)
    yield db_path


def _make_job(doi: str, pdf_path: Path):
    from academic_mcp.text_cache import CachedArticle

    cached = CachedArticle(
        doi=doi,
        text="",
        source="web_http",
        sections=[],
        section_detection="unknown",
        word_count=0,
        metadata={"title": f"Paper {doi}"},
    )
    return zotero_import._ImportJob(doi=doi, pdf_path=pdf_path, cached_article=cached)


async def test_enqueue_then_get_due_returns_job(tmp_queue_db, tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    job = _make_job("10.1/abc", pdf)
    await zotero_import._enqueue_job(job)

    # run_after is set _IMPORT_DELAY_SECONDS in the future, so immediate
    # poll should find nothing due.
    due = await zotero_import._get_due_job()
    assert due is None

    # Force the row to be due by rewinding run_after.
    import aiosqlite

    async with aiosqlite.connect(tmp_queue_db) as db:
        await db.execute(
            "UPDATE import_queue SET run_after = '1970-01-01T00:00:00+00:00' WHERE doi = ?",
            (job.doi,),
        )
        await db.commit()

    due = await zotero_import._get_due_job()
    assert due is not None
    assert due["doi"] == "10.1/abc"
    assert due["attempts"] == 0


async def test_reschedule_applies_exponential_backoff(tmp_queue_db, tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    await zotero_import._enqueue_job(_make_job("10.1/b", pdf))

    # Reschedule three times with growing attempt counts and verify monotonic backoff.
    from datetime import datetime

    import aiosqlite

    prev_run_after: datetime | None = None
    for attempts in (1, 2, 3):
        await zotero_import._reschedule_job("10.1/b", attempts, "probe error")
        async with aiosqlite.connect(tmp_queue_db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT attempts, last_error, run_after FROM import_queue WHERE doi = ?",
                ("10.1/b",),
            )
            row = await cur.fetchone()
        assert row["attempts"] == attempts
        assert row["last_error"] == "probe error"
        run_after = datetime.fromisoformat(row["run_after"])
        if prev_run_after is not None:
            assert run_after >= prev_run_after
        prev_run_after = run_after


async def test_delete_job_removes_row(tmp_queue_db, tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    await zotero_import._enqueue_job(_make_job("10.1/c", pdf))
    await zotero_import._delete_job("10.1/c")
    assert await zotero_import._queue_count() == 0


def test_friendly_import_error_maps_403_to_config_hint():
    msg = zotero_import._friendly_import_error("403 write access denied")
    assert "403" in msg
    assert "allowWriteAccess" in msg


def test_friendly_import_error_maps_connect_to_reachability_hint():
    msg = zotero_import._friendly_import_error("Connect timeout at localhost")
    assert "not reachable" in msg or "desktop" in msg.lower()


def test_friendly_web_api_error_does_not_suggest_local_setting():
    msg = zotero_import._friendly_import_error("web: Zotero Web API 403 write denied")
    assert "Web API key" in msg
    assert "allowWriteAccess" not in msg


def test_auto_import_hint_denied_is_surfaced(monkeypatch):
    # Simulate probe result: state == denied.
    monkeypatch.setattr(zotero_import.config, "auto_import_to_zotero", True)
    zotero_import._write_probe_result.update({"state": "denied", "message": "403 denied"})
    hint = zotero_import.get_auto_import_hint("10.1/x")
    assert hint is not None
    assert "denied" in hint.lower() or "blocked" in hint.lower()


def test_auto_import_hint_unknown_state_is_silent(monkeypatch):
    monkeypatch.setattr(zotero_import.config, "auto_import_to_zotero", True)
    zotero_import._write_probe_result.update({"state": "unknown", "message": "ok-ish"})
    zotero_import._latest_attempt_by_doi.clear()
    assert zotero_import.get_auto_import_hint("10.1/x") is None


def test_synthetic_url_identifier_is_not_enqueued(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    job = _make_job("url:0da8b2f2802e1231", pdf)
    monkeypatch.setattr(zotero_import.config, "auto_import_to_zotero", True)
    called = False

    def fake_worker():
        nonlocal called
        called = True

    monkeypatch.setattr(zotero_import, "_ensure_worker_running", fake_worker)
    zotero_import.enqueue_zotero_import(job.doi, pdf, job.cached_article)
    assert called is False


async def test_worker_rechecks_when_write_access_is_denied(monkeypatch, tmp_queue_db):
    monkeypatch.setitem(zotero_import._write_probe_result, "state", "denied")
    monkeypatch.setitem(zotero_import._write_probe_result, "checked_at", zotero_import._iso_utc())
    monkeypatch.setattr(zotero_import, "_worker_task", None)
    monkeypatch.setattr(zotero_import, "_worker_wakeup", None)
    zotero_import._ensure_worker_running()
    task = zotero_import._worker_task
    assert task is not None
    task.cancel()
    import asyncio
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_web_api_item_creation_accepts_success_key(monkeypatch):
    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"success": {"0": "ABCD2345"}}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(zotero_import.zot_config, "api_key", "key")
    monkeypatch.setattr(zotero_import.zot_config, "user_id", "123")
    monkeypatch.setattr(zotero_import.httpx, "AsyncClient", Client)
    key, error, status = await zotero_import._create_zotero_item_web({"itemType": "document"})
    assert (key, error, status) == ("ABCD2345", "", 200)


async def test_probe_uses_web_api_when_local_api_is_disabled(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"access": {"user": {"library": True, "files": True}}}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(zotero_import.zot_config, "local_enabled", False)
    monkeypatch.setattr(zotero_import.zot_config, "api_key", "key")
    monkeypatch.setattr(zotero_import.zot_config, "user_id", "123")
    monkeypatch.setattr(zotero_import.zot_config, "library_type", "user")
    monkeypatch.setattr(zotero_import.httpx, "AsyncClient", Client)

    await zotero_import._probe_local_write_access()

    assert zotero_import._write_probe_result["state"] == "ready"
    assert zotero_import._local_api_reachable is False


async def test_web_probe_reports_read_only_key(monkeypatch):
    class Response:
        status_code = 403

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(zotero_import.zot_config, "api_key", "read-only-key")
    monkeypatch.setattr(zotero_import.zot_config, "user_id", "123")
    monkeypatch.setattr(zotero_import.httpx, "AsyncClient", Client)

    await zotero_import._probe_web_write_access()

    assert zotero_import._write_probe_result["state"] == "denied"
    assert "lacks write access" in zotero_import._write_probe_result["message"]


async def test_queued_pdf_survives_download_eviction(tmp_queue_db, tmp_path):
    pdf = tmp_path / "download.pdf"
    pdf.write_bytes(b"%PDF-1.4\nretained bytes")
    await zotero_import._enqueue_job(_make_job("10.1234/retained", pdf))
    pdf.unlink()
    import aiosqlite
    async with aiosqlite.connect(tmp_queue_db) as db:
        cur = await db.execute("SELECT pdf_path FROM import_queue")
        retained = Path((await cur.fetchone())[0])
    assert retained.read_bytes() == b"%PDF-1.4\nretained bytes"
    await zotero_import._delete_job("10.1234/retained")
    assert not retained.exists()


async def test_upload_stub_is_not_a_successful_attachment(monkeypatch):
    from unittest.mock import AsyncMock
    import httpx
    monkeypatch.setattr(zotero_import, "_local_api_reachable", False)
    child = {"key": "STUB1234", "data": {"contentType": "application/pdf"}}
    get = AsyncMock(return_value=httpx.Response(200, json=[child]))
    monkeypatch.setattr(zotero_import.zotero, "_web_api_get", get)
    assert not await zotero_import._item_has_pdf_attachment("PARENT12")
    child["data"]["md5"] = "uploaded-file-hash"
    get.return_value = httpx.Response(200, json=[child])
    assert await zotero_import._item_has_pdf_attachment("PARENT12")


async def test_failed_jobs_are_retained_after_retry_limit(tmp_queue_db, tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    pdf = tmp_path / "download.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    await zotero_import._enqueue_job(_make_job("10.1234/retry", pdf))
    import aiosqlite
    async with aiosqlite.connect(tmp_queue_db) as db:
        await db.execute("UPDATE import_queue SET attempts=7, run_after='1970-01-01'")
        await db.commit()
    monkeypatch.setitem(zotero_import._write_probe_result, "state", "ready")
    monkeypatch.setattr(zotero_import, "import_to_zotero", AsyncMock(return_value=False))
    original = zotero_import._reschedule_job
    async def reschedule(*args):
        await original(*args)
        raise asyncio.CancelledError
    monkeypatch.setattr(zotero_import, "_reschedule_job", reschedule)
    with pytest.raises(asyncio.CancelledError):
        await zotero_import._worker_loop()
    assert await zotero_import._queue_count() == 1
    async with aiosqlite.connect(tmp_queue_db) as db:
        cur = await db.execute("SELECT attempts FROM import_queue")
        assert (await cur.fetchone())[0] == 8


async def test_doi_lookup_scans_changes_instead_of_quicksearch(monkeypatch):
    import httpx
    from unittest.mock import AsyncMock
    monkeypatch.setattr(zotero_import, '_mirror_doi_state', lambda doi: (None, 123))
    get = AsyncMock(return_value=httpx.Response(200, json=[{
        'key': 'NEWITEM1', 'data': {'DOI': '10.1234/new', 'itemType': 'journalArticle'},
    }]))
    monkeypatch.setattr(zotero_import.zotero, '_web_api_get', get)
    assert (await zotero_import._lookup_web_doi('10.1234/new'))['key'] == 'NEWITEM1'
    params = get.call_args.args[1]
    assert params['since'] == '123'
    assert 'q' not in params


async def test_doi_lookup_paginates_and_normalizes(monkeypatch):
    import httpx
    from unittest.mock import AsyncMock
    monkeypatch.setattr(zotero_import, '_mirror_doi_state', lambda doi: (None, 0))
    get = AsyncMock(side_effect=[
        httpx.Response(200, json=[{'data': {}}] * 100),
        httpx.Response(200, json=[{'key': 'PAGE2KEY', 'data': {'DOI': 'https://doi.org/10.1234/NEW'}}]),
    ])
    monkeypatch.setattr(zotero_import.zotero, '_web_api_get', get)
    assert (await zotero_import._lookup_web_doi('10.1234/new'))['key'] == 'PAGE2KEY'
    assert get.call_args.args[1]['start'] == '100'


async def test_failed_duplicate_check_blocks_creation(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(zotero_import, '_mirror_doi_state', lambda doi: (None, 0))
    monkeypatch.setattr(zotero_import.zotero, '_web_api_get', AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match='Cannot verify Zotero duplicates'):
        await zotero_import._lookup_web_doi('10.1234/new')


async def test_metadata_job_does_not_replace_pending_pdf(tmp_queue_db, tmp_path):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF-1.4\n')
    await zotero_import._enqueue_job(_make_job('10.1234/combined', pdf))
    await zotero_import._enqueue_job(_make_job('10.1234/combined', None))
    import aiosqlite
    async with aiosqlite.connect(tmp_queue_db) as db:
        cur = await db.execute('SELECT pdf_path FROM import_queue')
        assert Path((await cur.fetchone())[0]).read_bytes() == b'%PDF-1.4\n'


async def test_html_article_saves_metadata_without_attachment(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(zotero_import.config, 'auto_import_to_zotero', True)
    monkeypatch.setattr(zotero_import.zot_config, 'api_key', 'key')
    monkeypatch.setattr(zotero_import.zot_config, 'user_id', '123')
    monkeypatch.setattr(zotero_import, '_doi_exists_in_zotero', AsyncMock(return_value=None))
    monkeypatch.setattr(zotero_import, '_fetch_crossref_metadata', AsyncMock(return_value={'title': 'Article'}))
    create = AsyncMock(return_value=('NEWITEM1', '', 200))
    attach = AsyncMock()
    monkeypatch.setattr(zotero_import, '_create_zotero_item', create)
    monkeypatch.setattr(zotero_import, '_attach_pdf_to_item', attach)
    monkeypatch.setattr(zotero_import, '_remember_imported_doi', AsyncMock())
    job = _make_job('10.1234/html', None)
    job.cached_article.source = 'html_extraction (trafilatura)'
    assert await zotero_import.import_to_zotero(job.doi, None, job.cached_article)
    create.assert_awaited_once()
    attach.assert_not_awaited()


async def test_successful_import_updates_discovery_index(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    index = {}
    monkeypatch.setattr(zotero_import.zotero, 'get_doi_index', AsyncMock(return_value=index))
    save = Mock()
    monkeypatch.setattr(zotero_import.zotero, '_save_index', save)
    await zotero_import._remember_imported_doi('10.1234/NEW', 'NEWITEM1', True)
    assert index['10.1234/new']['item_key'] == 'NEWITEM1'
    assert index['10.1234/new']['has_pdf'] is True
    save.assert_called_once_with(index)
