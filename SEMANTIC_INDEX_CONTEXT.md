# Semantic Index Implementation Context

## Objective
Build a fulltext semantic search index over a 13k-item Zotero library using local embeddings (nomic-ai/nomic-embed-text-v1.5), with cloud provider support as an optional future extension.

## Hardware & Environment
- **Machine**: 24 GB unified memory Mac Studio (Apple Silicon)
- **Python**: 3.12.13
- **Package manager**: `uv` with lock file
- **Python env**: `.venv` (created by `uv sync --extra semantic`)

## Architecture Overview

```
User's MCP Client (Claude Desktop, etc.)
         ↓
    MCP Server (server.py)
    - search_papers tool
    - semantic_search_zotero tool
    - semantic_index_rebuild tool (with provider/model params)
    - semantic_index_status tool
         ↓
  SemanticIndex (semantic_index.py)
    - sync(force_rebuild, include_fulltext, provider, model)
    - search(query) — uses encode_query()
    - status()
         ↓
  Embedder (embeddings.py)
    - encode(texts) — for indexing
    - encode_query(texts) — for querying
         ↓
  [Local] Sentence-Transformers
    OR [Cloud] OpenAI / Gemini (vectors always stored locally in Chroma)
         ↓
  ChromaDB (persistent HNSW index in ~/.cache/academic-mcp/chroma/)
         ↓
  Zotero SQLite (zotero_sqlite.py)
    - list_items_for_semantic_index() — fetches title, abstract, DOI, attachment key, dateModified
    - reads .zotero-ft-cache files for fulltext mode
```

## Key Components

### 1. semantic_index.py
**Purpose**: Orchestrates ChromaDB vector index over Zotero metadata.

**Class: SemanticIndex**
- `cache_dir`: `~/.cache/academic-mcp/chroma/` (persistent HNSW index)
- `status_path`: `~/.cache/academic-mcp/chroma/status.json` (tracks provider/model/sync time)
- `_embedder`: Lazy-loaded `Embedder` instance (cached to avoid reloading)

**Methods:**
- `_load_status()` / `_save_status()` — JSON file-based status tracking
- `_get_chroma_collection()` — creates or retrieves Chroma collection "zotero_items"
- `_get_embedder(provider, model)` — resolves and caches embedder; raises error on provider mismatch
- `_assert_compatible(embedder)` — checks that current embedder provider/model/dim match stored collection
- `_maybe_ft_cache(attachment_key)` — reads `.zotero-ft-cache` file for fulltext mode, up to `_FULLTEXT_CHARS` (currently 2000 chars to fit in 24GB memory with batch_size=2)
- `sync(force_rebuild, include_fulltext, provider, model)` — incremental or full rebuild
  - Scans `zotero_sqlite.list_items_for_semantic_index()` for new/modified items
  - Embeds with `self._embed()` (which calls `embedder.encode()`)
  - Upserts to Chroma with metadata: `{item_key, doi, title, dateModified, include_fulltext, provider, model}`
  - Deletes stale items (in Chroma but not in Zotero)
  - Saves status with provider/model/dim/count/upserted/deleted/last_sync
- `search(query, k)` — vector search
  - Resolves embedder, checks compatibility
  - Embeds query with `embedder.encode_query()` (uses task prompt if nomic)
  - Queries Chroma with cosine distance
  - Returns `[{item_key, doi, title, score, snippet}, ...]`
- `status()` — returns current index state for diagnostics

**Key detail**: Each upserted record stores `provider` and `model` in Chroma metadata. A mismatch on sync/search is refused with an actionable error, preventing silent vector-space corruption.

### 2. embeddings.py
**Purpose**: Pluggable embedding backend abstraction.

**Dataclass: Embedder**
- `provider`: "local" | "openai" | "gemini"
- `model`: arbitrary string (e.g., "nomic-ai/nomic-embed-text-v1.5", "text-embedding-3-small")
- `dim`: vector dimensionality (None until first encode)
- `_encode`: Callable for document/index-time encoding
- `_encode_query`: Optional separate callable for query-time encoding (None for most models)

**Methods:**
- `encode(texts)` — documents (uses `_encode`, e.g., with "document" task prompt for nomic)
- `encode_query(texts)` — queries (uses `_encode_query` if present, else falls back to `_encode`, e.g., with "query" task prompt for nomic)

