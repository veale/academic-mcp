import json
from types import SimpleNamespace

import pytest

from academic_mcp import server


@pytest.mark.asyncio
async def test_compat_search_returns_structured_citable_results(monkeypatch):
    hits = [
        SimpleNamespace(doi="10.1234/ABC", url=None, title="A paper"),
        SimpleNamespace(doi=None, url="https://example.test/report", title="A report"),
        SimpleNamespace(doi=None, url=None, title="Uncitable item"),
    ]

    async def fake_collect(args, diagnostics=None):
        return hits

    monkeypatch.setattr(server, "_collect_search_results", fake_collect)
    result = await server._handle_compat_search({"query": "test"})

    assert result.structuredContent["results"][0] == {
        "id": "doi:10.1234/abc",
        "title": "A paper",
        "url": "https://doi.org/10.1234/abc",
    }
    assert len(result.structuredContent["results"]) == 2
    assert json.loads(result.content[0].text) == result.structuredContent


@pytest.mark.asyncio
async def test_compat_search_empty_query_does_not_search(monkeypatch):
    async def should_not_run(*args, **kwargs):
        raise AssertionError("search should not run")

    monkeypatch.setattr(server, "_collect_search_results", should_not_run)
    result = await server._handle_compat_search({"query": "  "})
    assert result.structuredContent == {"results": []}
