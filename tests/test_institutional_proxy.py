"""Institutional routing must respect the requested browser route."""
from unittest.mock import AsyncMock

from academic_mcp import pdf_fetcher
from academic_mcp.core import fetch
from academic_mcp.core.types import ArticleId


def test_browser_proxy_is_opt_in(monkeypatch):
    monkeypatch.setattr(pdf_fetcher.config, 'gost_proxy_url', 'socks5://campus:1080')
    direct = pdf_fetcher._build_tool_args('https://publisher.example', 'stealthy_fetch')
    campus = pdf_fetcher._build_tool_args('https://publisher.example', 'stealthy_fetch', use_proxy=True)
    assert 'proxy' not in direct
    assert campus['proxy'] == 'socks5://campus:1080'


async def test_landing_page_passes_proxy_to_browser(monkeypatch):
    monkeypatch.setattr(fetch.config, 'use_stealth_browser', True)
    browser = AsyncMock(return_value=(None, None, None))
    monkeypatch.setattr(pdf_fetcher, 'fetch_with_scrapling', browser)
    await fetch._extract_from_landing_page('https://publisher.example', True)
    browser.assert_awaited_once_with('https://publisher.example', use_proxy=True)


async def test_url_pdf_uses_institution_after_direct_failure(monkeypatch):
    monkeypatch.setattr(fetch.config, 'gost_proxy_url', 'socks5://campus:1080')
    monkeypatch.setattr(fetch.config, 'institutional_proxy_auto', True)
    monkeypatch.setattr(fetch.config, 'use_stealth_browser', False)
    monkeypatch.setattr(fetch.text_cache, 'get_cached', lambda doi: None)
    monkeypatch.setattr(pdf_fetcher, 'fetch_direct', AsyncMock(return_value=None))
    proxied = AsyncMock(return_value=None)
    monkeypatch.setattr(pdf_fetcher, 'fetch_proxied', proxied)
    await fetch.fetch_article(ArticleId(url='https://university.example/paper.pdf'))
    proxied.assert_awaited_once_with('https://university.example/paper.pdf')
