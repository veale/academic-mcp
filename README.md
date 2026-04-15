# Academic Research MCP Server

An MCP server that searches academic papers, fetches full text, and returns content ready for LLM context windows. Designed for zero-RAM PDF handling, HTML article extraction, native async I/O, and Zotero-first retrieval.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        MCP Server                                │
│                                                                  │
│  Tools:                                                          │
│  ├── search_papers       (Zotero + S2 + OpenAlex + Primo)        │
│  ├── search_zotero       (search your Zotero library)            │
│  ├── search_by_doi       (instant DOI lookup via SQLite)         │
│  ├── get_paper           (metadata by DOI)                       │
│  ├── fetch_fulltext      (multi-strategy HTML + PDF extraction)  │
│  │     mode: full | sections | preview | section | range         │
│  │     source: auto | html                                       │
│  ├── search_in_article   (BM25 search + dispersion heatmap)      │
│  ├── search_and_read     (combined search → full text)           │
│  ├── find_pdf_urls       (list available URLs)                   │
│  ├── list_zotero_libraries (all libraries + item counts)         │
│  └── refresh_zotero_index  (rebuild DOI cache + diagnostics)     │
│                                                                  │
│  Content Retrieval Priority:                                     │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │ TEXT CACHE (instant — .article.json in pdf_cache_dir)    │    │
│  │    Checked before all other sources.  Written on first   │    │
│  │    successful extraction.  Subject to LRU eviction.      │    │
│  ├──────────────────────────────────────────────────────────┤    │
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
│  │ 2. DIRECT HTTP (if not in Zotero)                        │    │
│  │    Unpaywall / Semantic Scholar / OpenAlex OA PDF URLs   │    │
│  │    — fast HTTP GET, no browser, handles arXiv/PMC/SSRN   │    │
│  ├──────────────────────────────────────────────────────────┤    │
│  │ 3. STEALTH BROWSER (if direct fetch fails)               │    │
│  │    Single Scrapling call to the DOI landing page, then:  │    │
│  │    a) citation_pdf_url meta tag → direct/proxied fetch   │    │
│  │    b) HTML article extraction via trafilatura (≥1500 wds)│    │
│  │    c) <a>-tag PDF link scanning → direct/proxied fetch   │    │
│  │    d) GOST proxy on candidate URLs (institutional access)│    │
│  │    e) Scrapling on candidate URLs (last resort)          │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

**Zero-RAM PDF pipeline.** Every PDF fetcher (HTTP, WebDAV, Scrapling) streams directly to `PDF_CACHE_DIR` and returns a file `Path` — never a `bytes` object. PyMuPDF reads from disk via `fitz.open(filename=...)`. A 50 MB PDF uses ~64 KB of RAM (one chunk buffer) regardless of size.

**Native async SQLite.** All database access uses `aiosqlite` for non-blocking queries on the main event loop. No `asyncio.to_thread()` overhead on every DOI lookup or keyword search.

**LRU cache eviction.** The PDF cache directory self-regulates. Before each fetch, files are scanned and the oldest are evicted if the total exceeds `PDF_CACHE_MAX_BYTES` (default 2 GB). Both `.pdf` and `.article.json` files are evictable.

**Bounded reads everywhere.** Zotero's `.zotero-ft-cache` files (which can exceed 100 MB for OCR'd textbooks) are read only up to `MAX_CONTEXT_LENGTH` characters. PDF text extraction breaks early once the context limit is reached.

**Parallel API pagination.** When building the DOI index via the Zotero Web API, all pages are fetched concurrently (semaphore-limited to 5 in-flight requests) with retry and exponential backoff on 429 responses.

**Zip bomb protection.** All zip extraction loops (WebDAV local, WebDAV HTTP) enforce a 150 MB cap on extracted file size.

