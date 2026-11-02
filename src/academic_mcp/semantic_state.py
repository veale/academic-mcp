"""Local progress ledger for resumable, incremental semantic indexing.

Hashes let an existing Chroma index be adopted without re-embedding its
unchanged text. Each completed vector batch is checkpointed. An item is only
complete once all its chunks and metadata have been written.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def metadata_json(metadata):
    return json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


class IndexState:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,item_key TEXT NOT NULL,doc_hash TEXT NOT NULL,metadata TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS chunks_item ON chunks(item_key);
            CREATE TABLE IF NOT EXISTS items (
                item_key TEXT PRIMARY KEY,signature TEXT NOT NULL,complete INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)

    def bootstrap(self, col, progress=None):
        count = col.count()
        row = self.db.execute(
            "SELECT value FROM state WHERE name='bootstrapped'"
        ).fetchone()
        if (
            row
            and self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == count
        ):
            return
        # Chroma is the authority after a crash between its commit and ours.
        # Stream hashes to disk instead of retaining every document in RAM.
        with self.db:
            self.db.execute("DELETE FROM chunks")
            self.db.execute("DELETE FROM items")
            offset = 0
            while True:
                page = col.get(
                    include=["documents", "metadatas"], limit=1000, offset=offset
                )
                ids = page.get("ids") or []
                if not ids:
                    break
                for cid, doc, md in zip(
                    ids, page.get("documents") or [], page.get("metadatas") or []
                ):
                    self.put(cid, doc or "", md or {})
                offset += len(ids)
                if progress and (offset % 10000 == 0 or offset >= count):
                    progress(offset, count)
                if len(ids) < 1000:
                    break
            self.db.execute("INSERT OR REPLACE INTO state VALUES('bootstrapped','1')")

    def put(self, cid, text, metadata):
        key = metadata.get("item_key") or cid.rsplit(":", 1)[0]
        self.db.execute(
            "INSERT OR REPLACE INTO chunks VALUES(?,?,?,?)",
            (cid, key, text_hash(text), metadata_json(metadata)),
        )

    def item(self, key):
        return self.db.execute(
            "SELECT signature,complete FROM items WHERE item_key=?", (key,)
        ).fetchone()

    def chunks(self, key):
        return {
            r["id"]: r
            for r in self.db.execute("SELECT * FROM chunks WHERE item_key=?", (key,))
        }

    def keys(self):
        return {r[0] for r in self.db.execute("SELECT DISTINCT item_key FROM chunks")}

    def start(self, key, signature):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO items VALUES(?,?,0)", (key, signature)
            )

    def finish(self, key):
        with self.db:
            self.db.execute("UPDATE items SET complete=1 WHERE item_key=?", (key,))

    def delete(self, ids):
        with self.db:
            self.db.executemany(
                "DELETE FROM chunks WHERE id=?", ((cid,) for cid in ids)
            )

    def close(self):
        self.db.close()
