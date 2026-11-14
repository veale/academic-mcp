"""Durable, leased handoff to the user's authenticated desktop browser.

The worker returns content, never credentials. Library writes remain on the
server's existing DOI-deduplicated Zotero import path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

import aiosqlite
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import config

_locks: dict[str, asyncio.Lock] = {}
ACTIVE = ("queued", "claimed", "needs_login", "needs_review")
LEASE_SECONDS = 600


def root() -> Path:
    return Path(os.getenv("BROWSER_ACQUISITION_DIR", str(config.pdf_cache_dir.parent / "browser-acquisition")))


def worker_token() -> str:
    value = os.getenv("BROWSER_WORKER_TOKEN", "").strip()
    if value:
        return value
    try:
        return (root() / "worker-token").read_text().strip()
    except FileNotFoundError:
        return ""


def enabled() -> bool:
    return bool(worker_token()) and os.getenv("BROWSER_ACQUISITION_ENABLED", "true").lower() not in {"false", "0"}


async def db():
    root().mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(root() / "queue.sqlite", timeout=30)
    conn.row_factory = aiosqlite.Row
    await conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, identity TEXT NOT NULL, doi TEXT, url TEXT NOT NULL,
        state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
        worker TEXT, lease TEXT, expires REAL, attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '{}', target_library TEXT NOT NULL DEFAULT '')""")
    columns = {r[1] for r in await (await conn.execute("PRAGMA table_info(jobs)")).fetchall()}
    if "target_library" not in columns:
        await conn.execute("ALTER TABLE jobs ADD COLUMN target_library TEXT NOT NULL DEFAULT ''")
    await conn.execute("CREATE INDEX IF NOT EXISTS jobs_identity ON jobs(identity)")
    await conn.execute("CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, seen REAL NOT NULL)")
    await conn.commit()
    return conn


def public(row):
    if row is None:
        return None
    item = dict(row)
    item.pop("lease", None)
    item["result"] = json.loads(item["result"])
    return item


async def request_acquisition(doi=None, url=None):
    if not enabled():
        return {"enabled": False, "message": "Desktop browser worker is not configured"}
    from .zotero import _normalize_doi, zot_config
    doi = _normalize_doi(doi or "")
    if doi and not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        raise ValueError("A canonical DOI is required")
    url = url or ("https://doi.org/" + doi if doi else "")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An http(s) publisher URL or DOI is required")
    if len(url) > 8000:
        raise ValueError("Article URL is too long")
    identity = doi or url
    conn = await db()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute(
            "SELECT * FROM jobs WHERE identity=? AND state IN ('queued','claimed','needs_login','needs_review') ORDER BY created DESC LIMIT 1", (identity,)
        )).fetchone()
        if not row:
            count = await (await conn.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','claimed','needs_login','needs_review')")).fetchone()
            if count[0] >= 100:
                raise ValueError("Desktop acquisition queue is full (100 pending requests)")
            job_id = secrets.token_hex(12)
            now = time.time()
            await conn.execute("INSERT INTO jobs(id,identity,doi,url,state,created,updated,target_library) VALUES(?,?,?,?,?,?,?,?)",
                               (job_id, identity, doi or None, url, "queued", now, now, zot_config.library_prefix))
            row = await (await conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))).fetchone()
        await conn.commit()
        return public(row)
    finally:
        await conn.close()