**HTML-first article extraction.** When a stealth browser fetch returns a publisher landing page, the server tries three extraction strategies before falling back to the PDF pipeline. First, it reads the `citation_pdf_url` meta tag — the publisher's own canonical PDF URL, the same one Google Scholar uses — and attempts a plain HTTP GET. This succeeds for OJS journals, SSRN, and JSTOR (with proxy) without launching trafilatura. If that fails, trafilatura extracts article text directly from the HTML body; this works for Wiley, Springer, OUP, and others where the full article is rendered in the page. If trafilatura returns fewer than 1,500 words (abstract-only or paywalled page), the server falls through to `<a>`-tag PDF link scanning. A DOI cross-check guards against publisher redirects that silently return a different article.

**Relevance-ranked results.** `search_papers` results are sorted by a composite score: Zotero membership → open-access availability → cross-source agreement → citation count (log-scaled) → recency. The LLM sees the most actionable papers first.

**Article text cache and section-based access.** After the first successful extraction for any DOI, the full text and section index are written to `<sha256>.article.json` in `PDF_CACHE_DIR`. Subsequent `fetch_fulltext` calls for the same DOI are instant local reads. The `mode` parameter controls what is returned from the cache:

| mode | what it returns |
|---|---|
| `sections` | headings with TF-IDF keywords and character offsets; large gaps auto-filled with keyword-labelled chunks — **call this first** |
| `section` | text of a single section by name (fuzzy-matched) |
| `preview` | abstract + first ~200 words of every other section |
| `range` | raw character slice using `range_start` / `range_end` offsets |
| `full` (default) | entire article text (can be 50,000+ characters — avoid for targeted questions) |

**Recommended workflow:** `fetch_fulltext(doi, mode="sections")` → inspect keyword-enriched headings → `fetch_fulltext(doi, mode="section", section="...")` or `search_in_article(doi, terms=[...])` → `fetch_fulltext(doi, mode="range", ...)` for specific passages. If `mode="sections"` returns ≤ 2 sections or uninformative keywords, try `fetch_fulltext(doi, source="html")` to bypass the PDF cache and fetch a fresh copy of the publisher's article page via the stealth browser — this often yields better section structure from the publisher's `<h2>`/`<h3>` tags. The HTML result updates the cache only if it produces >1,500 words and ≥ 3 parsed sections.

**Source-appropriate section detection.** Section boundaries are derived from the native structural signal of each extraction source rather than post-hoc text pattern matching. The default PDF backend is now **pymupdf4llm**, which produces Markdown output with native table, multi-column, and bold/italic support. Section headings are detected by a custom `hdr_info` callback that reuses the same font-analysis logic described below, so pymupdf4llm benefits from all the heading-detection tuning without relying on its built-in size-only heuristic. For PDF sources, four strategies are tried in order:

- **PDF bookmark / outline tree** (`section_detection: pdf_toc`) — LaTeX documents compiled with `hyperref` (ACM `acmart`, NeurIPS, ICML, ACL, IEEE, Springer LNCS, and virtually all modern templates) automatically embed a PDF bookmark tree mapping `\section`, `\subsection`, `\subsubsection` to a structured outline. PyMuPDF exposes this via `doc.get_toc()`, giving properly spaced titles, correct hierarchy, and page numbers with no font-analysis heuristics. The server uses this when the outline has ≥ 3 entries and ≥ 60% of them can be located in the extracted text. Word-to-document-position matching uses three passes: exact, case-insensitive, and whitespace-collapsed (handles LaTeX PDFs where text extraction produces `"EvaluatingZeroRating..."` but the bookmark title is `"Evaluating Zero Rating..."`). This is also used for Word-origin PDFs with heading-style bookmarks. Confidence: highest.

- **HTML sources** (`section_detection: html_headings`) — `<h2>` / `<h3>` tags are authoritative. Unique markers (`§§SEC:id:level:title§§`) are injected into the HTML before trafilatura runs, then parsed out of the output to give exact character positions without any string matching. Confidence: high.

