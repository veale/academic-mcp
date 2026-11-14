"""Checks for navigation protection and bounded tabs in the Mac prototype."""

import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


spec = importlib.util.spec_from_file_location(
    "mac_browser_helper", Path(__file__).parents[1] / "deploy/macos/browser_helper.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class Page:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


class Context:
    def __init__(self, pages):
        self._pages = pages

    @property
    def pages(self):
        return [p for p in self._pages if not p.closed]

    def on(self, event, callback):
        self.callback = callback


def test_proxy_wrapper_preserves_query_and_does_not_nest():
    article = "https://publisher.example/article?a=1&b=two#section"
    wrapped = helper.institutional_url(article)
    assert parse_qs(urlsplit(wrapped).query)["url"] == [article]
    assert helper.institutional_url(wrapped) == wrapped
    proxied = "https://publisher.example.libproxy.ucl.ac.uk/article"
    assert helper.institutional_url(proxied) == proxied


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                    "https://user:password@example.org", "example.org"])
def test_invalid_navigation_rejected(url):
    with pytest.raises(ValueError):
        helper.validate_url(url)


def test_login_protects_tab_from_next_request(tmp_path):
    article = Page("https://login.example/")
    browser = helper.AcquisitionBrowser(Context([article]), tmp_path)
    browser.waiting_for_login = True
    with pytest.raises(ValueError, match="Login is pending"):
        browser.dispatch("open", {"url": "https://other.example/"})
    assert article.url == "https://login.example/"
    assert browser.dispatch("resume", {})["waiting_for_login"] is False


def test_one_article_and_one_popup(tmp_path):
    article = Page()
    context = Context([article])
    browser = helper.AcquisitionBrowser(context, tmp_path)
    first, second = Page(), Page()
    browser.on_page(first)
    browser.on_page(second)
    assert not first.closed
    assert second.closed
    first.close()
    replacement = Page()
    browser.on_page(replacement)
    assert not replacement.closed


def test_restart_preserves_existing_login_popup(tmp_path):
    article, login, excess = Page(), Page("https://login.example/"), Page()
    context = Context([article, login, excess])
    browser = helper.AcquisitionBrowser(context, tmp_path)
    assert browser.popup == login
    assert not login.closed
    assert excess.closed


def test_sleeping_connector_is_woken_without_reloading_article(tmp_path):
    from types import SimpleNamespace

    article = Page("https://publisher.example/article")
    context = Context([article])
    context.service_workers = []
    worker = SimpleNamespace(url="chrome-extension://connector/background.js", evaluate=lambda script: True)
    created = []
    def new_page():
        page = Page()
        page.goto = lambda *args, **kwargs: None
        page.wait_for_timeout = lambda delay: context.service_workers.append(worker)
        created.append(page)
        context._pages.append(page)
        context.callback(page)
        return page
    context.new_page = new_page
    browser = helper.AcquisitionBrowser(context, tmp_path)
    assert browser.worker() is worker
    assert len(created) == 1 and created[0].closed
    assert context.pages == [article]


def test_capture_does_not_claim_full_text(tmp_path):
    article = Page("https://publisher.example/abstract")
    article.content = lambda: "<article>Abstract only</article>"
    article.title = lambda: "An article"
    browser = helper.AcquisitionBrowser(Context([article]), tmp_path)
    result = browser.dispatch("capture", {})
    assert result["completeness"] == "unverified"
    assert Path(result["html"]).read_text() == "<article>Abstract only</article>"


@pytest.mark.parametrize("title,text", [
    ("Just a moment...", ""),
    ("Checking your browser – reCAPTCHA", ""),
    ("Publisher", "Please verify you are human"),
])
def test_challenges_detected(title, text):
    assert helper.challenge_reason(title, text)


def test_article_discussing_captcha_is_not_a_challenge():
    assert helper.challenge_reason("Privacy and accessibility", "We study CAPTCHA systems.") is None


def test_challenge_blocks_navigation_and_saving_until_cleared(tmp_path):
    article = Page("https://publisher.example/")
    article.title = lambda: "Just a moment..."
    article.locator = lambda selector: type("Body", (), {"inner_text": lambda self, **kw: ""})()
    browser = helper.AcquisitionBrowser(Context([article]), tmp_path)
    browser.check_challenge()
    assert browser.waiting_for_login
    with pytest.raises(ValueError, match="browser check is paused"):
        browser.dispatch("open", {"url": "https://other.example/"})
    with pytest.raises(ValueError, match="before saving"):
        browser.dispatch("save-zotero", {})
    with pytest.raises(ValueError, match="before saving or translating"):
        browser.dispatch("inspect-zotero", {})
    # Resume must not override a challenge that is still present.
    assert browser.dispatch("resume", {})["waiting_for_login"]
    article.title = lambda: "The full article"
    assert not browser.dispatch("resume", {})["waiting_for_login"]


def test_direct_launch_has_no_automation_flags(tmp_path, monkeypatch):
    from argparse import Namespace

    calls = []
    def start(command, **kwargs):
        calls.append(command)
        return Namespace(pid=123)
    monkeypatch.setattr(helper.subprocess, "Popen", start)
    helper.launch_chrome(Namespace(state=tmp_path, proxy="socks5://127.0.0.1:1080",
                                   control=False, url="https://publisher.example/"))
    command = calls[0]
    assert not any("automation" in arg or "remote-debugging" in arg for arg in command)
    assert "--proxy-server=socks5://127.0.0.1:1080" in command
    assert not any("headless" in arg for arg in command)


def test_control_launch_is_loopback_and_stale_lock_is_left_to_chrome(tmp_path, monkeypatch):
    from argparse import Namespace

    profile = tmp_path / "profile"
    profile.mkdir()
    lock = profile / "SingletonLock"
    lock.symlink_to("host-12345")
    def dead_owner(pid, signal):
        raise ProcessLookupError
    monkeypatch.setattr(helper.os, "kill", dead_owner)
    calls = []
    def start(command, **kwargs):
        calls.append(command)
        return Namespace(pid=123)
    monkeypatch.setattr(helper.subprocess, "Popen", start)
    helper.launch_chrome(Namespace(state=tmp_path, proxy="socks5://127.0.0.1:1080",
                                   control=True, url="https://publisher.example/"))
    assert lock.is_symlink()
    assert "--remote-debugging-address=127.0.0.1" in calls[0]
    port = next(arg for arg in calls[0] if arg.startswith("--remote-debugging-port="))
    assert int(port.split("=")[1]) > 0


def test_running_profile_is_not_launched_twice(tmp_path, monkeypatch):
    from argparse import Namespace

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("host-12345")
    monkeypatch.setattr(helper.os, "kill", lambda *args: None)
    with pytest.raises(ValueError, match="Close this dedicated Chrome profile"):
        helper.launch_chrome(Namespace(state=tmp_path))