async def status(job_id=None):
    conn = await db()
    try:
        if job_id:
            row = await (await conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))).fetchone()
            result = public(row)
            if result and result["result"].get("doi"):
                from . import zotero_import
                doi = result["result"]["doi"]
                if result["result"].get("zotero", "").startswith("queued"):
                    await zotero_import._ensure_queue_db()
                    async with aiosqlite.connect(zotero_import._QUEUE_DB_PATH) as imports:
                        pending = await (await imports.execute("SELECT last_error FROM import_queue WHERE doi=?", (doi,))).fetchone()
                    if pending:
                        result["zotero"] = {"state": "pending", "last_error": pending[0]}
                    else:
                        try:
                            item = await zotero_import._doi_exists_in_zotero(doi)
                            has_pdf = await zotero_import._item_has_pdf_attachment(item["key"]) if item else False
                            result["zotero"] = {"state": "saved" if item else "unverified",
                                                "item_key": item["key"] if item else None,
                                                "pdf_available": has_pdf}
                        except Exception as exc:
                            result["zotero"] = {"state": "unverified", "message": str(exc)[:300]}
            return result
        rows = await (await conn.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 30")).fetchall()
        workers = await (await conn.execute("SELECT id,seen FROM workers")).fetchall()
        return {"enabled": enabled(), "jobs": [public(row) for row in rows],
                "workers": [{"id": r[0], "last_seen": r[1], "online": time.time() - r[1] < 60} for r in workers]}
    finally:
        await conn.close()


async def control(job_id, action):
    if action not in {"resume", "cancel"}:
        raise ValueError("Expected resume or cancel")
    conn = await db()
    try:
        if action == "cancel":
            await conn.execute("UPDATE jobs SET state='cancelled',lease=NULL,updated=? WHERE id=? AND state!='complete'", (time.time(), job_id))
        else:
            # A paused worker retains the page; resume does not restart navigation.
            await conn.execute("UPDATE jobs SET state='queued',retry_at=0,updated=? WHERE id=? AND state IN ('needs_login','needs_review','failed')", (time.time(), job_id))
        await conn.commit()
    finally:
        await conn.close()
    return await status(job_id)


async def claim(worker):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", worker):
        raise ValueError("Invalid worker identifier")
    conn = await db()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        await conn.execute("INSERT OR REPLACE INTO workers VALUES(?,?)", (worker, now))
        # One browser job at a time, including human-paused jobs. Expired active
        # work is recoverable after a crash; paused jobs require explicit resume.
        row = await (await conn.execute("SELECT * FROM jobs WHERE worker=? AND state IN ('claimed','needs_login','needs_review') ORDER BY created LIMIT 1", (worker,))).fetchone()
        if not row:
            await conn.execute("UPDATE jobs SET state='queued',worker=NULL,lease=NULL WHERE state='claimed' AND expires<?", (now,))
            blocked = await (await conn.execute("SELECT 1 FROM jobs WHERE state IN ('claimed','needs_login','needs_review') LIMIT 1")).fetchone()
            if not blocked:
                row = await (await conn.execute("SELECT * FROM jobs WHERE state='queued' AND retry_at<=? ORDER BY created LIMIT 1", (now,))).fetchone()
                if row:
                    await conn.execute("UPDATE jobs SET state='claimed',worker=?,lease=?,expires=?,attempts=attempts+1,updated=? WHERE id=?",
                                       (worker, secrets.token_hex(24), now + LEASE_SECONDS, now, row["id"]))
        if row:
            await conn.execute("UPDATE jobs SET expires=?,updated=? WHERE id=?", (now + LEASE_SECONDS, now, row["id"]))
            row = await (await conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],))).fetchone()
        await conn.commit()
        # Only the authenticated worker receives the lease capability.
        return dict(row) if row else None
    finally:
        await conn.close()


async def owned(conn, job_id, lease):
    row = await (await conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))).fetchone()
    if not row or not lease or not secrets.compare_digest(row["lease"] or "", lease):
        raise ValueError("Job lease is no longer owned by this worker")
    if row["state"] not in {"claimed", "needs_login", "needs_review", "queued"} or row["expires"] < time.time():
        raise ValueError("Job is cancelled, finished or its lease has expired")
    return row


async def report(job_id, lease, state, message=""):
    if state not in {"claimed", "needs_login", "needs_review", "complete", "retry", "failed"}:
        raise ValueError("Invalid worker report")
    conn = await db()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        row = await owned(conn, job_id, lease)
        if state == "complete" and not json.loads(row["result"]).get("content_ready"):
            raise ValueError("Cannot complete acquisition before content is available")
        retry_at = 0
        if state == "retry":
            state = "failed" if row["attempts"] >= 4 else "queued"
            retry_at = time.time() + min(1800, 30 * 2 ** row["attempts"])
        await conn.execute("UPDATE jobs SET state=?,message=?,retry_at=?,updated=? WHERE id=?",
                           (state, message[:1500], retry_at, time.time(), job_id))
        await conn.commit()
    finally:
        await conn.close()
    return await status(job_id)