- **PDF font analysis** (`section_detection: pdf_font_analysis`) — PyMuPDF's `get_text("dict")` returns per-span font metadata. Spans smaller than the dominant body-text size by more than 1 pt are discarded before building the text output — this eliminates footnotes, endnotes, page numbers, and running journal-title headers in one pass. **Bbox-aware span joining** inserts spaces between spans when the horizontal bbox gap exceeds 0.15 em of the font size, fixing the run-together word problem in LaTeX PDFs where words are positioned by x-coordinate rather than literal space characters. A **heading-line pre-scan** (`_precompute_heading_lines`) runs before extraction, recording which lines on every page are composed entirely of bold or large spans with no regular-weight text — this is the key filter that prevents inline bold within paragraphs (which shares a line with regular text) from being misidentified as headings. The heading detector uses four signals on the remaining spans:
  1. **Size-based** — span ≥ 1.5 pt larger than body size. A digit guard runs first to catch page numbers displayed at larger font sizes before they reach the size-based check.
  2. **Bold at body size** — bold flag set, span < 100 chars. Includes a `bold_at_or_near_body` tolerance (±1.5 pt) to catch journals that format headings in a slightly smaller bold font than body text (e.g. 10.5 pt bold headings in an 11.5 pt body). A `[` prefix guard filters out bold citation markers like `[1]`.
  3. **Italic at body size** — >70% of the line's body-size character content is italic, line < 100 chars, ≤ 12 words, does not end with a period (catches italic headings common in humanities journals). A case-citation guard filters out italic legal citations containing `v.` / `vs.` patterns (e.g. "Leander v Sweden").
  4. **Multi-word ALL CAPS** — lines where the text contains a space and is entirely upper-case, catching section headings common in law and humanities journals.

  For **LaTeX PDFs** (detected via `producer`/`creator` metadata — pdfTeX, XeTeX, LuaTeX, MiKTeX, dvips, etc.), font *names* are used for level assignment rather than size clustering: Biolinum Bold → level 2 (ACM `\section`), Libertine Bold → level 3 (ACM `\subsection`), CMBX/LMBX design size ≥ 12 → level 2 else level 3, CMSS/LMSS bold → level 2, Times-based by size differential. For non-LaTeX PDFs, headings are clustered by `(font_size, is_bold, is_italic)` and the largest/boldest group is level 2. Font baseline from first 30 pages. Confidence: reliable.

- **Zotero ft-cache / text fallback** (`section_detection: text_heuristic`) — No structural metadata is available. Conservative multi-pass heuristics:
  1. **Well-known names** — `Conclusion`, `References`, `Appendix`, etc. on isolated lines.
  2. **Roman numeral sections** — `I Introduction`, `III Gender equality and AI` (common in law reviews).
  3. **Numbered sections** — `1. Introduction`, `4.1. TDM and copyright`.
  4. **ALL CAPS isolated lines** — ≤ 80 chars, ≤ 12 words, surrounded by blank or long-paragraph lines.
  5. **Common section names** — preceded by a blank or paragraph line.

  Filters: **population filter** discards undotted numbered groups whose max integer exceeds 30 when dotted candidates also exist; **OCR filter** (see below) additionally discards dotted groups when the max exceeds 15 and structural headings (ALL CAPS, well-known names) are also present; **individual length filter** discards numbered lines whose body exceeds 60 chars; **running-header deduplication** normalises and discards headings appearing 3+ times (2+ for ALL CAPS); **body-text deduplication** discards numbered candidates with identical body text across 3+ entries. Confidence: approximate.

- **Keyword skeleton** (`section_detection: keyword_skeleton`) — When all structural detection fails, the article is split into 20 equal chunks and TF-IDF identifies the 5 most distinctive tokens per chunk. The `sections` mode output becomes a navigational chunk map — letting the LLM request specific ranges via `mode="range"` or `search_in_article` without reading the full text.

**OCR / scanned PDF handling.** PDFs from scanning pipelines (ABBYY FineReader, Tesseract, OmniPage, Adobe Scan, etc.) are detected automatically via producer/creator metadata and font-uniformity analysis (>95% of characters at a single size). When detected, font analysis is skipped entirely (it would produce garbage on uniform-size OCR output) and the text heuristic path is used directly with the OCR-specific footnote filter. Common Unicode ligature characters (ﬁ → fi, ﬂ → fl, etc.) are normalised before detection. The `is_ocr` flag is stored in the cached article's metadata.

