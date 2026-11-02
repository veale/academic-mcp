import asyncio

from mcp.types import TextContent
from academic_mcp import server, text_cache
from academic_mcp.core import fetch_jobs


async def test_batch_returns_partial_results_while_slow_fetch_continues(monkeypatch):
    fetch_jobs._jobs.clear()
    monkeypatch.setattr(fetch_jobs, "_semaphore", None)
    monkeypatch.setenv("BATCH_FETCH_WAIT_SECONDS", "0.02")
    articles = {}
    started = []
    article = text_cache.CachedArticle(
        doi="10.1234/fast",
        text="A paper",
        source="web",
        sections=[],
        section_detection="unknown",
        word_count=2,
        metadata={},
    )
    release = asyncio.Event()

    async def fetch(args):
        started.append(args["doi"])
        if args["doi"].endswith("slow"):
            await release.wait()
        articles[args["doi"]] = article
        return [TextContent(type="text", text="ready")]

    monkeypatch.setattr(server, "_handle_fetch_pdf", fetch)
    monkeypatch.setattr(text_cache, "get_cached", lambda doi: articles.get(doi))
    args = {"dois": ["10.1234/fast", "10.1234/slow"]}
    response = await server._handle_batch_sections(args)
    assert "No sections detected" in response[0].text
    assert "still running in the background" in response[0].text
    await server._handle_batch_sections(args)
    assert started.count("10.1234/slow") == 1
    release.set()
    await asyncio.gather(*(job for job, _ in fetch_jobs._jobs.values()))
    response = await server._handle_batch_sections(args)
    assert "still running" not in response[0].text


async def test_job_errors_are_reported_without_losing_other_papers(monkeypatch):
    fetch_jobs._jobs.clear()
    monkeypatch.setattr(fetch_jobs, "_semaphore", None)

    async def fail(args):
        raise RuntimeError("publisher unavailable")

    monkeypatch.setattr(server, "_handle_fetch_pdf", fail)
    monkeypatch.setattr(text_cache, "get_cached", lambda doi: None)
    response = await server._handle_batch_sections({"dois": ["10.1234/failure"]})
    assert "publisher unavailable" in response[0].text
