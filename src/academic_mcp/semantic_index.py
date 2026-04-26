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
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import zotero_sqlite
from .chunking import chunk_item
from .config import config
from .embeddings import Embedder, EmbedderUnavailable, resolve_embedder

logger = logging.getLogger(__name__)


class SemanticIndexUnavailable(RuntimeError):
    pass


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
        # Active embedder is resolved lazily via _get_embedder() so missing
        # cloud keys don't blow up at import time.
        self._embedder: Embedder | None = None

    # -- status file -----------------------------------------------------

    def _load_status(self) -> dict[str, Any]:
        if self.status_path.exists():
            try:
                return json.loads(self.status_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_status(self, status: dict[str, Any]) -> None:
        self.status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

    # -- chroma / model --------------------------------------------------

    def _get_chroma_collection(self):
        try:
            import chromadb
        except Exception as e:
            raise SemanticIndexUnavailable(
                "semantic index requires chromadb. Install with: "
                "pip install 'academic-mcp-server[semantic]'"
            ) from e

        client = chromadb.PersistentClient(path=str(self.cache_dir))
        return client.get_or_create_collection(name=self.collection_name)

    def _get_embedder(self, provider: str | None = None, model: str | None = None) -> Embedder:
        """Resolve and cache the active embedder.

        Explicit provider/model args override configuration for this call.
        Changing provider or model invalidates the cached instance so the
        new one loads on the next encode.
        """
        if (
            self._embedder is not None
            and (provider is None or self._embedder.provider == provider.lower())
            and (model is None or self._embedder.model == model)
        ):
            return self._embedder
        try:
            self._embedder = resolve_embedder(provider=provider, model=model)
        except EmbedderUnavailable as e:
            raise SemanticIndexUnavailable(str(e)) from e
        return self._embedder

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._get_embedder().encode(texts)

    @property
    def model_name(self) -> str:
        # Backwards-compat surface for tests and status output.
        if self._embedder is not None:
            return self._embedder.model
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
        if stored_provider and stored_model and (
            stored_provider != embedder.provider or stored_model != embedder.model
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

        if not zotero_sqlite.sqlite_config.available:
            raise SemanticIndexUnavailable("Zotero SQLite backend is not available")

        embedder = self._get_embedder(provider=provider, model=model)
        if not force_rebuild:
            # Bail out before doing any work if an existing index disagrees.
            self._assert_compatible(embedder)

        items = await zotero_sqlite.list_items_for_semantic_index()

        def _sync_blocking() -> dict[str, Any]:
            col = self._get_chroma_collection()

            # Snapshot of what's currently indexed.
            #   prior_items[item_key] = {"dateModified": str, "chunk_ids": [str, ...]}
            existing = col.get(include=["metadatas"])
            all_ids: list[str] = existing.get("ids", []) or []
            all_metas: list[dict] = existing.get("metadatas", []) or []

            # Detect old single-vector format and force rebuild if needed.
            if not force_rebuild and self._needs_migration(existing):
                logger.info(
                    "semantic_index: detected old single-vector format — "
                    "running automatic rebuild to migrate to chunk-level storage."
                )
                if all_ids:
                    col.delete(ids=all_ids)
                all_ids = []
                all_metas = []
                # Treat as force_rebuild for purposes of building prior.

            prior_items: dict[str, dict[str, Any]] = {}
            if not force_rebuild and all_ids:
                for cid, md in zip(all_ids, all_metas):
                    md = md or {}
                    ik = _item_key_from_chunk_id(cid)
                    entry = prior_items.setdefault(
                        ik, {"dateModified": md.get("dateModified") or "", "chunk_ids": []}
                    )
                    entry["chunk_ids"].append(cid)
            elif force_rebuild and all_ids:
                col.delete(ids=all_ids)

            seen_keys: set[str] = set()
            to_upsert_ids: list[str] = []
            to_upsert_docs: list[str] = []
            to_upsert_metas: list[dict] = []
            to_delete_ids: list[str] = []

            for it in items:
                item_key = it.get("item_key") or ""
                if not item_key:
                    continue
                seen_keys.add(item_key)

                date_mod = it.get("dateModified") or ""
                prev = prior_items.get(item_key)

                # Skip unchanged items.
                if prev is not None and prev["dateModified"] == date_mod:
                    continue

                # If the item changed, mark its old chunks for deletion.
                if prev is not None:
                    to_delete_ids.extend(prev["chunk_ids"])

                chunks = chunk_item(it)
                if not chunks:
                    continue

                title = (it.get("title") or "").strip()
                doi = (it.get("doi") or "").strip()

                for idx, chunk in enumerate(chunks):
                    chunk_id = _make_chunk_id(item_key, idx)
                    meta = {
                        "item_key": item_key,
                        "doi": doi,
                        "title": title[:400],
                        "dateModified": date_mod,
                        "chunk_idx": idx,
                        "chunk_count": len(chunks),
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "chunk_source": chunk.source,
                        "provider": embedder.provider,
                        "model": embedder.model,
                        "text_format": 2,
                    }
                    to_upsert_ids.append(chunk_id)
                    to_upsert_docs.append(chunk.text)
                    to_upsert_metas.append(meta)

            # Delete item chunks that are no longer present in Zotero.
            stale_item_keys = [ik for ik in prior_items if ik not in seen_keys]
            for ik in stale_item_keys:
                to_delete_ids.extend(prior_items[ik]["chunk_ids"])

            if to_delete_ids and not force_rebuild:
                col.delete(ids=to_delete_ids)

            upserted = 0
            total_pending = len(to_upsert_ids)
            batch_size = _get_upsert_batch_size()
            started_at = datetime.now(timezone.utc).isoformat()

            def _write_progress_status(done: bool) -> None:
                """Persist a status snapshot.  Called after every batch and at end."""
                now = datetime.now(timezone.utc).isoformat()
                snapshot = {
                    "last_sync": now,
                    "updated_at": now,
                    "started_at": started_at,
                    "count": col.count(),
                    "provider": embedder.provider,
                    "model": embedder.model,
                    "dim": embedder.dim,
                    "sqlite_items_seen": len(items),
                    "upserted": upserted,
                    "pending": max(0, total_pending - upserted),
                    "deleted": (
                        len(to_delete_ids) if not force_rebuild else len(all_ids)
                    ),
                    "in_progress": not done,
                }
                try:
                    self._save_status(snapshot)
                except Exception as e:
                    # Never fail the sync for a status-write hiccup; log and keep going.
                    logger.warning("semantic_index: status write failed: %s", e)

            if total_pending:
                logger.info(
                    "semantic_index: beginning streamed upsert of %d chunks "
                    "(batch_size=%d, provider=%s, model=%s)",
                    total_pending, batch_size, embedder.provider, embedder.model,
                )
                # Write an initial status.json so external pollers can see a build is
                # under way before the first batch finishes.
                _write_progress_status(done=False)

                _MAX_EMBED_RETRIES = 3
                _EMBED_RETRY_DELAY = 5.0  # seconds between retries

                try:
                    for start in range(0, total_pending, batch_size):
                        end = min(start + batch_size, total_pending)
                        ids_batch = to_upsert_ids[start:end]
                        docs_batch = to_upsert_docs[start:end]
                        metas_batch = to_upsert_metas[start:end]

                        embs_batch: list[list[float]] = []
                        for attempt in range(1, _MAX_EMBED_RETRIES + 1):
                            try:
                                embs_batch = embedder.encode(docs_batch)
                                break
                            except Exception as embed_exc:
                                if attempt < _MAX_EMBED_RETRIES:
                                    logger.warning(
                                        "semantic_index: embed attempt %d/%d failed "
                                        "(chunks %d-%d): %s — retrying in %.0fs",
                                        attempt, _MAX_EMBED_RETRIES, start, end,
                                        embed_exc, _EMBED_RETRY_DELAY,
                                    )
                                    import time
                                    time.sleep(_EMBED_RETRY_DELAY)
                                else:
                                    logger.error(
                                        "semantic_index: embed failed after %d attempts "
                                        "(chunks %d-%d): %s — aborting sync",
                                        _MAX_EMBED_RETRIES, start, end, embed_exc,
                                    )
                                    raise

                        # If the embedder returned fewer vectors than documents (rare but
                        # possible if the backend silently drops inputs), skip this batch
                        # and log — persisting a mismatched upsert would corrupt Chroma.
                        if len(embs_batch) != len(docs_batch):
                            logger.error(
                                "semantic_index: embedder returned %d vectors for %d "
                                "documents; skipping batch %d-%d",
                                len(embs_batch), len(docs_batch), start, end,
                            )
                            continue

                        col.upsert(
                            ids=ids_batch,
                            documents=docs_batch,
                            embeddings=embs_batch,
                            metadatas=metas_batch,
                        )
                        upserted += len(ids_batch)

                        logger.info(
                            "semantic_index: upserted %d / %d chunks (%.1f%%)",
                            upserted, total_pending,
                            100.0 * upserted / max(1, total_pending),
                        )
                        _write_progress_status(done=False)
                finally:
                    # Always write a terminal status so in_progress never stays True
                    # after the sync exits (whether cleanly or via exception).
                    _write_progress_status(done=True)
            else:
                # Nothing to upsert — still mark done.
                pass

            # Final status — consistent even if no upserts happened.
            _write_progress_status(done=True)

            status = self._load_status()
            return status

        return await asyncio.to_thread(_sync_blocking)

    # -- search ----------------------------------------------------------

    async def search(self, query: str, k: int = 10) -> list[dict[str, Any]]:
        """Return chunk-level search results for *query*.

        Each result dict includes:
          item_key, doi, title, score, snippet, char_start, char_end,
          chunk_source, chunk_idx, chunk_count.
        Callers (e.g. the server handler) can pass char_start/char_end to
        ``fetch_fulltext(mode="range", ...)`` for precise passage retrieval.
        """
        embedder = self._get_embedder()
        self._assert_compatible(embedder)

        def _search_blocking() -> list[dict[str, Any]]:
            col = self._get_chroma_collection()
            emb = embedder.encode_query([query])[0]
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
                        "item_key": md.get("item_key") or _item_key_from_chunk_id(chunk_id),
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

        embedder = self._get_embedder()
        self._assert_compatible(embedder)

        def _do() -> int:
            col = self._get_chroma_collection()
            # Remove any stale chunks for this key first (e.g. from a
            # previous partial upsert).
            prior = col.get(where={"item_key": item_key}, include=[])
            if prior.get("ids"):
                col.delete(ids=prior["ids"])

            ids, docs, metas = [], [], []
            for idx, c in enumerate(chunks):
                ids.append(_make_chunk_id(item_key, idx))
                docs.append(c.text)
                metas.append({
                    "item_key": item_key,
                    "doi": (target.get("doi") or "").strip(),
                    "title": (target.get("title") or "")[:400],
                    "dateModified": target.get("dateModified") or "",
                    "chunk_idx": idx,
                    "chunk_count": len(chunks),
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "chunk_source": c.source,
                    "provider": embedder.provider,
                    "model": embedder.model,
                    "text_format": 2,
                })
            embs = embedder.encode(docs)
            if len(embs) != len(docs):
                logger.warning(
                    "embed_item_now: embedder returned %d vectors for %d docs, skipping %s",
                    len(embs), len(docs), item_key,
                )
                return 0
            col.upsert(ids=ids, documents=docs, embeddings=embs, metadatas=metas)
            return len(ids)

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
        status["configured_model"] = (
            config.semantic_model
            or {"local": "all-MiniLM-L6-v2", "openai": "text-embedding-3-small", "gemini": "gemini-embedding-001"}.get(
                config.semantic_provider or "local", "all-MiniLM-L6-v2"
            )
        )
        return status


_index_singleton: SemanticIndex | None = None


def get_semantic_index() -> SemanticIndex:
    global _index_singleton
    if _index_singleton is None:
        _index_singleton = SemanticIndex()
    return _index_singleton