**Factory: resolve_embedder(provider, model)**
- Reads `config.semantic_provider` and `config.semantic_model` as defaults
- Validates provider is in `_DEFAULT_MODELS`
- Calls `_local_encoder()`, `_openai_encoder()`, or `_gemini_encoder()`
- Returns `Embedder` with both doc and query encoders

**Local Encoder (_local_encoder)**
- Reuses reranker's shared `all-MiniLM-L6-v2` instance if requested
- Loads other models on-demand into `_local_models` cache (shared across calls)
- **Nomic-specific**: Loads with `trust_remote_code=True` (required by custom architecture)
- **Task prompts**: Detects nomic-embed-text fragments and wires both `("document", "query")` prompts
- **Batch size**: Per-model tuning in `_BATCH_SIZES` dict
  - Default: 32
  - Nomic: 2 (quadratic attention on 24GB; 2000-char docs fit without OOM)
- Returns tuple `(doc_encoder, query_encoder_or_none)`

**Cloud Encoders (_openai_encoder, _gemini_encoder)**
- Batch into chunks (96 for OpenAI, 100 for Gemini)
- Make HTTPS calls to respective endpoints
- Check API key is present; raise `EmbedderUnavailable` if missing
- Return vectors as lists of floats

### 3. config.py
**New fields for semantic indexing:**
```python
semantic_provider: str = os.getenv("SEMANTIC_PROVIDER", "local")
semantic_model: str = os.getenv("SEMANTIC_MODEL", "").strip()
openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
```

### 4. server.py
**New Tools:**

1. `semantic_search_zotero(query, k)` → vector search results
2. `semantic_index_status()` → JSON status dump (provider, model, count, last_sync, etc.)
3. `semantic_index_rebuild(fulltext, provider, model)` → rebuild index with optional provider override
   - Handler: `_handle_semantic_index_rebuild()` passes provider/model through to `idx.sync()`
4. Updated `search_papers(semantic=true, include_scite=true)` → blends semantic Zotero hits + status block
   - Status block format: `[scite: 12/15 enriched | semantic: 13,158 items, synced 3h ago]`

**Key detail**: `search_papers` now has a status block at the top showing enrichment state — users/LLMs can see whether semantic/scite actually enriched the results or failed silently.

### 5. zotero_sqlite.py
**New async functions:**
- `list_items_for_semantic_index()` — returns `[{item_key, title, abstract, doi, dateModified, attachment_key}, ...]`
- Reads `.zotero-ft-cache` for fulltext mode (optional, up to 2000 chars per item)

## Deployment Path

### 1. Setup
```bash
cd /Users/michael/Developer/academic-mcp
uv sync --extra semantic  # Installs chromadb, einops, sentence-transformers
```

### 2. Configuration (.env)
```
SEMANTIC_PROVIDER=local
SEMANTIC_MODEL=nomic-ai/nomic-embed-text-v1.5
```

### 3. Initial Build (slow, one-time)
```bash
uv run python - <<'EOF'
import asyncio, json, sys
sys.path.insert(0, "src")
from academic_mcp.semantic_index import get_semantic_index

async def main():
    idx = get_semantic_index()
    print("Building semantic index (fulltext=True) …")
    status = await idx.sync(force_rebuild=True, include_fulltext=True)
    print(json.dumps(status, indent=2))

asyncio.run(main())
EOF
```
**Expected time**: ~50 min on 24GB Apple Silicon (batch_size=2)
**Model download**: ~547 MB (nomic-ai/nomic-bert-2048 foundation)

### 4. Incremental Syncs (fast, background)
Zotero items store `dateModified`. Subsequent syncs only re-embed changed items.

### 5. MCP Client Usage
```python
# Via Claude Desktop or CLI:
search_papers(query="algorithmic bias", semantic=true, include_scite=true)
# Returns status block + blended results with semantic Zotero hits

semantic_search_zotero(query="surveillance capitalism", k=10)
# Direct vector search, no other sources

semantic_index_status()
# See current provider, model, item count, last sync time

semantic_index_rebuild(fulltext=true, provider="nomic-ai", model="nomic-embed-text-v1.5")
# Force rebuild with explicit provider/model
```

## Fulltext Mode Details

### Why 2000 chars, not 8000?
- Nomic's 2048-token context window = ~2048 chars at ~1 char/token ratio
- Abstract + title: ~300 chars
- Leaving 1700 chars for fulltext append = 2000 target
- Batch_size=2 with 2000-char docs → ~1000 tokens/doc → peak attention memory ~6–8 GB (fits in 24GB)
- Batch_size=6 still overflows; batch_size=1 would work but cuts build speed in half again

