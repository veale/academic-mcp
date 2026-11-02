"""Bounded process workers keep PDF parsing off the MCP event loop.

PyMuPDF is not thread safe, so separate processes handle independent PDFs.
"""

import asyncio
import atexit
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_pool = None


def _extract(source, markdown, pages=None):
    from . import pdf_extractor

    if pages:
        start, *end = pages.split("-")
        return {
            "text": pdf_extractor.extract_text_by_pages(
                source, int(start), int(end[0]) if end else int(start)
            )
        }
    result = (
        pdf_extractor.extract_text_pymupdf4llm(source)
        if markdown
        else pdf_extractor.extract_text_with_sections(source)
    )
    if isinstance(source, Path) and not markdown:
        try:
            result["_charmap"] = pdf_extractor.build_charmap_bytes(
                source, result["text"]
            )
        except Exception:
            pass
    return result


async def extract(source, markdown, pages=None):
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=2, mp_context=multiprocessing.get_context("spawn")
        )
        atexit.register(lambda: _pool.shutdown(wait=False, cancel_futures=True))
    return await asyncio.get_running_loop().run_in_executor(
        _pool, _extract, source, markdown, pages
    )