**Post-hoc size filtering.** After any detection method runs, sections containing fewer than 50 words are merged into their neighbours (`consolidate_tiny_sections`). This handles footnotes that survive the text heuristic filters by appearing in isolated short blocks. If more than 60% of detected sections remain under 50 words after consolidation (`_majority_tiny`), the entire detection result is discarded and the server falls back to the next method in the cascade — keyword skeleton for the final fallback. This prevents the Brownsword problem (14 footnotes detected as sections, zero real sections found) from producing misleading navigation.

**Batch sections bibliographic metadata.** `batch_sections` displays a one-line citation (`Author et al. (year) — Title. Venue`) above each paper's section listing, drawn from the cached bibliographic metadata. This avoids the need to cross-reference DOIs with search results when surveying multiple papers.

The `sections` mode output includes a `Section detection:` line that tells you which method was used and its reliability. Cached `text_heuristic` entries are automatically re-processed on access so improvements to the heuristic take effect without manual cache clearing.

**pymupdf4llm as default PDF backend.** `pymupdf4llm` is now a main dependency (not optional) and `USE_PYMUPDF4LLM` defaults to `true`. It produces Markdown output with native handling of tables, multi-column layouts, and bold/italic formatting. Section headings are detected by a custom `hdr_info` callback that feeds each span through the same heading-detection logic as the font-analysis pipeline — size-based, bold-at-or-near-body, italic, and multi-word ALL CAPS signals — so pymupdf4llm benefits from all heading-detector tuning without relying on its built-in size-only heuristic. On failure or if the package is not installed, extraction falls back transparently to the existing `extract_text_with_sections` pipeline.

**Lazy cache upgrade.** When `USE_PYMUPDF4LLM` is enabled (the default), cached articles whose `section_detection` is `pdf_font_analysis` or `pdf_toc` are automatically re-extracted with pymupdf4llm the next time they are accessed — provided the original PDF is still on disk in the cache. The upgraded result replaces the old cache entry, so improved section detection takes effect without manual cache clearing.

**Keyword-enriched sections output.** Every `mode="sections"` response includes TF-IDF keywords computed per section — words that are frequent in that section but rare across the article as a whole. Keywords appear on the line below each heading, prefixed with `→`. When section detection is sparse (only 2–3 headings found, leaving large uncovered gaps), the gaps are automatically split into ~3,500-character keyword-labelled chunks interleaved with the structural headings. Keywords for gap chunks are computed locally within each gap so they reflect the concepts actually discussed there. The detection note gains `+ keyword infill` when infill was added. All offsets (structural sections and infill chunks alike) work with `mode="range"`.

**BM25 keyword search within articles.** `search_in_article` builds a BM25 index over overlapping 300-word windows of the cached text. Exact-match snippets are returned with `±context_chars` of surrounding context, match highlighting, character offsets, and section attribution. A lexical dispersion header shows where each term concentrates across 10 equal document segments — the LLM can see at a glance whether a concept appears throughout or only in one section. When no exact matches are found, BM25-ranked windows for semantic proximity are returned instead. This is often the most efficient way to answer a specific question about a paper — more targeted than reading a full section, and it works even when section detection is poor.

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
| `OPENALEX_API_KEY` | *(empty)* | Optional; premium rate limits via `Authorization: Bearer` |
| `GOST_PROXY_URL` | *(empty)* | SOCKS5/HTTP proxy for institutional access |
| `USE_STEALTH_BROWSER` | `true` | Enable Scrapling-based fetching |
| `SCRAPLING_MCP_URL` | *(empty)* | Remote Scrapling MCP server SSE endpoint (see below) |
| `PDF_CACHE_DIR` | `~/.cache/academic-mcp/pdfs` | Where to cache downloaded PDFs |
| `PDF_CACHE_MAX_BYTES` | `2147483648` (2 GB) | Max cache size before LRU eviction kicks in |
| `MAX_CONTEXT_LENGTH` | `100000` | Max characters returned to the LLM |
| `USE_PYMUPDF4LLM` | `true` | Use pymupdf4llm for Markdown PDF extraction (tables, multi-column, bold/italic). Falls back to the legacy pipeline on failure. |

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

### SQLite locking and shadow copy

