"""Tests for the semantic index — incremental sync and deletion.

Uses a fake Chroma collection (no chromadb import) and a stub SQLite listing,
so the sync logic can be exercised deterministically without ML deps.

Chunk-level design: each Zotero item is split by :mod:`chunking` into one or
more text windows. Chroma records use composite IDs of the form
``item_key:chunk_idx``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import semantic_index  # noqa: E402
from academic_mcp.semantic_index import _make_chunk_id, _item_key_from_chunk_id  # noqa: E402


# ---------------------------------------------------------------------------
# FakeCollection
# ---------------------------------------------------------------------------

class FakeCollection:
    """Minimal Chroma collection stand-in for sync/search tests."""

    def __init__(self) -> None:
        # chunk_id -> {doc, metadata}
        self._store: dict[str, dict] = {}
        # Each entry is a dict with the kwargs passed to upsert().
        self.upsert_calls: list[dict] = []

    def get(self, include=None):
        ids = list(self._store.keys())
        metas = [self._store[i]["metadata"] for i in ids]
        docs = [self._store[i]["doc"] for i in ids]
        return {"ids": ids, "documents": docs, "metadatas": metas}

    def upsert(self, ids, documents, embeddings, metadatas):
        self.upsert_calls.append({"ids": list(ids), "documents": list(documents), "metadatas": list(metadatas)})
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


# ---------------------------------------------------------------------------
# FakeEmbedder
# ---------------------------------------------------------------------------

class FakeEmbedder:
    provider = "local"
    model = "fake"
    dim = 4

    def encode(self, texts):
        return [[float(len(t) % 7)] * self.dim for t in texts]

    def encode_query(self, texts):
        return self.encode(texts)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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

    # Patch the embedder resolver so no ML libraries are loaded.
    fake_emb = FakeEmbedder()
    monkeypatch.setattr(idx, "_get_embedder", lambda *a, **kw: fake_emb)
    # Keep _embed in sync for any legacy callers.
    monkeypatch.setattr(idx, "_embed", lambda texts: fake_emb.encode(texts))

    # Prevent chunking from hitting the filesystem; items in these tests
    # have no attachment_key so chunk_item falls back to abstract-only path.
    return idx, col


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_item(key, title="", abstract="", doi="", date="2025-01-01"):
    return {
        "item_key": key,
        "title": title,
        "abstract": abstract,
        "doi": doi,
        "dateModified": date,
        "attachment_key": "",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_initial_sync_embeds_all_items(fake_index, monkeypatch):
    idx, col = fake_index

    items = [
        _make_item("A", title="Paper A", abstract="abs A"),
        _make_item("B", title="Paper B"),
        _make_item("C"),  # no title, no abstract — should be skipped
    ]

    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    status = await idx.sync()
    # C skipped (no title or abstract); A and B each produce 1 chunk.
    assert status["upserted"] == 2
    assert status["deleted"] == 0
    assert col.count() == 2

    # Verify composite chunk IDs.
    stored_ids = set(col._store.keys())
    assert "A:0" in stored_ids
    assert "B:0" in stored_ids


async def test_incremental_sync_skips_unchanged(fake_index, monkeypatch):
    idx, col = fake_index

    items_v1 = [
        _make_item("A", title="A", abstract="x"),
        _make_item("B", title="B", abstract="y"),
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
        _make_item("A", title="A", abstract="x", date="2025-02-01"),
        _make_item("B", title="B", abstract="y"),
    ]
    async def _list_v2(): return items_v2
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list_v2)
    status = await idx.sync()
    # 1 item changed; it has 1 abstract-only chunk.
    assert status["upserted"] == 1


async def test_deletion_removes_orphans(fake_index, monkeypatch):
    idx, col = fake_index

    items_full = [
        _make_item("A", title="A"),
        _make_item("B", title="B"),
    ]

    async def _list_full(): return items_full
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list_full)
    await idx.sync()
    assert col.count() == 2

    async def _list_a_only(): return [items_full[0]]
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list_a_only)
    status = await idx.sync()
    # B's 1 chunk should be deleted.
    assert status["deleted"] == 1
    assert col.count() == 1
    assert "B:0" not in col._store


async def test_force_rebuild_wipes_and_reembeds(fake_index, monkeypatch):
    idx, col = fake_index

    async def _list(): return [_make_item("A", title="A")]
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    await idx.sync()
    assert col.count() == 1

    status = await idx.sync(force_rebuild=True)
    assert status["upserted"] == 1  # re-embedded after wipe
    assert col.count() == 1


async def test_include_fulltext_emits_deprecation_warning(fake_index, monkeypatch):
    idx, col = fake_index

    async def _list(): return [_make_item("A", title="A")]
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    with pytest.warns(DeprecationWarning, match="include_fulltext is deprecated"):
        await idx.sync(include_fulltext=True)


async def test_migration_guard_triggers_rebuild(fake_index, monkeypatch):
    """If the collection has old single-vector records (no chunk_idx), rebuild."""
    idx, col = fake_index

    # Pre-populate with old-style records (no chunk_idx in metadata).
    col._store["A"] = {"doc": "old doc", "metadata": {"item_key": "A", "dateModified": "2025-01-01"}}
    col._store["B"] = {"doc": "old doc b", "metadata": {"item_key": "B", "dateModified": "2025-01-01"}}

    items = [
        _make_item("A", title="A"),
        _make_item("B", title="B"),
    ]
    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    # Sync should detect the old format, wipe, and re-embed.
    status = await idx.sync()
    assert status["upserted"] == 2

    # Old-style bare IDs should be gone; new composite IDs present.
    assert "A" not in col._store
    assert "B" not in col._store
    assert "A:0" in col._store
    assert "B:0" in col._store


async def test_migration_guard_triggers_rebuild_for_format1(fake_index, monkeypatch):
    """If the collection has text_format=1 records (pre-context-header), rebuild."""
    idx, col = fake_index

    # Pre-populate with format-1 records (chunk_idx present but text_format absent/1).
    col._store["A:0"] = {
        "doc": "old text",
        "metadata": {"item_key": "A", "chunk_idx": 0, "dateModified": "2025-01-01"},
    }

    items = [_make_item("A", title="A")]
    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    status = await idx.sync()
    assert status["upserted"] == 1

    # The new record should carry text_format=2.
    meta = col._store["A:0"]["metadata"]
    assert meta.get("text_format") == 2


async def test_chunk_metadata_contains_char_offsets(fake_index, monkeypatch):
    """Chunk metadata must include char_start, char_end, chunk_idx, chunk_source."""
    idx, col = fake_index

    items = [_make_item("A", title="Title", abstract="Abstract text")]
    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    await idx.sync()

    md = col._store["A:0"]["metadata"]
    assert "char_start" in md
    assert "char_end" in md
    assert "chunk_idx" in md
    assert "chunk_source" in md
    assert md["chunk_idx"] == 0
    assert md["chunk_source"] == "abstract"


def test_cache_dir_honours_env(monkeypatch, tmp_path):
    """SEMANTIC_CACHE_DIR env var must override the default cache dir."""
    target = tmp_path / "custom-chroma"
    monkeypatch.setenv("SEMANTIC_CACHE_DIR", str(target))
    idx = semantic_index.SemanticIndex()
    assert idx.cache_dir == target
    assert target.is_dir()


def test_cache_dir_default_when_env_missing(monkeypatch):
    """When the env var is unset, fall back to ~/.cache/academic-mcp/chroma."""
    monkeypatch.delenv("SEMANTIC_CACHE_DIR", raising=False)
    idx = semantic_index.SemanticIndex()
    assert idx.cache_dir == Path.home() / ".cache" / "academic-mcp" / "chroma"


async def test_search_returns_chunk_level_fields(fake_index, monkeypatch):
    """search() results should include chunk-level fields."""
    idx, col = fake_index

    items = [_make_item("A", title="Attention is all you need", abstract="Transformer architecture.")]
    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    # Disable compatibility guard so we can search without a prior sync status.
    monkeypatch.setattr(idx, "_assert_compatible", lambda e: None)

    await idx.sync()
    results = await idx.search("transformer model", k=5)

    assert len(results) >= 1
    r = results[0]
    assert "item_key" in r
    assert "char_start" in r
    assert "char_end" in r
    assert "chunk_idx" in r
    assert "chunk_source" in r
    assert "score" in r
    assert "snippet" in r


# ---------------------------------------------------------------------------
# Unit: composite ID helpers
# ---------------------------------------------------------------------------

def test_make_chunk_id():
    assert _make_chunk_id("ABCD1234", 0) == "ABCD1234:0"
    assert _make_chunk_id("ABCD1234", 7) == "ABCD1234:7"


def test_item_key_from_chunk_id():
    assert _item_key_from_chunk_id("ABCD1234:0") == "ABCD1234"
    assert _item_key_from_chunk_id("ABCD1234:7") == "ABCD1234"
    # Keys themselves should not contain the separator.
    assert _item_key_from_chunk_id("COMPLEX:KEY:0") == "COMPLEX:KEY"


# ---------------------------------------------------------------------------
# Streaming upsert tests
# ---------------------------------------------------------------------------

async def test_sync_streams_upserts_incrementally(fake_index, monkeypatch):
    """sync() should call col.upsert in multiple small batches, not one big call."""
    idx, col = fake_index

    # 50 items, each with a title+abstract → each produces 1 chunk → 50 chunks total.
    # With batch_size=8 we expect ceil(50/8) = 7 upsert calls.
    items = [
        _make_item(f"K{i:03d}", title=f"Paper {i}", abstract=f"Abstract for paper {i}")
        for i in range(50)
    ]

    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)
    monkeypatch.setenv("SEMANTIC_UPSERT_BATCH", "8")

    # Track _save_status calls via a wrapper.
    status_writes: list[dict] = []
    _orig_save = idx._save_status

    def _capturing_save(s):
        status_writes.append(dict(s))
        _orig_save(s)

    monkeypatch.setattr(idx, "_save_status", _capturing_save)

    status = await idx.sync()

    # Each upsert call must contain ≤ 8 ids.
    assert len(col.upsert_calls) >= 7
    for call in col.upsert_calls:
        assert len(call["ids"]) <= 8

    # status_writes: 1 initial (in_progress=True) + N batch writes + 1 final (in_progress=False)
    # Total = len(upsert_calls) + 2
    assert len(status_writes) == len(col.upsert_calls) + 2

    # Final status must reflect all chunks upserted.
    assert status["upserted"] == 50
    assert status["in_progress"] is False
    assert status["pending"] == 0


async def test_sync_persists_partial_state_on_mid_batch_crash(fake_index, monkeypatch):
    """If upsert raises on the 3rd call, partial progress must be persisted."""
    idx, col = fake_index

    # 100 items → 100 chunks; with batch_size=32: batches [0:32],[32:64],[64:96],[96:100].
    # The 3rd call raises → 2 successful batches × 32 = 64 upserted chunks.
    items = [
        _make_item(f"K{i:03d}", title=f"Paper {i}", abstract=f"Abstract {i}")
        for i in range(100)
    ]

    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)
    monkeypatch.setenv("SEMANTIC_UPSERT_BATCH", "32")

    call_count = 0
    _orig_upsert = col.upsert

    def _failing_upsert(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise RuntimeError("simulated upsert failure")
        _orig_upsert(**kwargs)

    monkeypatch.setattr(col, "upsert", _failing_upsert)

    with pytest.raises(RuntimeError, match="simulated upsert failure"):
        await idx.sync()

    # First two calls succeeded, third raised.
    assert call_count == 3
    assert len(col.upsert_calls) == 2  # only 2 recorded via _orig_upsert

    # status.json must exist and reflect partial progress.
    assert idx.status_path.exists()
    import json
    saved = json.loads(idx.status_path.read_text())
    assert saved["upserted"] == 64  # 2 batches × 32
    assert saved["in_progress"] is True


async def test_env_override_batch_size_respected(fake_index, monkeypatch):
    """SEMANTIC_UPSERT_BATCH env var must cap each upsert call's batch size."""
    idx, col = fake_index

    items = [
        _make_item(f"Z{i:02d}", title=f"Title {i}", abstract=f"Abstract {i}")
        for i in range(30)
    ]

    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)
    monkeypatch.setenv("SEMANTIC_UPSERT_BATCH", "8")

    await idx.sync()

    for call in col.upsert_calls:
        assert len(call["ids"]) <= 8


