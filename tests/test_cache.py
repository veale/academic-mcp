"""Tests for the TTL response cache and in-flight coalescing."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import cache as cache_mod
from academic_mcp.config import config


@pytest.fixture(autouse=True)
def _enable_cache(monkeypatch):
    monkeypatch.setattr(config, "search_cache_ttl", 900.0)
    monkeypatch.setattr(config, "search_cache_maxsize", 8)


@pytest.mark.asyncio
async def test_repeated_call_hits_cache():
    calls = 0

    @cache_mod.ttl_cached("t")
    async def fetch(q: str) -> str:
        nonlocal calls
        calls += 1
        return f"result-{q}"

    assert await fetch("a") == "result-a"
    assert await fetch("a") == "result-a"
    assert calls == 1


@pytest.mark.asyncio
async def test_distinct_args_are_distinct_entries():
    calls = 0

    @cache_mod.ttl_cached("t")
    async def fetch(q: str, limit: int = 5) -> str:
        nonlocal calls
        calls += 1
        return f"{q}-{limit}"

    await fetch("a")
    await fetch("a", limit=10)
    await fetch("b")
    assert calls == 3
    # kwargs and positional forms of the same call are separate keys; that is
    # a miss, not a bug, but the values must still be right.
    assert await fetch("a", limit=10) == "a-10"


@pytest.mark.asyncio
async def test_ttl_zero_disables_caching(monkeypatch):
    monkeypatch.setattr(config, "search_cache_ttl", 0.0)
    calls = 0

    @cache_mod.ttl_cached("t")
    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    await fetch()
    await fetch()
    assert calls == 2


@pytest.mark.asyncio
async def test_expired_entry_refetches(monkeypatch):
    monkeypatch.setattr(config, "search_cache_ttl", 0.01)
    calls = 0

    @cache_mod.ttl_cached("t")
    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await fetch() == 1
    await asyncio.sleep(0.02)
    assert await fetch() == 2


@pytest.mark.asyncio
async def test_concurrent_callers_coalesce():
    """Ten simultaneous identical calls must produce exactly one fetch."""
    calls = 0
    release = asyncio.Event()

    @cache_mod.ttl_cached("t")
    async def fetch() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "done"

    tasks = [asyncio.create_task(fetch()) for _ in range(10)]
    await asyncio.sleep(0)  # let them all reach the inflight check
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == ["done"] * 10
    assert calls == 1


@pytest.mark.asyncio
async def test_failure_is_not_cached_and_propagates():
    calls = 0

    @cache_mod.ttl_cached("t")
    async def fetch() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await fetch()
    with pytest.raises(RuntimeError):
        await fetch()
    assert calls == 2  # a failed fetch must not poison the cache


@pytest.mark.asyncio
async def test_failure_propagates_to_coalesced_waiters():
    started = asyncio.Event()
    release = asyncio.Event()

    @cache_mod.ttl_cached("t")
    async def fetch() -> str:
        started.set()
        await release.wait()
        raise RuntimeError("boom")

    first = asyncio.create_task(fetch())
    await started.wait()
    second = asyncio.create_task(fetch())
    await asyncio.sleep(0)
    release.set()

    for task in (first, second):
        with pytest.raises(RuntimeError):
            await task


@pytest.mark.asyncio
async def test_unhashable_argument_bypasses_cache():
    calls = 0

    @cache_mod.ttl_cached("t")
    async def fetch(payload) -> int:
        nonlocal calls
        calls += 1
        return calls

    await fetch(["a", "list"])
    await fetch(["a", "list"])
    assert calls == 2


@pytest.mark.asyncio
async def test_maxsize_evicts_oldest(monkeypatch):
    monkeypatch.setattr(config, "search_cache_maxsize", 2)
    calls = 0

    @cache_mod.ttl_cached("t")
    async def fetch(q: str) -> str:
        nonlocal calls
        calls += 1
        return q

    await fetch("a")
    await fetch("b")
    await fetch("c")  # evicts "a"
    assert calls == 3
    await fetch("a")   # re-fetched
    assert calls == 4
    await fetch("c")   # still resident
    assert calls == 4