Zotero uses `PRAGMA locking_mode=EXCLUSIVE` on its database to prevent external writers from causing corruption. This blocks other processes from reading the database at all while Zotero is open — regardless of journal mode, and with no supported way to override it.

The server handles this automatically with a shadow copy:

- **When Zotero is closed**: the server connects to the primary `zotero.sqlite` normally and refreshes a shadow copy at `~/.cache/academic-mcp/zotero-shadow.sqlite` in the background using the SQLite backup API.
- **When Zotero is open**: the primary is locked, so the server falls back silently to the shadow copy. The shadow is at most one "Zotero-closed" cycle out of date, which is fine for search.

The first time you run the server with Zotero closed, the shadow is created automatically — no configuration needed. If you get an error saying the database is locked and no shadow exists, close Zotero and run `refresh_zotero_index` once to create it.

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

## Ex Libris Primo (Institutional Catalogue)

If your institution runs Ex Libris Primo, the server can query it as an additional search source. Primo results are deduplicated against Semantic Scholar and OpenAlex results by DOI. When a paper is only available via your institution's link resolver, the resolver URL is shown directly in the result instead of the generic "may need proxy" message.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `PRIMO_DOMAIN` | *(empty)* | Primo hostname (e.g. `library.example.ac.uk`) |
| `PRIMO_VID` | *(empty)* | View ID — institution-specific (e.g. `44INST:VU2`) |
| `PRIMO_TAB` | `Everything` | Search tab |
| `PRIMO_SEARCH_SCOPE` | `MyInst_and_CI` | Search scope (local + central index) |

Find your `PRIMO_VID` by opening your library's Primo search page and inspecting the URL — it appears as the `vid=` parameter.

### Access

Primo requests are routed through `GOST_PROXY_URL` first (if configured) so that the catalogue returns institutional access metadata. A direct connection is used as fallback if the proxy is unavailable.

Primo is included automatically when `source="all"` (the default). To search only Primo:

```python
search_papers(query="...", source="primo")
```

### Query field prefixes

The `author:` prefix is translated to Primo's `creator,contains` format. `title:` and `subject:` are also mapped. Plain queries use `any,contains`.

## Search Quality

### How results are ranked

`search_papers` queries Zotero, Semantic Scholar, OpenAlex, and Primo (if configured), deduplicates by DOI, then sorts by a composite score:

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
The local server acts as an MCP *client*, connecting over Streamable HTTP to the remote Scrapling MCP server. It discovers available tools at runtime via `list_tools()`, calls `stealthy_fetch` with `{"url": "...", "proxy": "..."}`, and handles two response shapes: if the response contains base64-encoded PDF bytes, they are decoded and cached directly; if it contains HTML, the HTML is passed to the extraction pipeline (`citation_pdf_url` meta tag → trafilatura → `<a>`-tag PDF link scanning) without making a second browser call. When `GOST_PROXY_URL` is configured, it's forwarded in every tool call so the *remote* browser routes through your institution's network. No local Scrapling or Chromium needed.

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
```

## Testing from the Terminal

All tool handlers are importable Python functions, so you can exercise the server directly without running the MCP transport layer. These one-liners are useful for testing a new DOI, debugging extraction, or verifying configuration.

Substitute your own DOIs throughout.

### Check which articles are cached

```bash
uv run python -c "
import glob
from academic_mcp.config import config
from academic_mcp.text_cache import get_cached, _cache_key
import json, os

files = glob.glob(str(config.pdf_cache_dir / '*.article.json'))
print(f'{len(files)} cached articles in {config.pdf_cache_dir}')
for f in sorted(files, key=os.path.getmtime, reverse=True)[:10]:
    data = json.loads(open(f).read())
    print(f'  {data[\"doi\"]}  ({data[\"word_count\"]} words, {len(data[\"sections\"])} sections, {data[\"section_detection\"]})')
"
```

### Fetch an article and list its sections

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_fetch_pdf
r = asyncio.run(_handle_fetch_pdf({'doi': '10.1111/1468-2230.70009', 'mode': 'sections'}))
print(r[0].text)
"
```

