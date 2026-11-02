# Indexing and batch-fetch repair — 2 November 2026

The existing semantic collection used OpenRouter's `baai/bge-m3` model with
1,024-dimensional normalised vectors. Retrieval worked, but a stale Mac
snapshot, obsolete nightly counters, timestamp-only change detection, trash
handling, and incomplete-item recovery prevented reliable incremental updates.

## Changes

- An incremental, version-checked Zotero Web API catalog overlays the optional
  read-only Mac snapshot. Personal and group library changes, trash and
  permanent deletions are followed independently of the source Mac. Search
  metadata and DOI lookups hydrate from this catalog, so recently synced
  records are not lost when absent from the old SQLite snapshot.
- Metadata/index checks run every five minutes. Missing DOI lookups can refresh
  sooner. The nightly job requests a real incremental pass instead of checking
  counters no longer written by the indexer.
- Newly synced PDFs are fetched from the desktop's file backend and extracted
  in bounded process workers. WebDAV is used for personal libraries configured
  for it, and group downloads use their own Web API library endpoint. Metadata
  sync and WebDAV file sync are both required; the source Mac need not remain
  online afterward. PDF work is limited per pass, and unavailable files retry.
- Semantic indexing tracks file changes independently of Zotero timestamps.
  Reading an article cache now updates access time without changing content
  modification time. Unstructured cached full text can also be indexed.
- A progress ledger adopts existing vector hashes without re-embedding valid
  unchanged text. Partial papers resume missing chunks, and items become
  complete only after all vector and metadata writes succeed. Failed embedding
  responses retain previous searchable vectors and remain visibly pending.
  Metadata-only changes reuse vectors. Trashed items/attachments are excluded.
- The lexical index also detects changed full text rather than relying only on
  Zotero's metadata timestamp. Failed source reads cannot be mistaken for an
  empty library and trigger mass pruning.
- Embedding responses validate input/vector counts, dimensions, finite values,
  and input ordering. Transient overload/rate-limit responses use exponential
  retry backoff and `Retry-After`.
- Batch fetching has a bounded response wait and returns per-DOI cached,
  pending, or failed outcomes. Slow jobs continue and repeated calls coalesce.
  PDF parsing no longer occupies the MCP event loop. Publisher metadata
  requests run concurrently rather than serially.
- The optional Mac sync template watches WAL, journal and main-file changes.
  SQLite online backup includes committed WAL data; only the backup is
  normalised to journal mode DELETE and validated. Source Zotero settings are
  not changed. Published snapshots are opened immutable and replaced atomically.

## Embedded Chroma ownership

During the initial staged repair, the old running server interpreted repair
status as an interrupted build and launched a competing legacy sync. Chroma's
HNSW cache became inconsistent. A pre-repair copy-on-write vector backup was
restored, and repair was moved into the running server. Zotero records and
PDFs were not part of this vector-cache restoration.

An OS ownership lock now retains one embedded Chroma client per directory and
process. Other processes are rejected and directed to MCP. Concurrent threads
reuse that client, and indexing/hot-path writes also share a writer lock.
Manual maintenance must use the owning server's MCP tools, not another Python
process opening its Chroma directory. This also avoids serving from an old
in-memory HNSW view while another process writes the underlying files.

The pre-repair backup is retained on Asahi as
`/home/mv/docker-binds/academic-mcp/cache/chroma-before-20261102`.
The interrupted cache was retained separately for diagnosis. Source metadata,
PDF caches and WebDAV files were retained throughout.

## Validation

The full suite passes 300 tests, including real PDF process execution,
non-blocking batch responses, response coalescing, WAL backup with an open
writer, trashed-item exclusion, timestamp-independent content updates,
interrupted-paper completion, vector reuse, failed-source preservation,
metadata pagination/version consistency, recently synced DOI/search hydration,
group API routing, embedding overload backoff, and cross-process ownership.

The user's eight-DOI batch was exercised against the staged implementation
after desktop sync. All eight were found through Zotero; the first response
took about one second (two articles were newly fetched), and a repeat took
0.04 seconds. The server catalog followed the personal library and all ten
group libraries without requiring a new Mac snapshot.

Incremental adoption/recovery is monitored through MCP and persisted status.
Cloud provider overload can slow progress, but successful batches are retained
and incomplete papers are retryable. The embedding model is unchanged.

References:
[SQLite WAL](https://www.sqlite.org/wal.html),
[SQLite online backup](https://www.sqlite.org/backup.html),
[PyMuPDF multiprocessing](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html),
[OpenRouter embeddings and overload handling](https://openrouter.ai/docs/api-reference/embeddings).
