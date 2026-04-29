#!/usr/bin/env python3
"""
Semantic index diagnostics.

Usage (inside the project venv):
    python diagnose_semantic.py [--query "your query here"] [--search-only] [--fix]

Checks:
  1. Environment / config (SEMANTIC_CACHE_DIR, embedder env vars)
  2. Status file (last_sync, in_progress, pending, counts)
  3. ChromaDB collection health (can open, actual item count)
  4. SQLite shadow DB (items available for indexing)
  5. Embedder probe (can we embed a short test string?)
  6. End-to-end search (optional, requires --query)
  7. --fix: force-trigger a sync inside this process (requires running
     the MCP server process to already hold ChromaDB; useful for
     a one-shot repair in the same process context)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone


# ── helpers ──────────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)

def ok(msg: str) -> None:
    print(f"  [OK]  {msg}")

def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")

def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")

def info(msg: str) -> None:
    print(f"  [INFO] {msg}")


# ── 1. Environment ────────────────────────────────────────────────────────────

def check_env():
    section("1. Environment / Config")

    cache_dir = os.getenv("SEMANTIC_CACHE_DIR")
    if cache_dir:
        ok(f"SEMANTIC_CACHE_DIR = {cache_dir}")
    else:
        default = Path.home() / ".cache" / "academic-mcp" / "chroma"
        info(f"SEMANTIC_CACHE_DIR not set → default: {default}")
        cache_dir = str(default)

    for var in [
        "SEMANTIC_PROVIDER", "SEMANTIC_MODEL",
        "BULK_OPENAI_BASE_URL", "BULK_OPENAI_API_KEY", "BULK_OPENAI_MODEL",
        "OPENAI_BASE_URL", "OPENAI_API_KEY",
        "ZOTERO_SQLITE_PATH",
    ]:
        val = os.getenv(var)
        if val:
            masked = val[:8] + "…" if "KEY" in var and len(val) > 8 else val
            ok(f"{var} = {masked}")
        else:
            info(f"{var} not set")

    return cache_dir


# ── 2. Status file ────────────────────────────────────────────────────────────

def check_status(cache_dir: str):
    section("2. Status File")

    status_path = Path(cache_dir) / "status.json"
    if not status_path.exists():
        warn("status.json not found — sync has never completed")
        return None

    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"Could not parse status.json: {e}")
        return None

    print(f"  {json.dumps(status, indent=4)}")

    last_sync = status.get("last_sync")
    if last_sync:
        try:
            ts = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age < 24:
                ok(f"last_sync = {last_sync}  ({age:.1f}h ago)")
            else:
                warn(f"last_sync = {last_sync}  ({age:.1f}h ago — STALE)")
        except Exception:
            warn(f"last_sync = {last_sync}  (could not parse)")

    pending = status.get("pending", 0)
    in_progress = status.get("in_progress", False)
    count = status.get("count", 0)

    if in_progress:
        warn("in_progress = True (sync may be running or crashed)")
    else:
        ok("in_progress = False")

    if pending > 0:
        warn(f"pending = {pending} chunks still to embed")
    else:
        ok(f"pending = 0  (all {count} chunks indexed)")

    return status


# ── 3. ChromaDB collection ────────────────────────────────────────────────────

def check_chroma(cache_dir: str):
    section("3. ChromaDB Collection")

    try:
        import chromadb
    except ImportError:
        fail("chromadb not installed — pip install chromadb")
        return None

    try:
        t0 = time.time()
        client = chromadb.PersistentClient(path=cache_dir)
        elapsed = time.time() - t0
        ok(f"PersistentClient opened in {elapsed:.2f}s")
    except Exception as e:
        fail(f"Could not open ChromaDB: {e}")
        return None

    try:
        col = client.get_or_create_collection("zotero_items")
        count = col.count()
        ok(f"Collection 'zotero_items' has {count:,} vectors")
    except Exception as e:
        fail(f"Could not open collection: {e}")
        return None

    # Peek at a few records to check metadata shape
    try:
        sample = col.get(limit=3, include=["metadatas"])
        ids = sample.get("ids", [])
        metas = sample.get("metadatas", [])
        if ids:
            info(f"Sample IDs: {ids}")
            for m in metas[:2]:
                info(f"  metadata: {m}")
        # Check for chunk_idx (new format) vs old single-vector format
        has_chunk_idx = any(m and "chunk_idx" in m for m in metas)
        if has_chunk_idx:
            ok("Metadata format: chunked (has chunk_idx) ✓")
        else:
            warn("Metadata format: old single-vector (no chunk_idx) — rebuild needed")
    except Exception as e:
        warn(f"Could not sample collection: {e}")

    return col


# ── 4. SQLite items ───────────────────────────────────────────────────────────

async def check_sqlite():
    section("4. SQLite Shadow DB")

    try:
        from src.academic_mcp import zotero_sqlite
    except ImportError:
        try:
            from academic_mcp import zotero_sqlite
        except ImportError:
            fail("Could not import zotero_sqlite — run from project root with venv active")
            return 0

    if not zotero_sqlite.sqlite_config.available:
        fail("SQLite not available — ZOTERO_SQLITE_PATH not set or file missing")
        return 0

    ok(f"SQLite path: {zotero_sqlite.sqlite_config.path}")

    try:
        items = await zotero_sqlite.list_items_for_semantic_index()
        ok(f"Items available for indexing: {len(items):,}")
        if items:
            sample = items[0]
            info(f"  Sample item: key={sample.get('item_key')} title={sample.get('title','')[:60]!r}")
        return len(items)
    except Exception as e:
        fail(f"list_items_for_semantic_index() failed: {e}")
        import traceback; traceback.print_exc()
        return 0


# ── 5. Embedder probe ─────────────────────────────────────────────────────────

async def check_embedder():
    section("5. Embedder Probe")

    try:
        from src.academic_mcp.embeddings import resolve_embedder
    except ImportError:
        try:
            from academic_mcp.embeddings import resolve_embedder
        except ImportError:
            fail("Could not import embeddings module")
            return False

    for mode in ("interactive", "bulk"):
        try:
            t0 = time.time()
            embedder = resolve_embedder(mode=mode)
            elapsed = time.time() - t0
            ok(f"Mode '{mode}': provider={embedder.provider} model={embedder.model} dim={embedder.dim}  (resolved in {elapsed:.2f}s)")

            t0 = time.time()
            vecs = embedder.encode(["This is a test sentence for embedding."])
            elapsed = time.time() - t0
            if vecs and len(vecs[0]) == embedder.dim:
                ok(f"Mode '{mode}': encode() returned {len(vecs[0])}-dim vector in {elapsed:.2f}s")
            else:
                fail(f"Mode '{mode}': encode() returned unexpected shape: {[len(v) for v in vecs]}")
        except Exception as e:
            fail(f"Mode '{mode}': {e}")

    return True


# ── 6. End-to-end search ──────────────────────────────────────────────────────

async def check_search(query: str):
    section(f"6. End-to-end Search: {query!r}")

    try:
        from src.academic_mcp.semantic_index import get_semantic_index
    except ImportError:
        try:
            from academic_mcp.semantic_index import get_semantic_index
        except ImportError:
            fail("Could not import semantic_index")
            return

    try:
        idx = get_semantic_index()
        t0 = time.time()
        results = await idx.search(query, k=5)
        elapsed = time.time() - t0
        ok(f"Search returned {len(results)} results in {elapsed:.2f}s")
        for i, r in enumerate(results, 1):
            print(f"\n  [{i}] score={r['score']:.4f}  key={r['item_key']}")
            print(f"       title={r['title'][:80]!r}")
            print(f"       snippet={r['snippet'][:120]!r}")
            print(f"       chunk={r['chunk_idx']}/{r['chunk_count']}  source={r['chunk_source']}")
    except Exception as e:
        fail(f"Search failed: {e}")
        import traceback; traceback.print_exc()


# ── 7. Trigger sync (in-process) ─────────────────────────────────────────────

async def trigger_sync():
    section("7. Triggering Sync (in-process)")

    try:
        from src.academic_mcp.semantic_index import get_semantic_index, SemanticIndexUnavailable
    except ImportError:
        try:
            from academic_mcp.semantic_index import get_semantic_index, SemanticIndexUnavailable
        except ImportError:
            fail("Could not import semantic_index")
            return

    try:
        idx = get_semantic_index()
        info("Starting sync — this will block until complete (watch logs above)")
        t0 = time.time()
        result = await idx.sync(force_rebuild=False, include_fulltext=False)
        elapsed = time.time() - t0
        ok(f"Sync completed in {elapsed:.1f}s")
        print(f"  Result: {json.dumps(result, indent=4)}")
    except SemanticIndexUnavailable as e:
        fail(f"SemanticIndexUnavailable: {e}")
    except Exception as e:
        fail(f"Sync failed: {e}")
        import traceback; traceback.print_exc()


# ── main ──────────────────────────────────────────────────────────────────────

async def amain(args):
    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    cache_dir = check_env()
    status = check_status(cache_dir)
    check_chroma(cache_dir)

    if not args.search_only:
        await check_sqlite()
        await check_embedder()

    if args.query:
        await check_search(args.query)

    if args.fix:
        await trigger_sync()

    section("Summary")
    if status:
        count = status.get("count", 0)
        pending = status.get("pending", 0)
        total = count + pending
        if total > 0:
            pct = 100.0 * count / total
            print(f"  Index: {count:,} / {total:,} chunks ({pct:.1f}% complete)")
        if pending > 0:
            warn(f"{pending:,} chunks still to embed — rebuild or wait for nightly sync")
            info("To trigger sync inside the running container:  curl http://localhost:8765/trigger-sync")
            info("To trigger sync here (blocks, embeds in this process):  python diagnose_semantic.py --fix")
        else:
            ok("Index appears complete")
    else:
        warn("No status file found — index has never been built")


def main():
    p = argparse.ArgumentParser(description="Semantic index diagnostics")
    p.add_argument("--query", "-q", default="", help="Run a test search with this query")
    p.add_argument("--search-only", action="store_true", help="Skip SQLite and embedder checks (faster)")
    p.add_argument("--fix", action="store_true", help="Trigger a sync directly in this process (blocking)")
    args = p.parse_args()

    # Add src/ to path so imports work from project root
    sys.path.insert(0, str(Path(__file__).parent / "src"))

    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
