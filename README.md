# Academic Research MCP Server

An MCP server that searches academic papers, fetches PDFs, extracts text, and returns content ready for LLM context windows. Designed for zero-RAM PDF handling, native async I/O, and Zotero-first retrieval.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        MCP Server                                │
│                                                                  │
│  Tools:                                                          │
│  ├── search_papers       (Zotero + Semantic Scholar + OpenAlex)  │
│  ├── search_zotero       (search your Zotero library)            │
│  ├── search_by_doi       (instant DOI lookup via SQLite)         │
│  ├── get_paper           (metadata by DOI)                       │
│  ├── fetch_fulltext      (multi-strategy PDF grab + extraction)  │
│  ├── search_and_read     (combined search → full text)           │
│  ├── find_pdf_urls       (list available URLs)                   │
│  ├── list_zotero_libraries (all libraries + item counts)         │
│  └── refresh_zotero_index  (rebuild DOI cache + diagnostics)     │
│                                                                  │
│  Content Retrieval Priority:                                     │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ 0. SQLITE (fastest — direct read of zotero.sqlite)       │    │
│  │    a) .zotero-ft-cache  (pre-extracted fulltext on disk) │    │
│  │    b) Local storage PDF  (~/Zotero/storage/<key>/)       │    │
│  │    c) Local WebDAV dir   (skip HTTP, read zip from disk) │    │
│  │    d) WebDAV over HTTP   (stream zip → extract to cache) │    │
│  ├──────────────────────────────────────────────────────────┤    │
│  │ 1. ZOTERO API (fallback if SQLite unavailable)           │    │
│  │    a) Fulltext from Web API (pre-extracted text)         │    │
│  │    b) PDF from local ~/Zotero/storage/<key>/             │    │
│  │    c) PDF from Zotero Web API file download              │    │
│  │    d) PDF from WebDAV server (<key>.zip)                 │    │
│  ├──────────────────────────────────────────────────────────┤    │
│  │ 2. OPEN ACCESS (if not in Zotero)                        │    │
│  │    a) Unpaywall (best OA source)                         │    │
│  │    b) Semantic Scholar openAccessPdf                      │    │
│  │    c) OpenAlex open_access.oa_url                        │    │
│  ├──────────────────────────────────────────────────────────┤    │
│  │ 3. STEALTH / PROXY (last resort)                         │    │
│  │    a) Scrapling stealth browser (offloaded to thread)    │    │
│  │    b) GOST proxy for institutional access                │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

**Zero-RAM PDF pipeline.** Every PDF fetcher (HTTP, WebDAV, Scrapling) streams directly to `PDF_CACHE_DIR` and returns a file `Path` — never a `bytes` object. PyMuPDF reads from disk via `fitz.open(filename=...)`. A 50 MB PDF uses ~64 KB of RAM (one chunk buffer) regardless of size.

**Native async SQLite.** All database access uses `aiosqlite` for non-blocking queries on the main event loop. No `asyncio.to_thread()` overhead on every DOI lookup or keyword search.

**LRU cache eviction.** The PDF cache directory self-regulates. Before each fetch, files are scanned and the oldest are evicted if the total exceeds `PDF_CACHE_MAX_BYTES` (default 2 GB).

**Bounded reads everywhere.** Zotero's `.zotero-ft-cache` files (which can exceed 100 MB for OCR'd textbooks) are read only up to `MAX_CONTEXT_LENGTH` characters. PDF text extraction breaks early once the context limit is reached.

**Parallel API pagination.** When building the DOI index via the Zotero Web API, all pages are fetched concurrently (semaphore-limited to 5 in-flight requests) with retry and exponential backoff on 429 responses.

**Zip bomb protection.** All zip extraction loops (WebDAV local, WebDAV HTTP) enforce a 150 MB cap on extracted file size.

