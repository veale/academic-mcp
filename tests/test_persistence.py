"""Tests for http/persistence.py saved-search CRUD."""

import pytest

from academic_mcp.http.persistence import (
    delete_saved_search,
    init_db,
    list_saved_searches,
    save_search,
)


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBAPP_DB", str(tmp_path / "test_webapp.sqlite"))


@pytest.mark.asyncio
async def test_init_db_is_idempotent():
    await init_db()
    await init_db()  # second call must not raise


@pytest.mark.asyncio
async def test_save_and_list():
    await init_db()
    saved = await save_search("machine learning", {"semantic": True, "limit": 10})
    assert saved.id > 0
    assert saved.query == "machine learning"
    assert saved.params == {"semantic": True, "limit": 10}
    assert saved.created_at  # non-empty ISO string

    results = await list_saved_searches()
    assert len(results) == 1
    assert results[0].id == saved.id
    assert results[0].query == "machine learning"


@pytest.mark.asyncio
async def test_list_returns_newest_first():
    await init_db()
    a = await save_search("alpha")
    b = await save_search("beta")
    results = await list_saved_searches()
    assert results[0].id == b.id
    assert results[1].id == a.id


@pytest.mark.asyncio
async def test_delete_existing():
    await init_db()
    saved = await save_search("to delete")
    deleted = await delete_saved_search(saved.id)
    assert deleted is True
    results = await list_saved_searches()
    assert all(r.id != saved.id for r in results)


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false():
    await init_db()
    deleted = await delete_saved_search(99999)
    assert deleted is False


@pytest.mark.asyncio
async def test_save_without_params_defaults_to_empty_dict():
    await init_db()
    saved = await save_search("simple query")
    assert saved.params == {}
    results = await list_saved_searches()
    assert results[0].params == {}