async def ingest(job_id, lease, kind, data, metadata):
    from . import content_extractor, pdf_work, text_cache, zotero_import, zotero
    if kind not in {"html", "pdf"}:
        raise ValueError("Unknown artifact type")
    async with _locks.setdefault(job_id, asyncio.Lock()):
        conn = await db()
        try:
            row = await owned(conn, job_id, lease)
            if row["target_library"] and row["target_library"] != zotero.zot_config.library_prefix:
                raise ValueError("Configured Zotero target library changed; cancel and request acquisition again")
            doi = row["doi"] or zotero._normalize_doi(metadata.get("doi", ""))
            if doi and not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
                raise ValueError("Invalid DOI returned by browser")
            returned = zotero._normalize_doi(metadata.get("doi", ""))
            if row["doi"] and returned and row["doi"] != returned:
                raise ValueError("Browser returned a different article DOI")
            identity = doi or "url:" + hashlib.sha256(row["url"].encode()).hexdigest()[:16]
            folder = root() / "artifacts" / job_id
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ("article." + kind)
            used = sum(p.stat().st_size for p in (root() / "artifacts").rglob("*") if p.is_file())
            replacing = path.stat().st_size if path.exists() else 0
            quota = int(os.getenv("BROWSER_ARTIFACT_QUOTA_MB", "2048")) * 1024 * 1024
            if used - replacing + len(data) > quota:
                raise ValueError("Browser artifact storage quota reached; retain needed files and clear old artifacts")
            if kind == "pdf" and not data.lstrip().startswith(b"%PDF-"):
                raise ValueError("Browser response is not a PDF")
            if kind == "html":
                html = data.decode("utf-8")
                meta = content_extractor.extract_citation_meta(html, metadata.get("url") or row["url"])
                page_doi = zotero._normalize_doi(meta.get("citation_doi", ""))
                if doi and page_doi and doi != page_doi:
                    raise ValueError("HTML citation DOI does not match the request")
                result = await content_extractor.extract_article_with_sections(html, metadata.get("url") or row["url"])
                if not result:
                    raise ValueError("HTML does not contain a sufficiently complete article; try its PDF")
                path.write_bytes(data)
            else:
                temporary = path.with_suffix(".partial")
                temporary.write_bytes(data)
                temporary.replace(path)
                result = await pdf_work.extract(path, config.use_pymupdf4llm)
                if len(result.get("text", "").split()) < 30:
                    raise ValueError("PDF has no usable text (OCR may be required)")
            await conn.execute("BEGIN IMMEDIATE")
            await owned(conn, job_id, lease)  # A cancellation during extraction wins.
            existing = text_cache.get_cached(identity)
            # Keep successful HTML for reading even when an archival PDF follows.
            if kind == "pdf" and existing and existing.source == "desktop_browser_html":
                cached = text_cache.put_cached(identity, existing.text, existing.source, existing.sections,
                    section_detection=existing.section_detection, word_count=existing.word_count,
                    metadata={**existing.metadata, **metadata}, pdf_path=str(path), html_path=existing.html_path)
            else:
                cached = text_cache.put_cached(identity, result["text"], "desktop_browser_" + kind,
                    result.get("sections") or content_extractor.detect_sections_from_text(result["text"]),
                    section_detection=result.get("section_detection", "html_headings" if kind == "html" else "pdf_font_analysis"),
                    metadata=metadata, pdf_path=str(path) if kind == "pdf" else (existing.pdf_path if existing else None),
                    html_path=str(path) if kind == "html" else (existing.html_path if existing else None))
            # put_cached historically logs write errors. Never acknowledge a lost artifact.
            verified = text_cache.get_cached(identity)
            if not verified or verified.text != cached.text or verified.source != cached.source:
                raise RuntimeError("Could not persist the article cache")
            previous = json.loads(row["result"])
            result_status = {**previous, "content_ready": True, "doi": doi or None, "cache_key": text_cache._cache_key(identity),
                             "word_count": cached.word_count, kind + "_received": True}
            if doi and config.auto_import_to_zotero:
                # Await durable queue persistence; do not depend on a fire-and-forget task.
                await zotero_import._enqueue_job(zotero_import._ImportJob(doi, Path(cached.pdf_path) if cached.pdf_path else None, cached))
                await zotero_import.ensure_auto_import_initialized()
                zotero_import._ensure_worker_running()
                result_status["zotero"] = "queued; check zotero_import_status for completion"
            else:
                result_status["zotero"] = "not queued: automatic import disabled or DOI unavailable"
            await conn.execute("UPDATE jobs SET result=?,updated=? WHERE id=? AND lease=? AND state IN ('claimed','needs_login','needs_review','queued')",
                               (json.dumps(result_status), time.time(), job_id, lease))
            await conn.commit()
            return result_status
        finally:
            await conn.close()


def routes():
    async def endpoint(request):
        expected = worker_token()
        if not enabled() or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        action = request.path_params["action"]
        try:
            if action == "upload":
                limit = 75 * 1024 * 1024 if request.query_params.get("kind") == "pdf" else 25 * 1024 * 1024
                data = bytearray()
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > limit:
                        return JSONResponse({"error": "Artifact too large"}, status_code=413)
                metadata = json.loads(request.headers.get("x-article-metadata", "{}"))
                result = await ingest(request.query_params["id"], request.headers.get("x-job-lease", ""),
                                      request.query_params["kind"], bytes(data), metadata)
            else:
                raw = await request.body()
                if len(raw) > 16384:
                    return JSONResponse({"error": "Request too large"}, status_code=413)
                body = json.loads(raw or b"{}")
                if action == "claim":
                    result = await claim(body["worker"])
                elif action == "report":
                    result = await report(body["id"], body["lease"], body["state"], body.get("message", ""))
                elif action == "status":
                    result = await status(body.get("id"))
                elif action == "recover":
                    from . import zotero
                    conn = await db()
                    try:
                        row = await owned(conn, body["id"], body["lease"])
                    finally:
                        await conn.close()
                    paper = await zotero.get_paper_from_zotero(row["doi"]) if row["doi"] else None
                    if paper and paper.get("pdf_path"):
                        result = await ingest(row["id"], body["lease"], "pdf", Path(paper["pdf_path"]).read_bytes(),
                                              {"doi": row["doi"], "url": row["url"], "recovered_from_zotero": True})
                    else:
                        result = {"content_ready": False, "message": "Synced Zotero PDF is not yet available"}
                else:
                    return JSONResponse({"error": "Unknown action"}, status_code=404)
            return JSONResponse(result)
        except (ValueError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    return [Route("/acquisition/{action}", endpoint, methods=["POST"])]
