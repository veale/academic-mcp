"""Configuration loaded from environment / .env file."""

import logging
import os
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class Config:
    # ── External API keys ────────────────────────────────────────────
    unpaywall_email: str = field(
        default_factory=lambda: os.getenv("UNPAYWALL_EMAIL", "")
    )
    semantic_scholar_api_key: str = field(
        default_factory=lambda: os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    )
    openalex_api_key: str = field(
        default_factory=lambda: os.getenv("OPENALEX_API_KEY", "")
    )

    # ── Ex Libris Primo ──────────────────────────────────────────────
    primo_domain: str = field(
        default_factory=lambda: os.getenv("PRIMO_DOMAIN", "")
    )
    primo_vid: str = field(
        default_factory=lambda: os.getenv("PRIMO_VID", "")
    )
    primo_tab: str = field(
        default_factory=lambda: os.getenv("PRIMO_TAB", "Everything")
    )
    primo_search_scope: str = field(
        default_factory=lambda: os.getenv("PRIMO_SEARCH_SCOPE", "MyInst_and_CI")
    )

    # ── GOST proxy (institutional access) ────────────────────────────
    # Passed to httpx for proxied fetches *and* forwarded to the remote
    # Scrapling MCP server / local StealthyFetcher as the ``proxy`` arg.
    gost_proxy_url: str = field(
        default_factory=lambda: os.getenv("GOST_PROXY_URL", "")
    )

    # ── Stealth browser ──────────────────────────────────────────────
    use_stealth_browser: bool = field(
        default_factory=lambda: os.getenv("USE_STEALTH_BROWSER", "true").lower()
        in ("true", "1", "yes")
    )

    # Remote Scrapling MCP server (``scrapling mcp --http``).
    # When set, the fetcher acts as an MCP *client* over SSE and calls
    # the remote tool instead of launching a local Chromium instance.
    # Example: http://192.168.1.50:8000/sse
    scrapling_mcp_url: str = field(
        default_factory=lambda: os.getenv("SCRAPLING_MCP_URL", "")
    )

    # ── PDF cache ────────────────────────────────────────────────────
    pdf_cache_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("PDF_CACHE_DIR", "~/.cache/academic-mcp/pdfs")
        ).expanduser()
    )
    pdf_cache_max_bytes: int = field(
        default_factory=lambda: int(
            os.getenv("PDF_CACHE_MAX_BYTES", str(2 * 1024 * 1024 * 1024))
        )
    )

    # ── Context limits ───────────────────────────────────────────────
    max_context_length: int = field(
        default_factory=lambda: int(os.getenv("MAX_CONTEXT_LENGTH", "100000"))
    )

    # ── Auto-import to Zotero ─────────────────────────────────────────
    # When enabled, PDFs fetched from the web (not from Zotero) are
    # automatically added to the local Zotero library with full metadata.
    # Requires Zotero desktop to be running (local API at localhost:23119).
    # The .article.json text cache is kept; only the PDF moves to Zotero.
    auto_import_to_zotero: bool = field(
        default_factory=lambda: os.getenv("AUTO_IMPORT_TO_ZOTERO", "false").lower()
        in ("true", "1", "yes")
    )

    # ── CORE.ac.uk (OA paper aggregator, 300M+ papers) ───────────────
    core_api_key: str = field(
        default_factory=lambda: os.getenv("CORE_API_KEY", "")
    )

    # ── Web search fallback ──────────────────────────────────────────
    # Serper.dev: Google results. 2,500 free queries (no CC), then $1/1K.
    serper_api_key: str = field(
        default_factory=lambda: os.getenv("SERPER_API_KEY", "")
    )
    # Brave Search: Own index. $5/mo free credits (~1K queries).
    brave_search_api_key: str = field(
        default_factory=lambda: os.getenv("BRAVE_SEARCH_API_KEY", "")
    )

    # ── SSRN authenticated access ────────────────────────────────────
    # JSON array of cookies exported from Firefox.
    ssrn_cookies: str = field(
        default_factory=lambda: os.getenv("SSRN_COOKIES", "")
    )

    # ── Semantic index provider ──────────────────────────────────────
    # Controls which embedder is used to build and query the Zotero
    # semantic index. In every case, vectors are stored locally (Chroma)
    # and query-time ANN runs locally — cloud providers only compute the
    # embedding for the text being indexed or for the query string.
    #
    #   SEMANTIC_PROVIDER   local | openai | gemini   (default: local)
    #   SEMANTIC_MODEL      provider-specific model name; any string the
    #                       provider accepts (see README for suggestions).
    #                       Defaults:
    #                         local  -> all-MiniLM-L6-v2
    #                         openai -> text-embedding-3-small
    #                         gemini -> gemini-embedding-001
    semantic_provider: str = field(
        default_factory=lambda: os.getenv("SEMANTIC_PROVIDER", "local").lower()
    )
    semantic_model: str = field(
        default_factory=lambda: os.getenv("SEMANTIC_MODEL", "").strip()
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    gemini_api_key: str = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )

    # ── Local llama-server (OpenAI-compatible) ───────────────────────
    # When SEMANTIC_PROVIDER=openai and OPENAI_BASE_URL is set, the embedding
    # client talks to a local llama-server instead of api.openai.com.
    # No API key validation is enforced when the base URL is localhost.
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "").strip()
    )

    # ── Bulk-mode embedding endpoint (optional) ───────────────────────
    # When SemanticIndex.sync() runs (the bulk path: full reindex or large
    # incremental top-up), the OpenAI client may be redirected to a
    # different endpoint with these overrides.  Search and incremental
    # single-item embedding (interactive path) always use the regular
    # OPENAI_BASE_URL / OPENAI_API_KEY.
    #
    # Typical use: cloud bulk + local interactive.  Set BULK_OPENAI_BASE_URL
    # to a cloud provider (DeepInfra, Together, OpenAI) for a fast one-time
    # backfill, leave OPENAI_BASE_URL pointing at your local llama-server
    # for ongoing low-latency queries.  SEMANTIC_MODEL must be the same on
    # both endpoints — the vectors must be in one space or the index is
    # poisoned.
    #
    # Any unset BULK_* var falls back to the corresponding OPENAI_* value,
    # so leaving these blank reproduces the prior single-endpoint behaviour.
    bulk_openai_base_url: str = field(
        default_factory=lambda: os.getenv("BULK_OPENAI_BASE_URL", "").strip()
    )
    bulk_openai_api_key: str = field(
        default_factory=lambda: os.getenv("BULK_OPENAI_API_KEY", "")
    )
    # When true (default), the interactive/query path also falls back to the
    # BULK_OPENAI_* endpoint when OPENAI_BASE_URL / OPENAI_API_KEY are blank.
    # Set to false to keep interactive queries strictly on the non-bulk endpoint.
    bulk_openai_fallback_query: bool = field(
        default_factory=lambda: os.getenv("BULK_OPENAI_FALLBACK_QUERY", "true").lower()
        in ("true", "1", "yes")
    )

    # ── Cross-encoder reranker (applied to semantic_search_zotero) ────
    # Model ID for sentence-transformers CrossEncoder. Empty string disables
    # reranking entirely (retrieval order returned unchanged).
    cross_reranker_model: str = field(
        default_factory=lambda: os.getenv("CROSS_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    )
    # Number of chunks to retrieve from Chroma BEFORE reranking.
    # semantic_search_zotero(k=10) fetches cross_reranker_fetch=50 chunks,
    # reranks them with the cross-encoder, and returns the top 10.
    cross_reranker_fetch: int = field(
        default_factory=lambda: int(os.getenv("CROSS_RERANKER_FETCH", "50"))
    )

    # ── PDF extraction backend ───────────────────────────────────────
    # When true, use pymupdf4llm for Markdown extraction (tables, multi-column,
    # bold/italic). Falls back to extract_text_with_sections on failure or if
    # the package is not installed. Default: false (existing pipeline).
    use_pymupdf4llm: bool = field(
        default_factory=lambda: os.getenv("USE_PYMUPDF4LLM", "true").lower()
        in ("true", "1", "yes")
    )

    def __post_init__(self):
        self.pdf_cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def proxy_dict(self) -> dict | None:
        """Return proxy config for httpx if GOST proxy is set."""
        if self.gost_proxy_url:
            return {
                "http://": self.gost_proxy_url,
                "https://": self.gost_proxy_url,
            }
        return None

    # ------------------------------------------------------------------
    # LRU cache eviction
    # ------------------------------------------------------------------

    def evict_cache_lru(self) -> None:
        """Delete the oldest cached files until the directory is under the
        size cap.

        Only touches ``*.pdf``, ``*.tmp``, and ``*.zip.tmp`` — never the
        DOI index JSON or other metadata files.  Files are sorted by
        modification time (oldest first) so recently-used papers survive.
        """
        evictable_suffixes = {".pdf", ".tmp", ".article.json"}
        try:
            entries = []
            total = 0
            for f in self.pdf_cache_dir.iterdir():
                if not f.is_file():
                    continue
                if f.suffix not in evictable_suffixes:
                    continue
                size = f.stat().st_size
                mtime = f.stat().st_mtime
                entries.append((mtime, size, f))
                total += size

            if total <= self.pdf_cache_max_bytes:
                return

            entries.sort(key=lambda e: e[0])
            freed = 0
            target = total - self.pdf_cache_max_bytes
            for mtime, size, path in entries:
                try:
                    path.unlink()
                    freed += size
                    if freed >= target:
                        break
                except OSError:
                    pass

            logger.info(
                "Cache eviction: freed %.1f MB (was %.1f MB, cap %.1f MB)",
                freed / (1024 * 1024),
                total / (1024 * 1024),
                self.pdf_cache_max_bytes / (1024 * 1024),
            )
        except OSError as e:
            logger.debug("Cache eviction scan failed: %s", e)


config = Config()
