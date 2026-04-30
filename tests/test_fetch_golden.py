"""Golden/snapshot tests for fetch_article output.

Run once with GENERATE_GOLDENS=1 to create the golden files from the current
(pre-migration) code.  Thereafter every commit in the migration chain must
keep these tests passing.

Usage:
    GENERATE_GOLDENS=1 pytest tests/test_fetch_golden.py -v  # regenerate
    pytest tests/test_fetch_golden.py -v                     # assert
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

GOLDEN_DIR = Path(__file__).parent / "golden"
GENERATE = os.environ.get("GENERATE_GOLDENS") == "1"

# ---------------------------------------------------------------------------
# Fixture data — fixed text + sections for deterministic TF-IDF output
# ---------------------------------------------------------------------------

# HTML article fixture — 4 sections, html_headings detection.
# Text is kept short (no gap > 4000 chars) so infill_keyword_chunks produces
# no infill entries.
_HTML_PARAS = [
    (
        "This paper presents a novel approach to deep learning for natural language "
        "processing. We introduce a transformer architecture with improved attention "
        "mechanisms. Results demonstrate significant performance gains on benchmark "
        "datasets. The proposed method is evaluated on GLUE and SQuAD benchmarks."
    ),
    (
        "Prior work on language models relied on recurrent architectures. Our method "
        "addresses the gradient vanishing problem common in deep networks. The "
        "introduction of multi-head attention enables parallel sequence processing. "
        "We build on BERT and GPT pre-training paradigms for downstream adaptation."
    ),
    (
        "The model employs stacked transformer encoder layers with residual connections. "
        "Gradient descent with Adam optimiser updates parameters over one hundred "
        "training epochs. Dropout regularisation prevents overfitting on small training "
        "datasets. Hyperparameter tuning uses a validation set reserved from the corpus."
    ),
    (
        "Experimental results show 94.2 F1 on SQuAD and 88.1 average score on GLUE. "
        "Ablation studies confirm each architectural component contributes to accuracy. "
        "Our method outperforms the baseline by 3.4 points on the SuperGLUE benchmark. "
        "Performance improvements are consistent across all language understanding tasks."
    ),
]

_HTML_TEXT = "\n\n".join(_HTML_PARAS)
_HTML_DOI = "10.9999/html.test"
_HTML_META = {
    "title": "Improved Transformer Architecture for NLP",
    "authors": ["Alice Smith", "Bob Jones"],
    "year": "2024",
    "venue": "Journal of Machine Learning Research",
}

# Compute precise section boundaries
_html_sections: list[dict] = []
_pos = 0
_SEC_NAMES = ["Abstract", "Introduction", "Methods", "Results"]
for _i, (_name, _para) in enumerate(zip(_SEC_NAMES, _HTML_PARAS)):
    _sep = "\n\n" if _i < len(_HTML_PARAS) - 1 else ""
    _end = _pos + len(_para)
    _html_sections.append({
        "title": _name,
        "level": 2,
        "start": _pos,
        "end": _end,
        "word_count": len(_para.split()),
    })
    _pos = _end + len(_sep)

# PDF article fixture — 3 sections, pdf_font_analysis detection.
_PDF_PARAS = [
    (
        "We present a Bayesian framework for uncertainty quantification in neural networks. "
        "The method combines variational inference with deep learning architectures. "
        "Uncertainty estimates are validated on regression and classification problems."
    ),
    (
        "Variational dropout approximates Bayesian inference in neural networks. "
        "Monte Carlo sampling generates uncertainty estimates from posterior distributions. "
        "The framework scales to large datasets using stochastic gradient optimisation."
    ),
    (
        "Benchmarks include UCI regression datasets and CIFAR image classification tasks. "
        "Calibration curves confirm well-calibrated uncertainty across all test conditions. "
        "The Bayesian approach reduces overconfident predictions on out-of-distribution inputs."
    ),
]

_PDF_TEXT = "\n\n".join(_PDF_PARAS)
_PDF_DOI = "10.9999/pdf.test"
_PDF_META = {
    "title": "Bayesian Deep Learning for Uncertainty Quantification",
    "authors": ["Charlie Brown", "Diana Prince", "Eve Stone"],
    "year": "2023",
    "venue": "NeurIPS 2023",
}

_pdf_sections: list[dict] = []
_pos = 0
_PDF_SEC_NAMES = ["Abstract", "Methods", "Results"]
for _i, (_name, _para) in enumerate(zip(_PDF_SEC_NAMES, _PDF_PARAS)):
    _sep = "\n\n" if _i < len(_PDF_PARAS) - 1 else ""
    _end = _pos + len(_para)
    _pdf_sections.append({
        "title": _name,
        "level": 2,
        "start": _pos,
        "end": _end,
        "word_count": len(_para.split()),
    })
    _pos = _end + len(_sep)

_FAIL_DOI = "10.9999/fail.test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cached(doi, text, source, sections, section_detection, metadata):
    from academic_mcp.text_cache import CachedArticle
    return CachedArticle(
        doi=doi,
        text=text,
        source=source,
        sections=sections,
        section_detection=section_detection,
        word_count=len(text.split()),
        metadata=metadata,
        cached_at="2024-01-01T00:00:00+00:00",
    )


def _golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"fetch_{name}.txt"


def _check_or_save(name: str, actual: str) -> None:
    path = _golden_path(name)
    if GENERATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        print(f"\n  [GOLDEN] wrote {path.name}")
        return
    assert path.exists(), (
        f"Golden file missing: {path.name}\n"
        "Run with GENERATE_GOLDENS=1 to create it."
    )
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"Golden mismatch for '{name}'.\n"
        "Run with GENERATE_GOLDENS=1 to regenerate if the change is intentional."
    )


async def _call_fetch(args: dict, cached) -> str:
    """Call fetch_article with the given cached article injected into the cache."""
    from academic_mcp.core import fetch as core_fetch
    from academic_mcp import text_cache

    with patch.object(text_cache, "get_cached", return_value=cached), \
         patch("academic_mcp.zotero_import.get_auto_import_hint", return_value=None), \
         patch("academic_mcp.config.config.use_pymupdf4llm", False):
        result = await core_fetch.fetch_article(args)
    return result.text


# ---------------------------------------------------------------------------
# HTML article — modes: sections, section, preview, range, full
# ---------------------------------------------------------------------------

@pytest.fixture
def html_cached():
    return _make_cached(
        doi=_HTML_DOI,
        text=_HTML_TEXT,
        source="html_extraction (trafilatura)",
        sections=_html_sections,
        section_detection="html_headings",
        metadata=_HTML_META,
    )


async def test_html_sections(html_cached):
    actual = await _call_fetch({"doi": _HTML_DOI, "mode": "sections"}, html_cached)
    _check_or_save("html_sections", actual)


async def test_html_section(html_cached):
    actual = await _call_fetch(
        {"doi": _HTML_DOI, "mode": "section", "section": "Methods"},
        html_cached,
    )
    _check_or_save("html_section", actual)


async def test_html_preview(html_cached):
    actual = await _call_fetch({"doi": _HTML_DOI, "mode": "preview"}, html_cached)
    _check_or_save("html_preview", actual)


async def test_html_range(html_cached):
    actual = await _call_fetch(
        {"doi": _HTML_DOI, "mode": "range", "range_start": 50, "range_end": 300},
        html_cached,
    )
    _check_or_save("html_range", actual)


async def test_html_full(html_cached):
    actual = await _call_fetch({"doi": _HTML_DOI, "mode": "full"}, html_cached)
    _check_or_save("html_full", actual)


# ---------------------------------------------------------------------------
# PDF article — modes: sections, preview, full
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf_cached():
    return _make_cached(
        doi=_PDF_DOI,
        text=_PDF_TEXT,
        source="zotero_local",
        sections=_pdf_sections,
        section_detection="pdf_font_analysis",
        metadata=_PDF_META,
    )


async def test_pdf_sections(pdf_cached):
    actual = await _call_fetch({"doi": _PDF_DOI, "mode": "sections"}, pdf_cached)
    _check_or_save("pdf_sections", actual)


async def test_pdf_preview(pdf_cached):
    actual = await _call_fetch({"doi": _PDF_DOI, "mode": "preview"}, pdf_cached)
    _check_or_save("pdf_preview", actual)


async def test_pdf_full(pdf_cached):
    actual = await _call_fetch({"doi": _PDF_DOI, "mode": "full"}, pdf_cached)
    _check_or_save("pdf_full", actual)


# ---------------------------------------------------------------------------
# Failure path — all sources fail, synthetic DOI
# ---------------------------------------------------------------------------

async def test_failure_all_sources():
    from academic_mcp.core import fetch as core_fetch
    from academic_mcp import text_cache

    with patch.object(text_cache, "get_cached", return_value=None), \
         patch("academic_mcp.zotero.get_paper_from_zotero", new=AsyncMock(return_value=None)), \
         patch("academic_mcp.apis.s2_paper", new=AsyncMock(side_effect=Exception("mock fail"))), \
         patch("academic_mcp.apis.openalex_work", new=AsyncMock(side_effect=Exception("mock fail"))), \
         patch("academic_mcp.apis.unpaywall_lookup", new=AsyncMock(side_effect=Exception("mock fail"))), \
         patch("academic_mcp.apis.collect_pdf_urls", return_value=[]), \
         patch("academic_mcp.config.config.use_stealth_browser", False), \
         patch("academic_mcp.config.config.core_api_key", ""), \
         patch("academic_mcp.config.config.serper_api_key", ""), \
         patch("academic_mcp.config.config.brave_search_api_key", ""), \
         patch("academic_mcp.config.config.ssrn_cookies", ""), \
         patch("academic_mcp.config.config.gost_proxy_url", ""):
        result = await core_fetch.fetch_article({"doi": _FAIL_DOI})

    _check_or_save("failure", result.text)
