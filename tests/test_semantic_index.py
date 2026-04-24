"""Smoke tests for the semantic index — incremental sync and deletion.

Uses a fake Chroma collection (no chromadb import) and a stub SQLite listing,
so the sync logic can be exercised deterministically without ML deps.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import semantic_index  # noqa: E402


class FakeCollection:
    def __init__(self) -> None:
        # item_key -> {doc, metadata}
        self._store: dict[str, dict] = {}

    def get(self, include=None):
        ids = list(self._store.keys())
        docs = [self._store[i]["doc"] for i in ids]
        metas = [self._store[i]["metadata"] for i in ids]
        return {"ids": ids, "documents": docs, "metadatas": metas}

    def upsert(self, ids, documents, embeddings, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self._store[i] = {"doc": d, "metadata": m}

    def delete(self, ids):
        for i in ids:
            self._store.pop(i, None)

    def count(self):
        return len(self._store)

    def query(self, query_embeddings, n_results):
        ids = list(self._store.keys())[:n_results]
        return {
            "ids": [ids],
            "documents": [[self._store[i]["doc"] for i in ids]],
            "metadatas": [[self._store[i]["metadata"] for i in ids]],
            "distances": [[0.1] * len(ids)],
        }


@pytest.fixture
def fake_index(tmp_path, monkeypatch):
    # sqlite_config.available is a property — swap the whole object for a stub.
    monkeypatch.setattr(
        semantic_index.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=""),
    )

    idx = semantic_index.SemanticIndex()
    idx.cache_dir = tmp_path / "chroma"
    idx.cache_dir.mkdir(parents=True, exist_ok=True)
    idx.status_path = idx.cache_dir / "status.json"

    col = FakeCollection()
    monkeypatch.setattr(idx, "_get_chroma_collection", lambda: col)

    # Embedding stub: deterministic fake vector, no ML load.
    monkeypatch.setattr(idx, "_embed", lambda texts: [[float(len(t) % 7)] * 4 for t in texts])

    return idx, col


async def test_initial_sync_embeds_all_items(fake_index, monkeypatch):
    idx, col = fake_index

    items = [
        {"item_key": "A", "title": "Paper A", "abstract": "abs A", "doi": "10.1/a", "dateModified": "2025-01-01", "attachment_key": ""},
        {"item_key": "B", "title": "Paper B", "abstract": "",      "doi": "10.1/b", "dateModified": "2025-01-02", "attachment_key": ""},
        {"item_key": "C", "title": "",        "abstract": "",      "doi": "",       "dateModified": "2025-01-03", "attachment_key": ""},
    ]

    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    status = await idx.sync()
    assert status["upserted"] == 2  # C skipped (no title or abstract)
    assert status["deleted"] == 0
    assert col.count() == 2


async def test_incremental_sync_skips_unchanged(fake_index, monkeypatch):
    idx, col = fake_index

    items_v1 = [
        {"item_key": "A", "title": "A", "abstract": "x", "doi": "", "dateModified": "2025-01-01", "attachment_key": ""},
        {"item_key": "B", "title": "B", "abstract": "y", "doi": "", "dateModified": "2025-01-01", "attachment_key": ""},
    ]

    async def _list_v1(): return items_v1
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list_v1)
    await idx.sync()

    # Nothing changed → zero upserts.
    status = await idx.sync()
    assert status["upserted"] == 0
    assert status["deleted"] == 0

    # Bump dateModified on A only.
    items_v2 = [
        {"item_key": "A", "title": "A", "abstract": "x", "doi": "", "dateModified": "2025-02-01", "attachment_key": ""},
        {"item_key": "B", "title": "B", "abstract": "y", "doi": "", "dateModified": "2025-01-01", "attachment_key": ""},
    ]
    async def _list_v2(): return items_v2
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list_v2)
    status = await idx.sync()
    assert status["upserted"] == 1


async def test_deletion_removes_orphans(fake_index, monkeypatch):
    idx, col = fake_index

    items_full = [
        {"item_key": "A", "title": "A", "abstract": "", "doi": "", "dateModified": "2025-01-01", "attachment_key": ""},
        {"item_key": "B", "title": "B", "abstract": "", "doi": "", "dateModified": "2025-01-01", "attachment_key": ""},
    ]

    async def _list_full(): return items_full
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list_full)
    await idx.sync()
    assert col.count() == 2

    async def _list_a_only(): return [items_full[0]]
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list_a_only)
    status = await idx.sync()
    assert status["deleted"] == 1
    assert col.count() == 1


async def test_fulltext_flip_triggers_reembed(fake_index, monkeypatch, tmp_path):
    idx, col = fake_index

    # Stub ft-cache reader so include_fulltext=True has something to append.
    monkeypatch.setattr(semantic_index.SemanticIndex, "_maybe_ft_cache", staticmethod(lambda k: "fulltext body for " + k))

    items = [
        {"item_key": "A", "title": "A", "abstract": "abs", "doi": "", "dateModified": "2025-01-01", "attachment_key": "ATT1"},
    ]

    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    await idx.sync(include_fulltext=False)
    doc_before = col._store["A"]["doc"]

    status = await idx.sync(include_fulltext=True)
    assert status["upserted"] == 1
    doc_after = col._store["A"]["doc"]
    assert "fulltext body" in doc_after
    assert doc_after != doc_before


async def test_force_rebuild_wipes_and_reembeds(fake_index, monkeypatch):
    idx, col = fake_index

    async def _list(): return [
        {"item_key": "A", "title": "A", "abstract": "", "doi": "", "dateModified": "2025-01-01", "attachment_key": ""},
    ]
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    await idx.sync()
    assert col.count() == 1

    status = await idx.sync(force_rebuild=True)
    assert status["upserted"] == 1  # re-embedded after wipe
    assert col.count() == 1
