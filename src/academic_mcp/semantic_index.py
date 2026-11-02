"""Semantic index over Zotero metadata using ChromaDB.

The embedding backend is pluggable via :mod:`embeddings` — local
sentence-transformers by default, with optional OpenAI or Gemini
providers. Regardless of provider, the vector store is local (Chroma)
and ANN search runs on the user's machine.

Cross-provider safety: each upsert records ``provider``/``model``/``dim``
in Chroma metadata, and the collection-level status stores the same. A
sync or search that disagrees with the collection's provider triple is
refused with an actionable error — mixing vector spaces silently would
break cosine similarity.

This module is optional at runtime: if chromadb is not installed, semantic
tools return actionable guidance instead of crashing core search flows.

Sync semantics:
  * **Incremental by default.** Each upsert records the item's ``dateModified``
    in Chroma metadata. A subsequent ``sync()`` only re-embeds items whose
    ``dateModified`` is newer than the stored value or is missing entirely.
  * **Deletion detection.** Item keys present in Chroma but absent from the
    current Zotero scan are removed.
  * **Chunk-level storage.** Each item is split into one or more overlapping
    text windows by :mod:`chunking`. Chunk IDs have the form ``item_key:N``
    where N is a zero-based integer. Each chunk stores ``char_start``,
    ``char_end``, and ``chunk_source`` metadata so callers can pass exact
    byte ranges to ``fetch_fulltext(mode="range")``.
  * **Migration guard.** If the stored collection lacks ``chunk_idx`` metadata
    (old single-vector format), :meth:`sync` forces a rebuild automatically.
  * **include_fulltext** is accepted for backward compatibility but is a
    silent no-op — chunking already reads the ft-cache unconditionally.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import os
import threading
import fcntl
import logging
import warnings
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import zotero_sqlite
from .semantic_state import IndexState, text_hash, metadata_json
from .chunking import chunk_item
from .config import config
from .embeddings import EmbedMode, Embedder, EmbedderUnavailable, resolve_embedder

logger = logging.getLogger(__name__)

# Embedded Chroma clients must not share their HNSW files across processes.
# Retain both the client and an OS ownership lock for this process lifetime.
_chroma_clients = {}
_chroma_clients_lock = threading.Lock()


class SemanticIndexUnavailable(RuntimeError):
    pass


# Tiny LRU for interactive query embeddings.  Both search() and
# search_within_item() pay an embedder round-trip on the same query string;
# this cache lets a follow-up call (e.g. opening an article from search results)
# reuse the just-computed embedding without another network hop.
_QUERY_EMBED_CACHE_MAX = 64
_query_embed_cache: "OrderedDict[tuple[str, str], Any]" = OrderedDict()


def _embed_query_cached(embedder: "Embedder", query: str) -> Any:
    key = (f"{embedder.provider}:{embedder.model}", query)
    if key in _query_embed_cache:
        _query_embed_cache.move_to_end(key)
        return _query_embed_cache[key]
    emb = embedder.encode_query([query])[0]
    _query_embed_cache[key] = emb
    if len(_query_embed_cache) > _QUERY_EMBED_CACHE_MAX:
        _query_embed_cache.popitem(last=False)
    return emb


# Separator between item_key and chunk index in composite Chroma IDs.
_CHUNK_ID_SEP = ":"

# How many chunks to embed + upsert per streamed batch.
# Must be >= the embedder's per-HTTP-request batch size so we don't
# issue more round-trips than necessary, and small enough that a
# crash during the run loses at most this many chunks' worth of work.
# Read from env to allow tuning without code changes.
_DEFAULT_UPSERT_BATCH = 64


def _get_upsert_batch_size() -> int:
    import os

    try:
        v = int(os.getenv("SEMANTIC_UPSERT_BATCH", str(_DEFAULT_UPSERT_BATCH)))
        return max(8, v)
    except ValueError:
        return _DEFAULT_UPSERT_BATCH


# Chroma 1.x's Rust executor binds multiple SQL params per id internally
# (id + segment refs), so the practical safe batch is well below SQLite's
# 999-variable cap. 100 leaves headroom even on older bundled sqlite builds.
_DELETE_CHUNK_SIZE = 100


def _chroma_delete_in_chunks(col: Any, ids: list[str]) -> None:
    if not ids:
        return
    for i in range(0, len(ids), _DELETE_CHUNK_SIZE):
        col.delete(ids=ids[i : i + _DELETE_CHUNK_SIZE])


def _make_chunk_id(item_key: str, idx: int) -> str:
    return f"{item_key}{_CHUNK_ID_SEP}{idx}"


def _item_key_from_chunk_id(chunk_id: str) -> str:
    """Return the item_key portion of a composite chunk ID."""
    return chunk_id.rsplit(_CHUNK_ID_SEP, 1)[0]


class SemanticIndex:
    def __init__(self) -> None:
        import os

        env_dir = os.getenv("SEMANTIC_CACHE_DIR")
        if env_dir:
            self.cache_dir = Path(env_dir).expanduser()
        else:
            self.cache_dir = Path.home() / ".cache" / "academic-mcp" / "chroma"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.cache_dir / "status.json"
        self.collection_name = "zotero_items"
        # Active embedders are resolved lazily via _get_embedder() so missing
        # cloud keys don't blow up at import time.  Cached per-mode because
        # bulk and interactive may resolve to different endpoints (same
        # provider/model — see config.bulk_openai_*).
        self._embedders: dict[EmbedMode, Embedder] = {}
        # Set when we have already warned about cross-endpoint mixing so
        # the warning fires at most once per process.
        self._mix_warned: bool = False
        self._writer_lock = threading.Lock()

    # -- status file -----------------------------------------------------

    def _load_status(self) -> dict[str, Any]:
        if self.status_path.exists():
            try:
                return json.loads(self.status_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_status(self, status: dict[str, Any]) -> None:
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.status_path)

    # -- chroma / model --------------------------------------------------

    def _get_chroma_collection(self):
        with _chroma_clients_lock:
            return self._get_chroma_collection_owned()

    def _get_chroma_collection_owned(self):
        path = str(self.cache_dir.resolve())
        cached = _chroma_clients.get(path)
        if cached is not None and cached[0] != os.getpid():
            raise SemanticIndexUnavailable(
                "An embedded Chroma client cannot be inherited across fork; use the MCP server"
            )
        if cached is None:
            owner = (self.cache_dir / "client.lock").open("a")
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner.close()
                raise SemanticIndexUnavailable(
                    "The semantic index is owned by another process. Use the running server's MCP tools."
                ) from None
            try:
                import chromadb

                client = chromadb.PersistentClient(path=path)
            except ImportError as exc:
                owner.close()
                raise SemanticIndexUnavailable(
                    "semantic index requires chromadb; install academic-mcp-server[semantic]"
                ) from exc
            except BaseException:
                owner.close()
                raise
            cached = (os.getpid(), owner, client)
            _chroma_clients[path] = cached
        return cached[2].get_or_create_collection(name=self.collection_name)

    def _get_embedder(
        self,
        provider: str | None = None,
        model: str | None = None,
        mode: EmbedMode = "interactive",
    ) -> Embedder:
        """Resolve and cache the active embedder for *mode*.

        Explicit provider/model args override configuration for this call.
        Changing provider or model invalidates the cached instance for that
        mode so the new one loads on the next encode.

        Both modes must produce vectors in the same space — this is enforced
        by reading a single ``SEMANTIC_MODEL`` for both.  Endpoints differ
        only in transport (e.g. cloud bulk vs. local interactive), not in
        which model they evaluate.
        """
        cached = self._embedders.get(mode)
        if (
            cached is not None
            and (provider is None or cached.provider == provider.lower())
            and (model is None or cached.model == model)
        ):
            return cached
        try:
            embedder = resolve_embedder(provider=provider, model=model, mode=mode)
        except EmbedderUnavailable as e:
            raise SemanticIndexUnavailable(str(e)) from e
        self._embedders[mode] = embedder
        self._maybe_warn_endpoint_mix()
        return embedder

    def _maybe_warn_endpoint_mix(self) -> None:
        """One-shot warning when bulk and interactive resolve to different endpoints.

        Different endpoints serving the same model (e.g. DeepInfra-hosted
        bge-large for bulk + a local llama.cpp serving the same GGUF for
        interactive) produce vectors with cosine similarity ~0.999 but not
        bit-identical.  In practice this never affects top-k retrieval, but
        users should know.
        """
        if self._mix_warned:
            return
        bulk = self._embedders.get("bulk")
        interactive = self._embedders.get("interactive")
        if bulk and interactive and bulk.endpoint and interactive.endpoint:
            if bulk.endpoint != interactive.endpoint:
                logger.info(
                    "semantic_index: bulk endpoint (%s) differs from interactive endpoint (%s); "
                    "vector cosine drift between the two is ~0.001 in practice and does not "
                    "affect top-k retrieval, but exact-equality assumptions are off.",
                    bulk.endpoint,
                    interactive.endpoint,
                )
                self._mix_warned = True

    def _embed(self, texts: list[str]) -> list[list[float]]:
        # Default to interactive mode; sync() bypasses this and calls
        # embedder.encode() directly with mode="bulk" via _get_embedder.
        return self._get_embedder().encode(texts)

    @property
    def model_name(self) -> str:
        # Backwards-compat surface for tests and status output.
        # Prefer the interactive embedder (queries are the canonical
        # consumer of this name); fall back to bulk; then status; then the
        # legacy default.
        for mode in ("interactive", "bulk"):
            emb = self._embedders.get(mode)  # type: ignore[arg-type]
            if emb is not None:
                return emb.model
        status = self._load_status()
        return status.get("model") or "all-MiniLM-L6-v2"

    # -- provider-consistency guard --------------------------------------

    def _assert_compatible(self, embedder: Embedder) -> None:
        """Refuse to mix vectors from different providers/models.

        The collection's provider triple is recorded in status.json on each
        successful sync. A mismatch is surfaced with a clear fix, not a
        silent corruption.
        """
        status = self._load_status()
        stored_provider = status.get("provider")
        stored_model = status.get("model")
        if (
            stored_provider
            and stored_model
            and (stored_provider != embedder.provider or stored_model != embedder.model)
        ):
            raise SemanticIndexUnavailable(
                f"Index was built with {stored_provider}/{stored_model} but "
                f"current provider is {embedder.provider}/{embedder.model}. "
                "Run semantic_index_rebuild(force=True) to rebuild with the "
                "new provider (vectors from different models are not "
                "comparable)."
            )

    # -- migration guard -------------------------------------------------

    @staticmethod
    def _needs_migration(existing: dict) -> bool:
        """Return True if the collection uses an outdated format that requires rebuild.

        Format 1: stores ``chunk_idx`` but lacks the context-header enrichment
                  (``text_format`` key absent or < 2).
        Format 0: the old single-vector format (no ``chunk_idx`` at all).
        """
        metas = existing.get("metadatas") or []
        for md in metas:
            if md is not None:
                if "chunk_idx" not in md:
                    return True  # format 0: old single-vector layout
                if int(md.get("text_format") or 1) < 2:
                    return True  # format 1: pre-context-header layout
        return False  # empty collection — no migration required

    # -- sync ------------------------------------------------------------

    async def sync(
        self,
        force_rebuild: bool = False,
        include_fulltext: bool = False,  # no-op; kept for backward compat
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if include_fulltext:
            warnings.warn(
                "include_fulltext is deprecated and has no effect. "
                "Chunking reads ft-cache unconditionally.",
                DeprecationWarning,
                stacklevel=2,
            )

        from .zotero_catalog import enabled as catalog_enabled

        if not zotero_sqlite.sqlite_config.available and not catalog_enabled():
            raise SemanticIndexUnavailable(
                "Neither Zotero SQLite nor Web API metadata is available"
            )

        embedder = self._get_embedder(provider=provider, model=model, mode="bulk")
        if not force_rebuild:
            # Bail out before doing any work if an existing index disagrees.
            self._assert_compatible(embedder)

        try:
            items = await zotero_sqlite.list_items_for_semantic_index()
            from .zotero_fulltext import hydrate

            await hydrate(items)
        except Exception as exc:
            self._save_status(
                {
                    **self._load_status(),
                    "in_progress": False,
                    "phase": "source_error",
                    "last_error": str(exc),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            raise

        def _sync_blocking():
            with self._writer_lock, (self.cache_dir / "writer.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self._sync_items(items, embedder, force_rebuild=force_rebuild)

        return await asyncio.to_thread(_sync_blocking)

    def _source_signature(self, item):
        # Source identity includes the content files independently of Zotero's
        # metadata timestamp. rsync preserves mtimes, and atomic cache rewrites
        # change their nanosecond mtime/inode even if text length stays equal.
        from . import text_cache, chunking

        paths = []
        doi = item.get("doi") or (
            "zotero:" + item["item_key"] if item.get("item_key") else ""
        )
        if doi:
            paths.append(text_cache._cache_path(doi))
        attachment = item.get("attachment_key") or ""
        if attachment:
            storage = getattr(zotero_sqlite.sqlite_config, "storage_path", "")
            if storage:
                paths.append(Path(storage) / attachment / ".zotero-ft-cache")
        files = []
        for path in paths:
            try:
                stat = path.stat()
                files.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino))
            except FileNotFoundError:
                files.append((str(path), None))
        payload = {
            "item": item,
            "files": files,
            "format": 2,
            "chunk_chars": chunking._CHUNK_CHARS,
            "overlap": chunking._OVERLAP_CHARS,
            "cap": chunking._MAX_FT_CHARS,
        }
        return text_hash(metadata_json(payload))

    def _sync_items(
        self,
        items,
        embedder,
        *,
        force_rebuild=False,
        remove_stale=True,
        publish_status=True,
    ):
        col = self._get_chroma_collection()
        ledger = IndexState(self.cache_dir / "index-state.sqlite")
        previous = self._load_status()
        started_at = datetime.now(timezone.utc).isoformat()
        upserted = 0
        reused = 0
        deleted = 0
        items_indexed = 0
        total_items_to_index = 0
        pending = {}
        ids_buf = []
        docs_buf = []
        metas_buf = []

        def status(done, error=None):
            if not publish_status:
                return
            now = datetime.now(timezone.utc).isoformat()
            snapshot = {
                "last_sync": now if done else previous.get("last_sync"),
                "updated_at": now,
                "started_at": started_at,
                "count": col.count(),
                "provider": embedder.provider,
                "model": embedder.model,
                "dim": embedder.dim or previous.get("dim"),
                "sqlite_items_seen": len(items),
                "items_to_index": total_items_to_index,
                "items_indexed": items_indexed,
                "upserted": upserted,
                "reused": reused,
                "pending": max(0, total_items_to_index - items_indexed),
                "deleted": deleted,
                "in_progress": not done,
                "phase": "complete" if done else "indexing",
            }
            if error:
                snapshot["last_error"] = error
            self._save_status(snapshot)

        def finish_ready():
            nonlocal items_indexed, deleted
            for key in list(pending):
                job = pending[key]
                if job["remaining"] or not job["assembled"]:
                    continue
                tail = job["tail"]
                if tail:
                    _chroma_delete_in_chunks(col, tail)
                    ledger.delete(tail)
                    deleted += len(tail)
                ledger.finish(key)
                items_indexed += 1
                del pending[key]
                if items_indexed % 100 == 0:
                    status(False)

        def flush():
            nonlocal ids_buf, docs_buf, metas_buf, upserted
            if not ids_buf:
                return
            import time

            for attempt in range(3):
                try:
                    vectors = embedder.encode(docs_buf)
                    if len(vectors) != len(docs_buf):
                        raise SemanticIndexUnavailable(
                            "Embedding response omitted document vectors"
                        )
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(5)
            col.upsert(
                ids=ids_buf, documents=docs_buf, embeddings=vectors, metadatas=metas_buf
            )
            with ledger.db:
                for cid, doc, md in zip(ids_buf, docs_buf, metas_buf):
                    ledger.put(cid, doc, md)
                    pending[md["item_key"]]["remaining"].discard(cid)
            upserted += len(ids_buf)
            ids_buf = []
            docs_buf = []
            metas_buf = []
            finish_ready()
            status(False)
            logger.info(
                "semantic_index: upserted %d chunks; reused %d; completed %d/%d items",
                upserted,
                reused,
                items_indexed,
                total_items_to_index,
            )

        try:
            # Status includes scanning time, rather than looking idle for the
            # several minutes required to adopt a large pre-existing index.
            if publish_status:
                self._save_status(
                    {
                        **previous,
                        "in_progress": True,
                        "phase": "scanning",
                        "started_at": started_at,
                        "updated_at": started_at,
                    }
                )

            def progress(scanned, total):
                if publish_status:
                    self._save_status(
                        {
                            **previous,
                            "in_progress": True,
                            "phase": "adopting_existing_vectors",
                            "started_at": started_at,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "chunks_scanned": scanned,
                            "existing_chunks": total,
                        }
                    )

            ledger.bootstrap(col, progress)
            if force_rebuild:
                for key in ledger.keys():
                    ids = list(ledger.chunks(key))
                    _chroma_delete_in_chunks(col, ids)
                    ledger.delete(ids)
                    deleted += len(ids)
                with ledger.db:
                    ledger.db.execute("DELETE FROM items")
            seen = {item["item_key"] for item in items if item.get("item_key")}
            if remove_stale:
                for key in ledger.keys() - seen:
                    ids = list(ledger.chunks(key))
                    _chroma_delete_in_chunks(col, ids)
                    ledger.delete(ids)
                    deleted += len(ids)
                    with ledger.db:
                        ledger.db.execute("DELETE FROM items WHERE item_key=?", (key,))
            work = []
            for item in items:
                key = item.get("item_key")
                if not key:
                    continue
                signature = self._source_signature(item)
                old = ledger.item(key)
                if old and old["complete"] and old["signature"] == signature:
                    continue
                work.append((item, signature))
            total_items_to_index = len(work)
            status(False)
            for item, signature in work:
                key = item["item_key"]
                chunks = chunk_item(item)
                old = ledger.chunks(key)
                expected = {_make_chunk_id(key, i) for i in range(len(chunks))}
                ledger.start(key, signature)
                pending[key] = {
                    "remaining": set(expected),
                    "tail": list(set(old) - expected),
                    "assembled": False,
                }
                metadata_updates = []
                for number, chunk in enumerate(chunks):
                    cid = _make_chunk_id(key, number)
                    md = {
                        "item_key": key,
                        "doi": (item.get("doi") or "").strip(),
                        "title": (item.get("title") or "")[:400],
                        "dateModified": item.get("dateModified") or "",
                        "chunk_idx": number,
                        "chunk_count": len(chunks),
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "chunk_source": chunk.source,
                        "provider": embedder.provider,
                        "model": embedder.model,
                        "text_format": 2,
                    }
                    prior = old.get(cid)
                    prior_md = json.loads(prior["metadata"]) if prior else {}
                    same_space = (
                        prior_md.get("provider", embedder.provider) == embedder.provider
                        and prior_md.get("model", embedder.model) == embedder.model
                    )
                    if (
                        prior
                        and same_space
                        and prior["doc_hash"] == text_hash(chunk.text)
                    ):
                        reused += 1
                        pending[key]["remaining"].discard(cid)
                        if prior["metadata"] != metadata_json(md):
                            metadata_updates.append((cid, chunk.text, md))
                    else:
                        ids_buf.append(cid)
                        docs_buf.append(chunk.text)
                        metas_buf.append(md)
                    if len(ids_buf) >= _get_upsert_batch_size():
                        flush()
                for start in range(0, len(metadata_updates), 100):
                    batch = metadata_updates[start : start + 100]
                    col.update(
                        ids=[r[0] for r in batch], metadatas=[r[2] for r in batch]
                    )
                    with ledger.db:
                        for cid, doc, md in batch:
                            ledger.put(cid, doc, md)
                pending[key]["assembled"] = True
                finish_ready()
            flush()
            finish_ready()
            status(True)
            return (
                self._load_status()
                if publish_status
                else {"upserted": upserted, "reused": reused}
            )
        except Exception as exc:
            if publish_status:
                try:
                    status(False, str(exc))
                except Exception:
                    logger.exception("Could not persist failed semantic sync status")
            raise
        finally:
            ledger.close()

    # -- search ----------------------------------------------------------

    async def search(self, query: str, k: int = 10) -> list[dict[str, Any]]:
        """Return chunk-level search results for *query*.

        Each result dict includes:
          item_key, doi, title, score, snippet, char_start, char_end,
          chunk_source, chunk_idx, chunk_count.
        Callers (e.g. the server handler) can pass char_start/char_end to
        ``fetch_fulltext(mode="range", ...)`` for precise passage retrieval.
        """
        embedder = self._get_embedder(mode="interactive")
        self._assert_compatible(embedder)

        def _search_blocking() -> list[dict[str, Any]]:
            col = self._get_chroma_collection()
            emb = _embed_query_cached(embedder, query)
            res = col.query(query_embeddings=[emb], n_results=max(1, min(k, 200)))

            ids = (res.get("ids") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]

            out: list[dict[str, Any]] = []
            for idx, chunk_id in enumerate(ids):
                md = metas[idx] if idx < len(metas) else {}
                doc = docs[idx] if idx < len(docs) else ""
                dist = dists[idx] if idx < len(dists) else 1.0
                score = 1.0 - float(dist)
                out.append(
                    {
                        "item_key": md.get("item_key")
                        or _item_key_from_chunk_id(chunk_id),
                        "chunk_id": chunk_id,
                        "doi": md.get("doi") or "",
                        "title": md.get("title") or "",
                        "score": round(score, 4),
                        "snippet": doc[:300],
                        "char_start": int(md.get("char_start") or 0),
                        "char_end": int(md.get("char_end") or 0),
                        "chunk_source": md.get("chunk_source") or "unknown",
                        "chunk_idx": int(md.get("chunk_idx") or 0),
                        "chunk_count": int(md.get("chunk_count") or 1),
                    }
                )
            return out

        return await asyncio.to_thread(_search_blocking)

    async def search_within_item(
        self,
        query: str,
        item_key: str,
        k: int = 20,
    ) -> list[dict[str, Any]]:
        """Return chunks for *item_key* ranked by relevance to *query*.

        Same shape as :meth:`search`, but restricted to a single Zotero item.
        Used by the webapp's per-article semantic heatmap.
        """
        if not item_key:
            return []
        embedder = self._get_embedder(mode="interactive")
        self._assert_compatible(embedder)

        def _search_blocking() -> list[dict[str, Any]]:
            col = self._get_chroma_collection()
            emb = _embed_query_cached(embedder, query)
            res = col.query(
                query_embeddings=[emb],
                n_results=max(1, min(k, 200)),
                where={"item_key": item_key},
            )
            ids = (res.get("ids") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            out: list[dict[str, Any]] = []
            for idx, chunk_id in enumerate(ids):
                md = metas[idx] if idx < len(metas) else {}
                doc = docs[idx] if idx < len(docs) else ""
                dist = dists[idx] if idx < len(dists) else 1.0
                score = 1.0 - float(dist)
                out.append(
                    {
                        "item_key": md.get("item_key")
                        or _item_key_from_chunk_id(chunk_id),
                        "chunk_id": chunk_id,
                        "doi": md.get("doi") or "",
                        "title": md.get("title") or "",
                        "score": round(score, 4),
                        "snippet": doc[:600],
                        "char_start": int(md.get("char_start") or 0),
                        "char_end": int(md.get("char_end") or 0),
                        "chunk_source": md.get("chunk_source") or "unknown",
                        "chunk_idx": int(md.get("chunk_idx") or 0),
                        "chunk_count": int(md.get("chunk_count") or 1),
                    }
                )
            return out

        return await asyncio.to_thread(_search_blocking)

    # -- hot-path embed --------------------------------------------------

    async def embed_item_now(self, item_key: str) -> int:
        """Chunk and embed a single item immediately. Returns number of chunks written.

        Used as a hot-path fallback when the user looks up an item that hasn't
        been reached by the background sync yet. Safe to call concurrently with
        the background sync — Chroma serialises writers at the SQLite layer.
        """
        items = await zotero_sqlite.list_items_for_semantic_index()
        target = next((it for it in items if it.get("item_key") == item_key), None)
        if target is None:
            return 0

        chunks = chunk_item(target)
        if not chunks:
            return 0

        embedder = self._get_embedder(mode="interactive")
        self._assert_compatible(embedder)

        def _do() -> int:
            with self._writer_lock, (self.cache_dir / "writer.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = self._sync_items(
                    [target], embedder, remove_stale=False, publish_status=False
                )
                return result["upserted"]

        return await asyncio.to_thread(_do)

    # -- status ----------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        status = await asyncio.to_thread(self._load_status)

        def _count_indexed() -> int:
            col = self._get_chroma_collection()
            return col.count()

        try:
            indexed = await asyncio.to_thread(_count_indexed)
        except SemanticIndexUnavailable:
            raise
        except Exception:
            indexed = int(status.get("count") or 0)

        status.setdefault("provider", config.semantic_provider or "local")
        status.setdefault("model", self.model_name)
        status["indexed_count"] = indexed
        status["cache_dir"] = str(self.cache_dir)
        # Expose the currently configured provider for drift detection.
        status["configured_provider"] = config.semantic_provider or "local"
        status["configured_model"] = config.semantic_model or {
            "local": "all-MiniLM-L6-v2",
            "openai": "text-embedding-3-small",
            "gemini": "gemini-embedding-001",
        }.get(config.semantic_provider or "local", "all-MiniLM-L6-v2")
        from . import zotero_fulltext

        status["fulltext_sync"] = zotero_fulltext.status()
        return status


_index_singleton: SemanticIndex | None = None


def get_semantic_index() -> SemanticIndex:
    global _index_singleton
    if _index_singleton is None:
        _index_singleton = SemanticIndex()
    return _index_singleton
