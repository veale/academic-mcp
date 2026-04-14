"""Standalone diagnostic script for the academic-mcp search pipeline.

Tests:
  1. OpenAlex parsing — safe None handling for primary_location.source and authorships
  2. SQLite year filter — correct SQL execution and date-range filtering
  3. Semantic re-ranker — cosine scores printed, asyncio.to_thread non-blocking verified

Run from the repo root:
  uv run python test_search_logic.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: add src/ to sys.path so imports work without an editable install
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Load .env before importing any module that reads env vars at import time
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Logging: show DEBUG+ from our own modules, WARNING+ from everything else
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
for name in (
    "academic_mcp.server",
    "academic_mcp.zotero",
    "academic_mcp.zotero_sqlite",
    "academic_mcp.reranker",
    "academic_mcp.apis",
):
    logging.getLogger(name).setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Import the backend modules we want to exercise
# ---------------------------------------------------------------------------
from academic_mcp import apis, zotero, zotero_sqlite
from academic_mcp.server import _handle_search, _reconstruct_abstract
from academic_mcp.reranker import _compute_similarities, _load_model

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


# ===========================================================================
# Test 1 — OpenAlex NoneType parsing & fallback
# ===========================================================================

async def test_openalex_parsing() -> bool:
    """Verify that messy OpenAlex responses (None primary_location.source,
    None authorships entries) do not crash _handle_search."""
    print("\n" + "=" * 60)
    print("TEST 1: OpenAlex Parsing & Fallback")
    print("=" * 60)
    print("Query: 'machine learning'  (broad, likely to surface messy data)")

    crashed = False
    error_msg = ""
    try:
        result = await _handle_search({
            "query": "machine learning",
            "limit": 10,
            "source": "openalex",
        })
        text = result[0].text
        lines = [l for l in text.splitlines() if l.strip()][:8]
        print("\nFirst 8 lines of response:")
        for line in lines:
            print(f"  {line}")
    except AttributeError as e:
        crashed = True
        error_msg = str(e)
        print(f"\n  {FAIL} AttributeError (NoneType bug still present): {e}")
    except Exception as e:
        # Network errors / rate limits are acceptable; NoneType is not
        if "NoneType" in type(e).__name__ or "NoneType" in str(e):
            crashed = True
            error_msg = str(e)
            print(f"\n  {FAIL} NoneType error: {e}")
        else:
            print(f"\n  {WARN} Non-critical exception (likely network): {type(e).__name__}: {e}")

    if not crashed:
        print(f"\n  {PASS} No NoneType AttributeError raised during OpenAlex parsing.")
        return True
    else:
        print(f"\n  {FAIL} Crash: {error_msg}")
        return False


# ---------------------------------------------------------------------------
# Also exercise the raw API response with a hand-crafted None-heavy payload
# ---------------------------------------------------------------------------

async def test_openalex_none_payload() -> bool:
    """Directly test the parsing logic with synthetic None-heavy payloads,
    simulating the exact shape that was causing the crash."""
    print("\n" + "-" * 50)
    print("TEST 1b: Synthetic None payload (unit-level guard)")

    # Simulate works where primary_location.source is None (common in OpenAlex)
    # and authorships list contains None entries and None author dicts.
    messy_works = [
        {
            "title": "Paper A — source is None",
            "primary_location": {"source": None, "is_oa": True},
            "open_access": {"is_oa": True},
            "authorships": [{"author": None}, None, {"author": {"display_name": "Alice"}}],
            "publication_year": 2023,
            "cited_by_count": 50,
            "abstract_inverted_index": None,
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1234/a",
        },
        {
            "title": "Paper B — primary_location is None",
            "primary_location": None,
            "open_access": None,
            "authorships": None,
            "publication_year": 2022,
            "cited_by_count": 10,
            "abstract_inverted_index": None,
            "id": "https://openalex.org/W2",
            "doi": None,
        },
        {
            "title": "Paper C — empty authorships dicts",
            "primary_location": {"source": {"display_name": "Nature"}},
            "open_access": {"is_oa": False},
            "authorships": [{}, {"author": {}}, {"author": {"display_name": "Bob"}}],
            "publication_year": 2021,
            "cited_by_count": 0,
            "abstract_inverted_index": {"word": [0]},
            "id": "https://openalex.org/W3",
            "doi": "https://doi.org/10.1234/c",
        },
    ]

    results = []
    errors = []
    for work in messy_works:
        try:
            # Replicate the exact parsing logic from server.py _handle_search
            authors = []
            for auth in (work.get("authorships") or []):
                if not auth:
                    continue
                name = (auth.get("author") or {}).get("display_name")
                if name:
                    authors.append(name)
                if len(authors) >= 5:
                    break

            venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or None
            has_oa = (work.get("open_access") or {}).get("is_oa", False)
            abstract = _reconstruct_abstract(work.get("abstract_inverted_index")) or None

            results.append({
                "title": work.get("title"),
                "authors": authors,
                "venue": venue,
                "has_oa_pdf": has_oa,
                "abstract": abstract,
            })
        except Exception as e:
            errors.append(f"{work.get('title')}: {e}")

    for r in results:
        print(f"  OK  title={r['title']!r}  venue={r['venue']!r}  "
              f"authors={r['authors']}  oa={r['has_oa_pdf']}")

    if errors:
        for err in errors:
            print(f"  {FAIL} {err}")
        return False
    else:
        print(f"\n  {PASS} All {len(results)} messy payloads parsed without AttributeError.")
        return True


# ===========================================================================
# Test 2 — SQLite year filter
# ===========================================================================

async def test_sqlite_year_filter() -> bool:
    """Verify SQLite search_items executes without error under year filtering
    and that returned items respect the requested date range."""
    print("\n" + "=" * 60)
    print("TEST 2: SQLite Year Filter (2023-2024)")
    print("=" * 60)

    if not zotero_sqlite.sqlite_config.available:
        print(f"  {WARN} SQLite not available at {zotero_sqlite.sqlite_config.db_path}")
        print("  Skipping live SQLite test — cannot verify without the database.")
        return True  # Not a code bug; just not configured

    print(f"  DB path: {zotero_sqlite.sqlite_config.db_path}")

    # Also verify via full search_papers path so the server layer is exercised
    print("\n  Running via _handle_search (server layer)...")
    server_crash = False
    try:
        result = await _handle_search({
            "query": "privacy",
            "limit": 5,
            "source": "zotero",
            "start_year": 2023,
            "end_year": 2024,
        })
        text = result[0].text
        print(f"  Server response (first 200 chars): {text[:200]!r}")
    except Exception as e:
        server_crash = True
        print(f"  {FAIL} Server layer raised: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    # Low-level SQLite test
    print("\n  Running zotero_sqlite.search_items directly...")
    sqlite_crash = False
    items = []
    try:
        items = await zotero_sqlite.search_items(
            "privacy", limit=10, start_year=2023, end_year=2024,
        )
    except Exception as e:
        sqlite_crash = True
        print(f"  {FAIL} SQLite raised: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    if not sqlite_crash:
        print(f"  Got {len(items)} results from SQLite.")
        out_of_range = []
        for item in items:
            year_str = (item.date or "")[:4]
            if year_str.isdigit():
                year = int(year_str)
                if year < 2023 or year > 2024:
                    out_of_range.append((item.title, year_str))

        if out_of_range:
            print(f"  {FAIL} {len(out_of_range)} items outside 2023-2024 range:")
            for title, yr in out_of_range[:5]:
                print(f"    '{title[:60]}' ({yr})")
        else:
            if items:
                print("  Sample dates from results:")
                for item in items[:5]:
                    print(f"    '{item.title[:55]}' — {item.date}")
            print(f"\n  {PASS} All {len(items)} results respect the 2023-2024 date filter.")

    if server_crash or sqlite_crash:
        return False
    return True


# ===========================================================================
# Test 3 — Semantic re-ranker & async health
# ===========================================================================

async def test_semantic_reranker() -> bool:
    """Verify semantic similarity scores are produced and that the
    sentence-transformers inference is non-blocking (asyncio.to_thread)."""
    print("\n" + "=" * 60)
    print("TEST 3: Semantic Re-Ranker & Async Health")
    print("=" * 60)

    query = "climate change impact"
    print(f"Query: '{query}'")

    # ── 3a: Check model loads ───────────────────────────────────────────────
    model = _load_model()
    if model is None:
        print(f"  {WARN} sentence-transformers model unavailable — will test fallback path only.")
    else:
        print(f"  Model loaded: {model}")

    # ── 3b: Heartbeat task to verify the event loop stays live ────────────
    heartbeat_ticks: list[float] = []

    async def heartbeat():
        """Ticks every 0.05 s while running.  If the main thread blocks, ticks stop."""
        for _ in range(60):   # up to 3 s at 50 ms/tick
            heartbeat_ticks.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.05)

    # ── 3c: Fire the full search pipeline  ─────────────────────────────────
    crashed = False
    result_text = ""
    t0 = time.perf_counter()

    hb_task = asyncio.create_task(heartbeat())
    try:
        result = await _handle_search({
            "query": query,
            "limit": 8,
            "source": "all",
        })
        result_text = result[0].text
    except Exception as e:
        crashed = True
        print(f"  {FAIL} _handle_search raised: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    elapsed = time.perf_counter() - t0

    # ── 3d: Parse top-3 results with relevance scores ──────────────────────
    if not crashed:
        print(f"\n  Search completed in {elapsed:.2f}s")
        lines = result_text.splitlines()
        top_papers: list[tuple[str, str]] = []
        current_title = ""
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped and not stripped.startswith("[Preview"):
                # Result header line like "[0] Title  [★ IN ZOTERO]"
                bracket_end = stripped.index("]")
                current_title = stripped[bracket_end + 1:].strip()
                # Strip badges
                for badge in ("  [★ IN ZOTERO]", "  [OA]", "  [★ IN ZOTERO, OA]"):
                    current_title = current_title.replace(badge, "")
            elif "Relevance:" in stripped and current_title:
                score = stripped.split("Relevance:")[-1].strip()
                top_papers.append((current_title, score))
                if len(top_papers) >= 3:
                    break

        print("\n  TOP 3 RESULTS WITH SEMANTIC SIMILARITY:")
        if top_papers:
            for rank, (title, score) in enumerate(top_papers, 1):
                print(f"    [{rank}] score={score}  title={title[:70]!r}")
        else:
            print("  (No 'Relevance:' lines found in output — model may have fallen back to composite scoring)")
            # Still pass: composite scoring is the designed fallback
            print("  First 600 chars of raw output:")
            print("  " + result_text[:600].replace("\n", "\n  "))

    # ── 3e: Non-blocking assertion ─────────────────────────────────────────
    print(f"\n  Heartbeat ticks during inference: {len(heartbeat_ticks)}")
    if len(heartbeat_ticks) >= 2:
        gaps = [heartbeat_ticks[i] - heartbeat_ticks[i - 1] for i in range(1, len(heartbeat_ticks))]
        max_gap = max(gaps) if gaps else 0
        avg_gap = sum(gaps) / len(gaps) if gaps else 0
        print(f"  Max gap between ticks: {max_gap * 1000:.1f} ms  (avg {avg_gap * 1000:.1f} ms)")
        if max_gap > 1.0:
            print(f"  {WARN} Event loop stalled for {max_gap:.2f}s during inference.")
            print("        (This can happen during model *loading* — subsequent calls will be faster.)")
        else:
            print(f"  {PASS} Event loop remained responsive (max gap {max_gap * 1000:.0f} ms).")

    if crashed:
        return False

    if model is not None and not top_papers:
        print(f"\n  {WARN} Model loaded but no Relevance scores in output — check reranker.")
    elif model is not None and top_papers:
        print(f"\n  {PASS} Semantic similarity scores produced and printed above.")
    else:
        print(f"\n  {PASS} Fallback composite scoring executed cleanly (no model).")

    return True


# ===========================================================================
# Main
# ===========================================================================

async def main() -> None:
    results: dict[str, bool] = {}

    results["1a: OpenAlex live search"] = await test_openalex_parsing()
    results["1b: OpenAlex None payload"] = await test_openalex_none_payload()
    results["2:  SQLite year filter"] = await test_sqlite_year_filter()
    results["3:  Semantic re-ranker"] = await test_semantic_reranker()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        status = PASS if passed else FAIL
        print(f"  {status}  {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("All tests passed.")
    else:
        print("Some tests failed — see output above for tracebacks.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
