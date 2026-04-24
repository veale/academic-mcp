"""Tests for the persistent Zotero auto-import queue."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import zotero_import  # noqa: E402


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
    import aiosqlite
    from datetime import datetime

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