**Relevance-ranked results.** `search_papers` results are sorted by a composite score: Zotero membership → open-access availability → cross-source agreement → citation count (log-scaled) → recency. The LLM sees the most actionable papers first.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (install with `curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
cd academic-mcp
uv sync
```

That creates a `.venv`, resolves all dependencies, and installs the package. Takes a few seconds.

For optional stealth browser support:
```bash
uv sync --extra stealth
```

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

### Core settings

| Variable | Default | Description |
|---|---|---|
| `UNPAYWALL_EMAIL` | *(required)* | Email for Unpaywall API (no key needed) |
| `SEMANTIC_SCHOLAR_API_KEY` | *(empty)* | Optional; gives higher rate limits |
| `GOST_PROXY_URL` | *(empty)* | SOCKS5/HTTP proxy for institutional access |
| `USE_STEALTH_BROWSER` | `true` | Enable Scrapling-based fetching |
| `SCRAPLING_MCP_URL` | *(empty)* | Remote Scrapling MCP server SSE endpoint (see below) |
| `PDF_CACHE_DIR` | `~/.cache/academic-mcp/pdfs` | Where to cache downloaded PDFs |
| `PDF_CACHE_MAX_BYTES` | `2147483648` (2 GB) | Max cache size before LRU eviction kicks in |
| `MAX_CONTEXT_LENGTH` | `100000` | Max characters returned to the LLM |

### Zotero settings

| Variable | Default | Description |
|---|---|---|
| `ZOTERO_SQLITE_PATH` | `~/Zotero/zotero.sqlite` | Path to Zotero's SQLite database (preferred backend) |
| `ZOTERO_LOCAL_STORAGE` | `~/Zotero/storage` | Path to Zotero's attachment storage directory |
| `ZOTERO_LOCAL_ENABLED` | `true` | Connect to Zotero desktop's local API |
| `ZOTERO_LOCAL_HOST` | `localhost` | Host for local API (change for SSH tunnels) |
| `ZOTERO_LOCAL_PORT` | `23119` | Port for local API |
| `ZOTERO_API_KEY` | *(empty)* | Web API key from zotero.org/settings/keys |
| `ZOTERO_USER_ID` | *(empty)* | Your Zotero user ID |
| `ZOTERO_LIBRARY_TYPE` | `user` | `user` or `group` |
| `ZOTERO_GROUP_ID` | *(empty)* | Group ID (only when library_type=group) |
| `ZOTERO_WEBDAV_URL` | *(empty)* | WebDAV server URL |
| `ZOTERO_WEBDAV_USER` | *(empty)* | WebDAV username |
| `ZOTERO_WEBDAV_PASS` | *(empty)* | WebDAV password |
| `ZOTERO_WEBDAV_LOCAL_PATH` | *(empty)* | Local mount of WebDAV dir (skips HTTP entirely) |

## Running

```bash
# stdio mode (for Claude Desktop, Cursor, etc.)
uv run python -m academic_mcp

# SSE mode (for remote/web connections)
uv run python -m academic_mcp --transport sse --port 8080
```

### Claude Desktop config

The `--directory` flag is the key detail — it tells `uv` where to find `pyproject.toml` so it activates the right venv regardless of the working directory Claude Desktop launches from.

```json
{
  "mcpServers": {
    "academic": {
      "command": "uv",
      "args": [
        "--directory", "/Users/you/path/to/academic-mcp",
        "run", "python", "-m", "academic_mcp"
      ],
      "env": {
        "UNPAYWALL_EMAIL": "you@example.com"
      }
    }
  }
}
```

### Adding dependencies

```bash
uv add some-package        # adds to [dependencies]
uv add scrapling --extra stealth  # adds to [project.optional-dependencies]
```

Commit the generated `uv.lock` to version control — it pins exact versions so anyone else can `uv sync` and get an identical environment.

## Zotero Integration

The server checks your Zotero library **before** scraping the internet. This is the fastest path and avoids unnecessary network requests, anti-bot issues, and paywall problems for papers you already have.

### Backend priority

1. **SQLite** (fastest, preferred) — Reads `zotero.sqlite` directly with `aiosqlite`. No API calls, no running Zotero instance needed. Searches all libraries (user + groups). Supports DOI lookup, keyword search across title/authors/abstract/tags/fulltext, and reads `.zotero-ft-cache` files for instant fulltext retrieval.

2. **Local API** — Connects to Zotero 7/8 desktop at `localhost:23119`. Fast, no auth needed. Also reads PDFs from `~/Zotero/storage/`.

3. **Web API** — Connects to `api.zotero.org`. Needs an API key. Supports the `/fulltext` endpoint (pre-extracted text) and `/file` download. Pagination is parallelized with a concurrency limit of 5 and retry on 429.

4. **WebDAV** — Fetches `<key>.zip` from your WebDAV server. Streams to disk, extracts PDF with zip bomb protection (150 MB cap).

### DOI index

The Zotero Web API has **no way to search by DOI field** — confirmed by the Zotero team. So the server builds a DOI→itemKey index by scanning your library on first use. With the SQLite backend this is instant (a single SQL query). For the API fallback, pages are fetched concurrently. The index is persisted to disk between sessions. Use `refresh_zotero_index` to rebuild it after adding papers.

### Fulltext truncation

Zotero's fulltext indexing defaults to ~100 pages / ~500K chars. The server detects truncation and warns you. Fix: Zotero → Settings → Search → PDF Indexing → increase limits, then reindex and sync.

### Setup options

**SQLite (recommended):**
```bash
ZOTERO_SQLITE_PATH=~/Zotero/zotero.sqlite
ZOTERO_LOCAL_STORAGE=~/Zotero/storage
# Optional: local WebDAV mount
ZOTERO_WEBDAV_LOCAL_PATH=/mnt/nextcloud/zotero
```

**Web API:**
```bash
ZOTERO_API_KEY=your_key_here
ZOTERO_USER_ID=12345678
```

**WebDAV:**
```bash
ZOTERO_WEBDAV_URL=https://dav.example.com/zotero/
ZOTERO_WEBDAV_USER=alice
ZOTERO_WEBDAV_PASS=secret
```

## Search Quality

### How results are ranked

`search_papers` queries Zotero, Semantic Scholar, and OpenAlex, deduplicates by DOI, then sorts by a composite score:

1. **In Zotero** — papers you already own surface first (instant full-text retrieval).
2. **Open access available** — papers the server can actually fetch.
3. **Cross-source breadth** — a paper found by multiple databases is more likely relevant.
4. **Citation count** (log-scaled) — highly cited papers are preferred, but a single mega-cited survey doesn't bury everything else.
5. **Recency** — papers from the last 3 years get a small boost.

### Tips for better queries

The underlying APIs are keyword-based, not semantic. The tool descriptions guide the LLM toward concise keyword queries, but if results are poor:

- Use 2–6 specific keywords, not full sentences.
- Include author surnames when searching for a specific paper.
- Use `source="zotero"` to search only your library.
- For DOI-based lookup, use `search_by_doi` or `get_paper` directly.

### SQLite keyword search phases

When the SQLite backend is available, `search_zotero` runs a multi-phase search. Results from earlier phases (title, DOI, author) are ranked above later phases (fulltext body mentions):

1. Title match — all query terms must appear in the title.
2. DOI exact match — single-term queries checked against the DOI field.
3. Creator match — author first/last names.
4. Abstract match — all terms in the abstract.
5. Tag match — Zotero tags.
6. Fulltext word index — Zotero's `fulltextItemWords` table.

## Remote Scrapling Server (Optional)

By default, Scrapling launches a local Chromium instance. On headless servers or resource-constrained machines, set `SCRAPLING_MCP_URL` to offload the browser to a remote Scrapling MCP server.

**Remote mode** (`SCRAPLING_MCP_URL` set):
```bash
# On the remote machine (has Chromium installed):
scrapling mcp --http --host 0.0.0.0 --port 8000

# In your .env:
SCRAPLING_MCP_URL=http://192.168.1.50:8000/sse
```
The local server acts as an MCP *client*, connecting over SSE to the remote Scrapling MCP server. It discovers available tools at runtime via `list_tools()`, calls the scraping tool with `{"url": "...", "proxy": "..."}`, and handles the response — either decoding a base64-encoded PDF directly, or parsing the HTML to find a PDF link and making a second tool call. When `GOST_PROXY_URL` is configured, it's forwarded in every tool call so the *remote* browser routes through your institution's network. No local Scrapling or Chromium needed.

**Local mode** (default — `SCRAPLING_MCP_URL` empty):
```bash
SCRAPLING_MCP_URL=
```
Scrapling launches a local Chromium instance inside `asyncio.to_thread`. The GOST proxy is passed directly to `StealthyFetcher.fetch(proxy=...)` so the local browser routes through your institution.

## GOST Proxy Setup (Optional)

For IP locked access, run [GOST](https://github.com/ginuerzh/gost) as a local proxy:

```bash
gost -L socks5://:1080 -F ssh://user@iplocked-gateway:22
```

Then set `GOST_PROXY_URL=socks5://localhost:1080` in your `.env`.

## Remote Zotero Access

**Option A: SQLite + rsync (recommended for headless servers):**
```bash
rsync -az user@zotero-machine:~/Zotero/zotero.sqlite ~/Zotero/
rsync -az user@zotero-machine:~/Zotero/storage/ ~/Zotero/storage/
```

**Option B: SSH tunnel:**
```bash
autossh -M 0 -N -L 23119:localhost:23119 user@your-zotero-machine
```

**Option C: Web API only:**
```bash
ZOTERO_LOCAL_ENABLED=false
ZOTERO_API_KEY=your_key
ZOTERO_USER_ID=12345678
```

## Running Zotero Headless

Zotero does not officially support headless mode. Workarounds:

```bash
# xvfb (simplest)
sudo apt install xvfb
xvfb-run -a /opt/zotero/zotero &

# Docker (LinuxServer.io)
docker run -p 23119:23119 -p 3000:3000 -v ./zotero-data:/config lscr.io/linuxserver/zotero:latest
``