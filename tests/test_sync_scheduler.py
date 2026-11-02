import asyncio
import pytest
from academic_mcp import __main__ as main
from academic_mcp.core import background


async def test_nightly_checks_for_new_items_even_when_old_index_was_complete(
    monkeypatch,
):
    calls = []
    sleeps = 0
    monkeypatch.setenv("SEMANTIC_NIGHTLY_SYNC", "true")

    async def sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(
        background, "_ensure_semantic_background_sync", lambda **kw: calls.append(kw)
    )
    with pytest.raises(asyncio.CancelledError):
        await main._nightly_sync_loop()
    assert calls == [{"max_age_hours": 0}]
