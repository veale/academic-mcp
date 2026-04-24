"""Semantic index over Zotero metadata using ChromaDB.

Default embedding backend is sentence-transformers all-MiniLM-L6-v2, loaded
via :mod:`reranker` so the same in-memory model instance is shared with the
search-result re-ranker.

This module is optional at runtime: if chromadb is not installed, semantic
tools return actionable guidance instead of crashing core search flows.

Sync semantics:
  * **Incremental by default.** Each upsert records the item's ``dateModified``
    in Chroma metadata. A subsequent ``sync()`` only re-embeds items whose
    ``dateModified`` is newer than the stored value (or missing entirely).
  * **Deletion detection.** Item keys present in Chroma but absent from the
    current Zotero scan are removed.
  * **Fulltext mode** (``include_fulltext=True``) appends up to
    ``_FULLTEXT_CHARS`` of ``.zotero-ft-cache`` text to the embedded document,
    for items that have a PDF attachment with a cached extraction on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import zotero_sqlite

logger = logging.getLogger(__name__)

_FULLTEXT_CHARS = 8000
_EMBED_BATCH = 64


class SemanticIndexUnavailable(RuntimeError):
    pass


class SemanticIndex:
    def __init__(self) -> None:
        self.cache_dir = Path.home() / ".cache" / "academic-mcp" / "chroma"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.cache_dir / "status.json"
        self.collection_name = "zotero_items"
        self.model_name = "all-MiniLM-L6-v2"

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

    def _get_model(self):
        # Share the sentence-transformers model with reranker.py — there is
        # exactly one instance per process.
        from .reranker import _load_model

        model = _load_model()
        if model is None:
            raise SemanticIndexUnavailable(
                "sentence-transformers model failed to load (see logs). "
                "The core package ships it as a required dependency; this "
                "usually indicates an OOM or a broken install."
            )
        return model

    def _embed(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        out: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = texts[i : i + _EMBED_BATCH]
            embs = model.encode(batch, normalize_embeddings=True)
            out.extend(e.tolist() for e in embs)
        return out

    # -- fulltext helper -------------------------------------------------

    @staticmethod
    def _maybe_ft_cache(attachment_key: str) -> str:
        """Return up to _FULLTEXT_CHARS from an attachment's .zotero-ft-cache, or ''."""
        if not attachment_key:
            return ""
        try:
            base = Path(zotero_sqlite.sqlite_config.storage_path or "")
            if not base:
                return ""
            path = base / attachment_key / ".zotero-ft-cache"
            if not path.exists():
                return ""
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(_FULLTEXT_CHARS)
        except Exception as e:
            logger.debug("ft-cache read failed for %s: %s", attachment_key, e)
            return ""

    # -- sync ------------------------------------------------------------

    async def sync(
        self,
        force_rebuild: bool = False,
        include_fulltext: bool = False,
    ) -> dict[str, Any]:
        if not zotero_sqlite.sqlite_config.available:
            raise SemanticIndexUnavailable("Zotero SQLite backend is not available")

        items = await zotero_sqlite.list_items_for_semantic_index()

        def _sync_blocking() -> dict[str, Any]:
            col = self._get_chroma_collection()

            # Snapshot of what's currently indexed, keyed by item_key.
            #   prior[key] = {"dateModified": str, "include_fulltext": bool}
            prior: dict[str, dict[str, Any]] = {}
            if force_rebuild:
                existing_ids = col.get(include=[]).get("ids", []) or []
                if existing_ids:
                    col.delete(ids=existing_ids)
            else:
                existing = col.get(include=["metadatas"])
                for iid, md in zip(existing.get("ids", []) or [], existing.get("metadatas", []) or []):
                    md = md or {}
                    prior[iid] = {
                        "dateModified": md.get("dateModified") or "",
                        "include_fulltext": bool(md.get("include_fulltext")),
                    }

            seen_keys: set[str] = set()
            to_upsert: list[tuple[str, str, dict]] = []

            for it in items:
                item_key = it.get("item_key") or ""
                if not item_key:
                    continue
                seen_keys.add(item_key)

                title = (it.get("title") or "").strip()
                abstract = (it.get("abstract") or "").strip()
                if not (title or abstract):
                    continue

                date_mod = it.get("dateModified") or ""
                prev = prior.get(item_key)
                # Re-embed when: new, dateModified changed, or fulltext-mode flipped.
                if (
                    prev is not None
                    and prev["dateModified"] == date_mod
                    and prev["include_fulltext"] == bool(include_fulltext)
                ):
                    continue

                text = title
                if abstract:
                    text += "\n\n" + abstract
                if include_fulltext:
                    ft = self._maybe_ft_cache(it.get("attachment_key") or "")
                    if ft:
                        text += "\n\n" + ft

                meta = {
                    "item_key": item_key,
                    "doi": (it.get("doi") or "").strip(),
                    "title": title[:400],
                    "dateModified": date_mod,
                    "include_fulltext": bool(include_fulltext),
                }
                to_upsert.append((item_key, text[: _FULLTEXT_CHARS + 4000], meta))

            # Delete items that left Zotero since the last sync.
            stale = [iid for iid in prior.keys() if iid not in seen_keys]
            if stale and not force_rebuild:
                col.delete(ids=stale)

            upserted = 0
            if to_upsert:
                ids = [x[0] for x in to_upsert]
                docs = [x[1] for x in to_upsert]
                metas = [x[2] for x in to_upsert]
                embs = self._embed(docs)
                col.upsert(ids=ids, documents=docs, embeddings=embs, metadatas=metas)
                upserted = len(ids)

            total = col.count()
            status = {
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "count": total,
                "model": self.model_name,
                "include_fulltext": bool(include_fulltext),
                "sqlite_items_seen": len(items),
                "upserted": upserted,
                "deleted": len(stale) if not force_rebuild else len(prior),
            }
            self._save_status(status)
            return status

        return await asyncio.to_thread(_sync_blocking)

    # -- search ----------------------------------------------------------

    async def search(self, query: str, k: int = 10) -> list[dict[str, Any]]:
        def _search_blocking() -> list[dict[str, Any]]:
            col = self._get_chroma_collection()
            emb = self._embed([query])[0]
            res = col.query(query_embeddings=[emb], n_results=max(1, min(k, 50)))

            ids = (res.get("ids") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]

            out: list[dict[str, Any]] = []
            for idx, item_key in enumerate(ids):
                md = metas[idx] if idx < len(metas) else {}
                doc = docs[idx] if idx < len(docs) else ""
                dist = dists[idx] if idx < len(dists) else 1.0
                score = 1.0 - float(dist)
                out.append(
                    {
                        "item_key": item_key,
                        "doi": md.get("doi") or "",
                        "title": md.get("title") or "",
                        "score": round(score, 4),
                        "snippet": doc[:300],
                    }
                )
            return out

        return await asyncio.to_thread(_search_blocking)

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

        status.setdefault("model", self.model_name)
        status["indexed_count"] = indexed
        status["cache_dir"] = str(self.cache_dir)
        return status


_index_singleton: SemanticIndex | None = None


def get_semantic_index() -> SemanticIndex:
    global _index_singleton
    if _index_singleton is None:
        _index_singleton = SemanticIndex()
    return _index_singleton
