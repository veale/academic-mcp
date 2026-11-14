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


def _ocr(source):
    from . import pdf_extractor

    return pdf_extractor.ocr_text(
        source,
        max_pages=int(os.getenv("OCR_MAX_PAGES", "80")),
        dpi=int(os.getenv("OCR_DPI", "200")),
        language=os.getenv("OCR_LANGUAGES", "eng"),
    )


def needs_ocr(result) -> bool:
    """A scan without a text layer, or text from a broken font encoding."""
    from . import identity

    pages = result.get("pages") or (result.get("metadata") or {}).get("pages")
    pages = pages if isinstance(pages, int) else None
    warnings = identity.assess_content(result.get("text") or "", pages=pages)
    if any(w.startswith(("no_text_layer", "garbled_text")) for w in warnings):
        return True
    words = len((result.get("text") or "").split())
    return bool(pages and pages >= 2 and words < 40 * pages)


def _extract_child(sender, source, markdown, pages, task=None):
    try:
        if task == "ocr":
            sender.send((True, _ocr(source)))
            return
        sender.send((True, _extract(source, markdown, pages)))
    except Exception as exc:
        sender.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


async def extract(source, markdown, pages=None):
    result = await _run(source, markdown, pages, None, float(os.getenv("PDF_PARSE_TIMEOUT_SECONDS", "120")))
    if pages or os.getenv("OCR_ENABLED", "true").lower() in ("0", "false", "no"):
        return result
    if not needs_ocr(result):
        return result
    from . import pdf_extractor

    if not pdf_extractor.ocr_available():
        return result
    try:
        recognised = await _run(source, markdown, None, "ocr", float(os.getenv("OCR_TIMEOUT_SECONDS", "300")))
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("OCR failed for %s: %s", source, exc)
        result["ocr_error"] = str(exc)
        return result
    if len(recognised["text"].split()) <= len((result.get("text") or "").split()):
        return result
    # Positions for PDF highlighting were computed for the old text.
    recognised.pop("_charmap", None)
    return recognised


async def _run(source, markdown, pages, task, timeout):
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(2)
    async with _semaphore:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_extract_child, args=(sender, source, markdown, pages, task), daemon=True
        )
        process.start()
        sender.close()
        timeout = max(0.01, timeout)
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
            what = "OCR" if task == "ocr" else "PDF parsing"
            raise TimeoutError(f"{what} exceeded {timeout:g} seconds") from None
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 2)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join)
            process.close()
