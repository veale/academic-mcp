"""Academic must handle current and legacy Scrapling without losing metadata."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from academic_mcp import pdf_fetcher


async def test_current_browser_tool_is_preferred():
    session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[
        SimpleNamespace(name="browser_open"), SimpleNamespace(name="browser_fetch_once"),
        SimpleNamespace(name="stealthy_fetch")
    ])))
    assert await pdf_fetcher._discover_tool(session) == "browser_fetch_once"


async def test_unrelated_first_tool_is_never_used_as_fetch():
    session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="open_session")])))
    assert await pdf_fetcher._discover_tool(session) is None


def test_full_html_keeps_citation_metadata_and_uses_browser_units():
    args = pdf_fetcher._build_tool_args("https://publisher.example/article", "browser_fetch_once")
    assert args["extraction_type"] == "html"
    assert args["main_content_only"] is False
    assert args["timeout"] == 45000
    assert args["solve_cloudflare"] is True
    assert pdf_fetcher._build_tool_args("https://publisher.example", "make_request")["timeout"] == 30


def test_empty_response_is_not_misreported_as_html():
    html, url = pdf_fetcher._unwrap_scrapling_response('{"status":200,"content":[],"url":"https://example.test/"}')
    assert html == ""
    assert url == "https://example.test/"


def test_structured_wrapper_and_failure_status():
    html, url = pdf_fetcher._unwrap_scrapling_response('{"result":{"status":200,"content":["<html><head></head><body>Article</body></html>"],"url":"https://example.test/"}}')
    assert "<head>" in html
    assert url == "https://example.test/"
    assert pdf_fetcher._unwrap_scrapling_response('{"status":403,"content":["Access denied"]}') == ("", "")