async def test_sync_writes_text_format_2_in_metadata(fake_index, monkeypatch):
    """Each upserted chunk must carry text_format=2 so future migrations detect stale data."""
    idx, col = fake_index

    items = [_make_item("K1", title="Some Paper", abstract="Some abstract.")]
    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    await idx.sync()

    assert "K1:0" in col._store
    meta = col._store["K1:0"]["metadata"]
    assert meta.get("text_format") == 2


# ---------------------------------------------------------------------------
# Bulk vs. interactive embedder dispatch
# ---------------------------------------------------------------------------

async def test_sync_uses_bulk_mode_search_uses_interactive(tmp_path, monkeypatch):
    """sync() must request a 'bulk' embedder; search() must request 'interactive'.

    Validates the wiring that lets a user point bulk at cloud and
    interactive at local llama-server in the same process.
    """
    from types import SimpleNamespace

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
    monkeypatch.setattr(idx, "_assert_compatible", lambda e: None)

    requested_modes: list[str] = []

    def _fake_resolve(*, provider=None, model=None, mode="interactive"):
        requested_modes.append(mode)
        # Return distinct embedders so we can also verify caching by mode.
        emb = FakeEmbedder()
        emb.mode = mode  # type: ignore[attr-defined]
        emb.endpoint = f"http://{mode}.test/v1/embeddings"  # type: ignore[attr-defined]
        return emb

    monkeypatch.setattr(semantic_index, "resolve_embedder", _fake_resolve)

    items = [_make_item("A", title="Paper A", abstract="abstract A")]
    async def _list(): return items
    monkeypatch.setattr(semantic_index.zotero_sqlite, "list_items_for_semantic_index", _list)

    await idx.sync()
    assert "bulk" in requested_modes

    requested_modes.clear()
    await idx.search("any query", k=3)
    assert requested_modes == ["interactive"]


