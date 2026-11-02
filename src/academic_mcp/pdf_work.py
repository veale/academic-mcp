"""Bounded, isolated PDF parsing without blocking MCP or retaining hung workers.

PyMuPDF is not thread safe. Each extraction gets a process and a hard deadline;
client cancellation also terminates that process and releases its worker slot.
"""

import asyncio
import multiprocessing
import os
from pathlib import Path

_semaphore = None


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


def _extract_child(sender, source, markdown, pages):
    try:
        sender.send((True, _extract(source, markdown, pages)))
    except Exception as exc:
        sender.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


async def extract(source, markdown, pages=None):
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(2)
    async with _semaphore:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_extract_child, args=(sender, source, markdown, pages), daemon=True
        )
        process.start()
        sender.close()
        timeout = max(0.01, float(os.getenv("PDF_PARSE_TIMEOUT_SECONDS", "120")))
        try:
            async with asyncio.timeout(timeout):
                while not receiver.poll():
                    if not process.is_alive():
                        raise RuntimeError(
                            f"PDF worker exited unexpectedly ({process.exitcode})"
                        )
                    await asyncio.sleep(0.02)
                ok, result = await asyncio.to_thread(receiver.recv)
                if not ok:
                    raise RuntimeError("PDF extraction failed: " + result)
                return result
        except TimeoutError:
            raise TimeoutError(f"PDF parsing exceeded {timeout:g} seconds") from None
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 2)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join)
            process.close()
