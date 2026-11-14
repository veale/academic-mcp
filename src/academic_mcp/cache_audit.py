"""Audit the article cache for text that is not the work it is filed under.

Entries cached before identity verification existed can hold a different
paper (a web-search hit that cites the requested one, a CORE title match,
a mislinked repository file).  This re-runs the identity check on every
entry, purges web-fetched mismatches so the next fetch retrieves the right
work, and reports Zotero items whose auto-imported attachment is wrong.
Nothing in the Zotero library is modified.

    python -m academic_mcp.cache_audit [--dry-run] [--json PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import identity, text_cache, zotero_sqlite
from .config import config
from .core import fetch


async def _audit_one(path: Path, dry_run: bool, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"file": path.name, "status": "unreadable", "reason": str(exc)}
        doi = str(data.get("doi") or "")
        meta = data.get("metadata") or {}
        verdict = meta.get("identity")
        if isinstance(verdict, dict) and not (
            fetch._is_zotero_source(str(data.get("source") or ""))
            and str(verdict.get("reason", "")).startswith("the attachment's opening text")
            and not meta.get("auto_import_checked")
        ):
            return {"doi": doi, "status": "already_checked", "verdict": verdict.get("status")}
        zotero_key = doi.split(":", 1)[1] if doi.startswith("zotero:") else None
        if doi.startswith("url:"):
            return {"doi": doi, "status": "skipped", "reason": "URL entry has no bibliographic record"}
        expected = await fetch._expected_for(doi, zotero_key)
        if not expected.get("title"):
            return {"doi": doi, "status": "skipped", "reason": "no metadata"}
        source = str(data.get("source") or "")
        kwargs = {"pdf_title": str(meta.get("pdf_title") or ""), "html": fetch._is_html_source(source)}
        check = identity.check_identity(data.get("text") or "", expected["title"], expected.get("authors"), **kwargs)
        if check.status != identity.PASS and fetch._is_real_doi(doi):
            published = await fetch._published_titles(doi)
            if published:
                check = identity.check_identity_any(
                    data.get("text") or "", [expected["title"], *published], expected.get("authors"), **kwargs)
        row = {
            "doi": doi, "source": source, "verdict": check.status,
            "expected_title": check.expected_title, "found_title": check.found_title,
            "reason": check.reason,
        }
        if dry_run:
            purge = check.rejected and (
                not fetch._is_zotero_source(source) or await fetch._is_auto_imported(doi, zotero_key))
            row["status"] = "would_purge" if purge else "checked"
            return row
        cached = text_cache.CachedArticle.from_dict(data)
        token = fetch._ctx.set(fetch._FetchContext())
        try:
            kept = await fetch._revalidate_cached(cached, doi, zotero_key)
        finally:
            fetch._ctx.reset(token)
        row["status"] = "purged" if kept is None else "annotated"
        return row


async def _auto_imported_items() -> dict[str, str]:
    """DOI -> item key for library items created by this server's auto-import."""
    out: dict[str, str] = {}
    if not zotero_sqlite.sqlite_config.available:
        return out
    conn = await zotero_sqlite._get_connection()
    try:
        doi_fid = await zotero_sqlite._get_field_id(conn, "DOI")
        cursor = await conn.execute(
            """SELECT i.key, LOWER(TRIM(v.value)) AS doi FROM items i
               JOIN itemTags it ON it.itemID = i.itemID JOIN tags t ON t.tagID = it.tagID
               JOIN itemData d ON d.itemID = i.itemID AND d.fieldID = ?
               JOIN itemDataValues v ON v.valueID = d.valueID
               WHERE t.name = 'auto-imported'""",
            (doi_fid,),
        )
        for row in await cursor.fetchall():
            out[row["doi"]] = row["key"]
    finally:
        await conn.close()
    from . import zotero_catalog
    if zotero_catalog.enabled():
        with zotero_catalog._connect() as db:
            for key, data in db.execute("SELECT key, data FROM items WHERE data LIKE '%auto-imported%'"):
                item = json.loads(data)
                if item.get("DOI") and any(t.get("tag") == "auto-imported" for t in item.get("tags", [])):
                    out[item["DOI"].lower().strip()] = key
    return out


async def run(dry_run: bool = False, concurrency: int = 4) -> dict:
    paths = sorted(config.pdf_cache_dir.glob("*.article.json"))
    sem = asyncio.Semaphore(concurrency)
    rows = [r for r in await asyncio.gather(*(_audit_one(p, dry_run, sem) for p in paths)) if r]
    auto = await _auto_imported_items()
    wrong_attachments = [
        {**r, "zotero_key": auto[r["doi"].lower()]}
        for r in rows
        if r.get("verdict") == identity.MISMATCH and str(r.get("doi") or "").lower() in auto
    ]
    summary: dict = {"entries": len(rows)}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {
        "summary": summary,
        "mismatches": [r for r in rows if r.get("verdict") == identity.MISMATCH],
        "uncertain": [r for r in rows if r.get("verdict") == identity.UNCERTAIN],
        "auto_imported_wrong_attachments": wrong_attachments,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()
    report = asyncio.run(run(dry_run=args.dry_run))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report["summary"]))
    for r in report["mismatches"]:
        print(f"MISMATCH {r['doi']} [{r.get('source', '')}] expected={r['expected_title'][:70]!r} "
              f"found={r['found_title'][:70]!r} -> {r['status']}")
    for r in report["auto_imported_wrong_attachments"]:
        print(f"WRONG ZOTERO ATTACHMENT {r['zotero_key']} {r['doi']}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