Expected output (HTML-extracted Wiley article with keyword infill):
```
Sections for DOI: 10.1111/1468-2230.70009
Source: html_extraction (onlinelibrary.wiley.com)
Section detection: html_headings (high confidence — publisher <h2>/<h3> tags)
============================================================
[0] Abstract  (207 words, chars 776–2,195)
    → private actors, UNGPs, regulatory divergence, post-Brexit, corporate
[1] INTRODUCTION  (614 words, chars 2,195–6,300)
    → Brexit, divergence, due diligence, preventative, common law
[2] CONTEXTUALISING BUSINESS AND HUMAN RIGHTS  (1,122 words, chars 6,300–13,974)
    → UNGPs, Pillar I, state duty, corporate responsibility, supranational
...
[7] CONCLUSION  (1,045 words, chars 47,973–54,843)
    → apathy, corporate accountability, divergence, regulation

→ fetch_fulltext(doi="10.1111/1468-2230.70009", mode="range", range_start=N, range_end=M)
→ search_in_article(doi="10.1111/1468-2230.70009", terms=["keyword"])
```

When section detection is sparse (only 2–3 headings), large gaps are automatically filled with keyword-labelled infill chunks:

```
Section detection: text_heuristic (approximate — regex on plain text) + keyword infill
============================================================
[0] I Introduction  (800 words, chars 0–5,200)
    → Brexit, regulatory, divergence, corporate, human rights
  [1/5] chars 5,200–11,500 (1,000 words): UNGPs, corporate, due diligence, Pillar I
  [2/5] chars 11,500–17,800 (980 words): EU, CSDDD, directive, preventative, regulation
  [3/5] chars 17,800–24,100 (990 words): UK, common law, parent company, supply chain
  [4/5] chars 24,100–30,400 (970 words): Northern Ireland, hybrid, Windsor, alignment
  [5/5] chars 30,400–36,700 (960 words): ESG, scepticism, transnational, legitimacy
[1] V Conclusion  (1,045 words, chars 36,700–42,843)
    → apathy, corporate accountability, divergence, regulation
```

Structural sections are flush-left with named headings and `→ keywords`. Infill chunks are indented with `[n/total]` and keywords inline. All offsets work with `mode="range"`.

### Read a specific section

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_fetch_pdf
r = asyncio.run(_handle_fetch_pdf({
    'doi': '10.1111/1468-2230.70009',
    'mode': 'section',
    'section': 'northern ireland',
}))
print(r[0].text[:800])
print(f'--- total {len(r[0].text):,} chars ---')
"
```

The `section` parameter is fuzzy-matched — `"methods"` matches `"Materials and Methods"`, `"northern ireland"` matches `"NORTHERN IRELAND"`.

### Preview an article (abstract + section stubs)

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_fetch_pdf
r = asyncio.run(_handle_fetch_pdf({'doi': '10.48550/arXiv.2311.08577', 'mode': 'preview'}))
print(r[0].text)
"
```

### Read a character range

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_fetch_pdf
r = asyncio.run(_handle_fetch_pdf({
    'doi': '10.48550/arXiv.2311.08577',
    'mode': 'range',
    'range_start': 12796,
    'range_end': 17000,
}))
print(r[0].text)
"
```

### Fetch full text (first time — hits network/Zotero)

Add `use_proxy=True` to route through `GOST_PROXY_URL` when the paper needs institutional access.

```bash
uv run python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO)
from academic_mcp.server import _handle_fetch_pdf
r = asyncio.run(_handle_fetch_pdf({
    'doi': '10.1177/00323292251396395',
    'use_proxy': True,
    'mode': 'sections',
}))
print(r[0].text)
"
```

### Search within a cached article (BM25 + dispersion)

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_search_in_article
r = asyncio.run(_handle_search_in_article({
    'doi': '10.3138/utlj-2025-0034',
    'terms': ['algorithmic bias', 'surveillance capitalism', 'due diligence'],
    'context_chars': 400,
    'max_matches_per_term': 2,
}))
print(r[0].text)
"
```

Output includes a lexical dispersion bar showing where each term concentrates across the article, followed by annotated snippets with match highlighting and section attribution.

