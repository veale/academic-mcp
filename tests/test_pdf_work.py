"""Exercise the actual process boundary with a small real PDF."""

import asyncio
import pymupdf
from academic_mcp import pdf_work

# Capture the production function before conftest substitutes mock-friendly
# inline execution for extraction golden tests.
process_extract = pdf_work.extract


async def test_real_pdf_process_worker_returns_text_without_blocking_loop(tmp_path):
    path = tmp_path / "paper.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "A real PDF worker test for privacy research.")
        doc.save(path)
    task = asyncio.create_task(process_extract(path, False))
    heartbeats = 0
    while not task.done():
        await asyncio.sleep(0.01)
        heartbeats += 1
    result = await task
    assert "privacy research" in result["text"]
    assert heartbeats > 0
