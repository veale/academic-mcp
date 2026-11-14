"""Mac state machine checks; no real browser, notifications or library writes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parents[1] / "deploy/macos"
spec = importlib.util.spec_from_file_location("acquisition_worker", HERE / "acquisition_worker.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    (tmp_path / "worker.json").write_text(json.dumps({"server": "http://localhost:1", "token": "test", "worker": "test"}))
    page = SimpleNamespace(url="https://publisher.example/article", content=lambda: "<article>Full HTML</article>",
                           evaluate=lambda script: {"title": "Article", "doi": "10.1234/test", "url": "https://publisher.example/article"})
    calls = []
    def dispatch(action, data):
        calls.append(action)
        if action == "inspect-zotero":
            return [{"items": [{"title": "Article", "doi": "10.1234/test", "attachments": [
                {"url": "https://publisher.example/pdf", "mimeType": "application/pdf"}]}]}]
    browser = SimpleNamespace(article=page, waiting_for_login=False, pause_reason=None,
                              check_challenge=lambda: None, dispatch=dispatch)
    result = module.AcquisitionWorker(browser, tmp_path)
    result.calls = calls
    result.settle = lambda: None
    result.reports = []
    result.report = lambda job, state, message="": result.reports.append((state, message))
    return result


def job(state="claimed"):
    return {"id": "one", "state": state, "message": "Login", "lease": "test-lease", "doi": "10.1234/test",
            "url": "https://publisher.example/article", "result": "{}"}


def test_html_is_delivered_before_pdf_without_connector_library_save(worker):
    order = []
    def upload(job, kind, path, metadata):
        order.append(kind)
        return {"content_ready": True, kind + "_received": True}
    worker.upload = upload
    worker.download_pdf = lambda url, referrer, path: path.write_bytes(b"%PDF-1.4 test")
    worker.acquire(job())
    assert order == ["html", "pdf"]
    assert worker.reports[-1][0] == "complete"
    assert "save-zotero" not in worker.calls
    assert list((worker.state / "outbox").iterdir()) == []


def test_recovered_login_is_preserved_until_explicit_resume(worker):
    worker.notify = lambda *args: None
    worker.acquire(job("needs_login"))
    assert worker.browser.waiting_for_login
    assert worker.calls == []


def test_recovered_paused_job_restores_a_blank_tab_once(worker):
    navigations = []
    worker.browser.article.url = "about:blank"
    worker.browser.article.goto = lambda url, **kwargs: navigations.append(url)
    worker.notify = lambda *args: None
    worker.recovery = {"id": "one", "route": "direct"}
    worker.acquire(job("needs_login"))
    worker.acquire(job("needs_login"))
    assert navigations == [job()["url"]]
    assert worker.browser.waiting_for_login


def test_failed_pdf_does_not_discard_successful_html(worker):
    worker.upload = lambda *args: {"content_ready": True, "html_received": True}
    def fail(*args):
        raise ValueError("Publisher returned a reader")
    worker.download_pdf = fail
    worker.acquire(job())
    assert worker.reports[-1][0] == "complete"
    assert "PDF unavailable" in worker.reports[-1][1]


def test_notification_is_not_repeated_after_restart(worker, monkeypatch):
    calls = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    worker.notify(job(), "Login")
    worker.notify(job(), "Login")
    restarted = module.AcquisitionWorker(worker.browser, worker.state)
    restarted.notify(job(), "Login")
    assert len(calls) == 1