When no exact matches are found, the tool automatically falls back to BM25-ranked windows — useful for multi-word queries where the exact phrase doesn't appear but the concepts do.

### Test keywords for cached sections

```bash
uv run python -c "
from academic_mcp.content_extractor import keywords_for_sections
from academic_mcp.text_cache import get_cached

cached = get_cached('10.1111/1468-2230.70009')
if cached and cached.sections:
    kws = keywords_for_sections(cached.text, cached.sections)
    for sec, kw in zip(cached.sections, kws):
        print(f'{sec[\"title\"][:50]:50s} → {\", \".join(kw)}')
"
```

### Test the text heuristic directly

```bash
uv run python -c "
from academic_mcp.content_extractor import detect_sections_from_text
from academic_mcp.text_cache import get_cached

cached = get_cached('10.3138/utlj-2025-0034')
sections = detect_sections_from_text(cached.text)
print(f'Detected {len(sections)} sections:')
for s in sections:
    print(f'  [{s[\"level\"]}] {s[\"title\"]}  ({s[\"word_count\"]} words)')
"
```

### Test PDF section extraction

Useful when you have a local PDF and want to verify that footnotes and running headers are filtered correctly. The output shows which detection strategy fired (`pdf_toc`, `pdf_font_analysis`, or `text_heuristic`) and whether the document was identified as OCR:

```bash
uv run python -c "
from pathlib import Path
from academic_mcp.pdf_extractor import extract_text_with_sections

result = extract_text_with_sections(Path('/path/to/paper.pdf'))
print(f'Pages: {result[\"pages\"]}  Words: {len(result[\"text\"].split())}')
print(f'Section detection: {result[\"section_detection\"]}')
print(f'Is OCR: {result[\"metadata\"].get(\"is_ocr\", False)}')
print(f'Sections ({len(result[\"sections\"])}):')
for s in result['sections']:
    print(f'  [{s[\"level\"]}] {s[\"title\"]}  (p{s.get(\"page\", \"?\")}, {s[\"word_count\"]} words)')
print()
print(result['text'][:2000])
"
```

For LaTeX PDFs with `hyperref` (ACM, NeurIPS, ICML, etc.), the output will typically show `section_detection: pdf_toc` with clean section titles. For scanned journal articles, `is_ocr: True` and `section_detection: text_heuristic`.

### Search Zotero library

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_search_zotero
r = asyncio.run(_handle_search_zotero({'query': 'corporate accountability due diligence', 'limit': 5}))
print(r[0].text)
"
```

### Look up a paper by DOI in Zotero

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_search_by_doi
r = asyncio.run(_handle_search_by_doi({'doi': '10.1111/1468-2230.70009'}))
print(r[0].text)
"
```

### List all Zotero libraries

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_list_libraries
r = asyncio.run(_handle_list_libraries({}))
print(r[0].text)
"
```

### Refresh the Zotero index and show diagnostics

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_refresh_zotero_index
r = asyncio.run(_handle_refresh_zotero_index({}))
print(r[0].text)
"
```

### Search across all sources (Zotero + S2 + OpenAlex + Primo)

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_search
r = asyncio.run(_handle_search({
    'query': 'platform economy cloud capitalism AI',
    'limit': 5,
    'source': 'all',
}))
print(r[0].text)
"
```

### View available PDF URLs for a DOI

```bash
uv run python -c "
import asyncio
from academic_mcp.server import _handle_find_pdf_urls
r = asyncio.run(_handle_find_pdf_urls({'doi': '10.48550/arXiv.2311.08577'}))
print(r[0].text)
"
```

### Enable detailed logging

Prefix any command with `logging.basicConfig(level=logging.INFO)` to see which retrieval path fires:

```bash
uv run python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
from academic_mcp.server import _handle_fetch_pdf
r = asyncio.run(_handle_fetch_pdf({'doi': '10.1177/00323292251396395', 'mode': 'sections'}))
print(r[0].text)
"
```

Log lines show the retrieval path taken (SQLite → direct HTTP → Scrapling → proxy), shadow copy fallback when Zotero is open, BM25 index builds, and section detection results.