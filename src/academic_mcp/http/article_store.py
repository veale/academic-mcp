"""Sidecar store for article PDF/HTML paths keyed by cache_key.

The text cache (text_cache.py) records extracted text but not the location of
the original PDF or HTML file — those are fetched to content-addressed paths
that can't be derived from the DOI alone.  When fetch_article() returns a
FetchedArticle with pdf_path / html_path, the webapp routes call
store_paths() so that subsequent /article/pdf and /article/html requests can
serve the file without re-running the full fetch pipeline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import config

logger = logging.getLogger(__name__)


def _paths_file(cache_key: str) -> Path:
    return config.pdf_cache_dir / f"{cache_key}.paths.json"


def store_paths(
    cache_key: str,
    pdf_path: str | None,
    html_path: str | None,
) -> None:
    data: dict[str, str] = {}
    if pdf_path:
        data["pdf_path"] = str(pdf_path)
    if html_path:
        data["html_path"] = str(html_path)
    if not data:
        return
    try:
        _paths_file(cache_key).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.debug("Failed to write paths sidecar for %s: %s", cache_key, exc)


def load_paths(cache_key: str) -> dict[str, str]:
    p = _paths_file(cache_key)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Failed to read paths sidecar for %s: %s", cache_key, exc)
        return {}