async def test_get_embedder_caches_per_mode(tmp_path, monkeypatch):
    """Repeated _get_embedder calls with the same mode must hit the cache."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        semantic_index.zotero_sqlite,
        "sqlite_config",
        SimpleNamespace(available=True, storage_path=""),
    )

    idx = semantic_index.SemanticIndex()
    idx.cache_dir = tmp_path / "chroma"
    idx.cache_dir.mkdir(parents=True, exist_ok=True)
    idx.status_path = idx.cache_dir / "status.json"

    resolve_calls: list[str] = []

    def _fake_resolve(*, provider=None, model=None, mode="interactive"):
        resolve_calls.append(mode)
        emb = FakeEmbedder()
        emb.mode = mode  # type: ignore[attr-defined]
        emb.endpoint = ""  # type: ignore[attr-defined]
        return emb

    monkeypatch.setattr(semantic_index, "resolve_embedder", _fake_resolve)

    e1 = idx._get_embedder(mode="interactive")
    e2 = idx._get_embedder(mode="interactive")
    e3 = idx._get_embedder(mode="bulk")
    e4 = idx._get_embedder(mode="bulk")

    assert e1 is e2  # cache hit — same mode
    assert e3 is e4  # cache hit — same mode
    assert e1 is not e3  # distinct embedders for distinct modes
    # resolve_embedder called twice: once per mode.
    assert resolve_calls == ["interactive", "bulk"]
