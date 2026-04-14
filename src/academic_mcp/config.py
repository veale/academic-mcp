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
        evictable_suffixes = {".pdf", ".tmp"}
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
