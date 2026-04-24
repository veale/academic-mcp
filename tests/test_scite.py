"""Tests for the Scite client module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp import scite  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_scite_cache():
    scite._tally_cache.clear()
    yield
    scite._tally_cache.clear()


def _mock_response(status_code: int, json_body):
    resp = AsyncMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = lambda: json_body
    return resp


async def test_normalize_doi_strips_scheme_and_lowercases():
    assert scite._normalize_doi("HTTPS://doi.org/10.1234/ABC") == "10.1234/abc"


async def test_get_scite_tallies_parses_counts(monkeypatch):
    payload = {
        "supporting": 5,
        "contradicting": 2,
        "mentioning": 10,
        "citingPublications": 17,
        "total": 17,
    }

    class FakeClient:
        async def get(self, url, headers=None):
            assert "10.1234/abc" in url
            return _mock_response(200, payload)

    result = await scite.get_scite_tallies("10.1234/abc", client=FakeClient())
    assert result is not None
    assert result["supporting"] == 5
    assert result["contrasting"] == 2  # note key rename
    assert result["mentioning"] == 10
    assert result["citing"] == 17
    assert result["total"] == 17
    assert result["retracted"] is False

    # Second call hits the TTL cache — no further HTTP.
    class RaisingClient:
        async def get(self, *a, **kw):
            raise AssertionError("cache miss: should not re-request")

    cached = await scite.get_scite_tallies("10.1234/abc", client=RaisingClient())
    assert cached == result


async def test_get_scite_tallies_returns_none_on_http_error():
    class FakeClient:
        async def get(self, *a, **kw):
            return _mock_response(500, {})

    assert await scite.get_scite_tallies("10.1234/xyz", client=FakeClient()) is None


def test_paper_has_retraction_notice_detects_keywords():
    assert scite.paper_has_retraction_notice(
        {"editorialNotices": [{"type": "retraction", "label": "Retracted"}]}
    )
    assert scite.paper_has_retraction_notice(
        {"editorialNotices": [{"description": "This article has been withdrawn."}]}
    )
    assert not scite.paper_has_retraction_notice(None)
    assert not scite.paper_has_retraction_notice({})
    assert not scite.paper_has_retraction_notice({"editorialNotices": []})
    assert not scite.paper_has_retraction_notice(
        {"editorialNotices": [{"type": "comment", "label": "Response"}]}
    )


async def test_batch_deduplicates_and_calls_once_per_doi():
    calls: list[str] = []

    async def fake_tallies(doi, client=None):
        calls.append(doi)
        return {
            "doi": doi,
            "supporting": 1,
            "contrasting": 0,
            "mentioning": 0,
            "citing": 1,
            "total": 1,
            "retracted": False,
        }

    with patch.object(scite, "get_scite_tallies", side_effect=fake_tallies):
        out = await scite.get_scite_tallies_batch(
            ["10.1/A", "10.1/B", "10.1/A"],  # duplicate
        )

    assert set(out.keys()) == {"10.1/a", "10.1/b"}
    assert calls.count("10.1/a") == 1
    assert calls.count("10.1/b") == 1