### What 2000 chars captures
- Entire abstract (typically 150–300 words)
- Introduction section (typically 500–1000 words)
- Early results if available

### What it misses
- Late-stage novel results (usually come after page 6+)
- Discussion/limitations sections
- Appendices

### Trade-off reasoning
- For algorithmic regulation / law / AI ethics (your domain), the abstract + intro almost always state the core contribution
- Late results are refinements, not new concepts
- Concepts buried past page 10 are rare in your domain and usually mentioned earlier in the abstract anyway

## Current Known Issues & Workarounds

### Issue: Zotero fulltext cache truncation

**Root**: Zotero’s PDF indexing defaults cap at 100 pages / 500,000 chars per
attachment. The semantic chunker reads whatever ended up in `.zotero-ft-cache`,
so book-length documents are silently truncated.

**Fix**: Raise limits in Zotero Settings → Search → PDF Indexing to
500 pages / 2,000,000 chars, then trigger “Rebuild index” to re-process
existing items. Composes with `chunking._MAX_FT_CHARS` (defaults to
200,000 per-item) to determine final coverage.

**Detection**: `fetch_fulltext(doi=..., mode="sections")` for a book
whose last section’s `char_end` is suspiciously close to a round number
(100k, 500k) is a strong signal of Zotero-side truncation.

### Issue: nomic model requires `trust_remote_code=True`
**Root**: nomic uses a custom `modeling_hf_nomic_bert.py` from HuggingFace Hub
**Solution**: Wired in `_local_encoder()` when fragment "nomic-embed-text" detected
**Dependency**: `einops` (added to `[semantic]` extra in pyproject.toml)

### Issue: Task prompts ("document" vs "query") required for nomic
**Root**: Nomic model card specifies two prompts for asymmetric embedding (different optimization for docs vs. queries)
**Solution**: Detected in `_local_encoder()`, wired to `Embedder._encode_query()`, used in `semantic_index.search()`
**Prompt names**: `"document"` (indexing) and `"query"` (searching) in v1.5

### Issue: Attention memory quadratic in sequence length and batch size
**Root**: Nomic uses standard transformer attention: O(L² × B) memory where L=sequence length, B=batch size
**Workaround**: Batch_size=2 (no further reduction without unacceptable slowdown)
**Future mitigation**: Consider sparse attention or switch to a smaller model (BGE-large-en-v1.5 uses 1024 dims but standard attention, much faster)

## Testing
- 33 passing tests in `tests/test_embeddings.py`
- Coverage: provider selection, model loading, API key validation, task prompt wiring, encode_query fallback
- No integration tests yet (would need mock Zotero DB)

## Dependencies (pyproject.toml)
```toml
[project.optional-dependencies]
semantic = [
    "chromadb>=0.5.0",
    "einops>=0.7.0",       # required by nomic-embed-text-v1.5
]
```

## Future Extensibility
1. **Provider switching at runtime**: Already wired — `semantic_index_rebuild(provider="openai", model="text-embedding-3-small")` works
2. **Custom batch sizes per model**: `_BATCH_SIZES` dict — add new entries as needed
3. **Multi-chunk fulltext mode**: Currently one vector per paper; could chunk into ~500-token segments and store (sep. feature)
4. **GPU offload**: Sentence-transformers supports CUDA/MPS; would need device detection + config
5. **Incremental Chroma backup**: Currently just persistent HNSW; could snapshot to S3/etc.

## Code Organization
```
src/academic_mcp/
├── embeddings.py           # Provider abstraction + loaders
├── semantic_index.py       # ChromaDB orchestration
├── config.py               # Env var loading (includes SEMANTIC_* fields)
├── server.py               # MCP tools (search_papers status block, new tools)
├── zotero_sqlite.py        # Data layer (list_items_for_semantic_index)
└── reranker.py             # Shares sentence-transformers model instance
tests/
└── test_embeddings.py      # Provider switching tests
```

## Rollback / Reset
```bash
# Clear the index (but keep embedding model cache):
rm -rf ~/.cache/academic-mcp/chroma/

# Clear model cache:
rm -rf ~/.cache/huggingface/

# Re-init in-process:
# (reload Python, which re-triggers SemanticIndex.__init__())
```

---

**Last updated**: April 24, 2026 (post-fulltext-mode wiring)
