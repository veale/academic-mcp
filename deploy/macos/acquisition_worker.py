"""Single-job Mac worker. Authentication stays in the dedicated browser."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


class AcquisitionWorker:
    def __init__(self, browser, state):
        self.browser = browser
        self.state = state
        self.config = json.loads((state / "worker.json").read_text())
        self.opener = build_opener(ProxyHandler({}))
        self.active = None
        self.completed = 0
        self.last_error = None
        self.notified = set()
        self.download_session = None
        try:
            self.recovery = json.loads((state / "active-job.json").read_text())
        except FileNotFoundError:
            self.recovery = {}

    def api(self, action, body=None, data=None, metadata=None, lease=None, params=None):
        url = self.config["server"].rstrip("/") + "/acquisition/" + action
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": "Bearer " + self.config["token"]}
        if metadata:
            headers["X-Article-Metadata"] = json.dumps(metadata, ensure_ascii=True)
        if lease:
            headers["X-Job-Lease"] = lease
        if data is None:
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"
        else:
            headers["Content-Type"] = "application/octet-stream"
        try:
            with self.opener.open(Request(url, data=data, headers=headers), timeout=180) as response:
                return json.load(response)
        except HTTPError as exc:
            raise ValueError(exc.read(2000).decode()) from None

    def report(self, job, state, message=""):
        return self.api("report", {"id": job["id"], "lease": job["lease"], "state": state, "message": message})

    def notify(self, job, message):
        marker = (job["id"], message)
        digest = hashlib.sha256((job["id"] + message).encode()).hexdigest()
        if marker in self.notified or self.recovery.get("notified") == digest:
            return
        self.notified.add(marker)
        self.remember(notified=digest)
        # Publisher-provided strings never enter executable code or the dialog.
        command = shlex.join([str(Path.home() / ".local/bin/uv"), "run", "--script",
                              str(Path(__file__).with_name("browser_helper.py")), "resume"])
        script = ('display notification "Complete sign-in or verification in the Academic Chrome window." '
                  'with title "Academic browser paused"\n'
                  'set choice to display dialog "Academic needs your attention in its Chrome window. '
                  'Complete sign-in or verification there, then click Resume here. You can also resume '
                  'later from your assistant." with title "Academic browser" '
                  'buttons {"Later", "Resume"} default button "Resume"\n'
                  'if button returned of choice is "Resume" then\n'
                  'do shell script ' + json.dumps(command) + '\nend if')
        subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def remember(self, **values):
        self.recovery.update(values)
        path = self.state / "active-job.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.recovery))
        temporary.replace(path)

    def upload(self, job, kind, path, metadata):
        return self.api("upload", data=path.read_bytes(), metadata=metadata, lease=job["lease"],
                        params={"id": job["id"], "kind": kind})

    def pause(self, job, reason, review=False):
        self.browser.waiting_for_login = True
        self.browser.pause_reason = reason
        self.report(job, "needs_review" if review else "needs_login", reason)
        self.notify(job, reason)

    def download_pdf(self, url, referrer, path):
        worker = self.browser.worker()
        encoded = worker.evaluate("""async ({url, referrer}) => {
            const tabs = await chrome.tabs.query({});
            const tab = tabs.find(t => t.url === referrer);
            if (!tab) throw new Error('Article tab missing');
            // Explicitly disable Connector's challenge-retry/window fallback.
            const buffer = await Zotero.ItemSaver._fetchAttachment({url, referrer,
                mimeType:'application/pdf', title:'Full Text PDF'}, tab, false);
            if (buffer.byteLength > 75 * 1024 * 1024) throw new Error('PDF exceeds 75 MiB');
            const bytes = new Uint8Array(buffer);
            let binary = '';
            for (let i=0; i<bytes.length; i+=32768) {
                binary += String.fromCharCode(...bytes.subarray(i,i+32768));
            }
            return btoa(binary);
        }""", {"url": url, "referrer": referrer})
        content = base64.b64decode(encoded, validate=True)
        if not content.lstrip().startswith(b"%PDF-"):
            raise ValueError("Publisher returned a reader/login page instead of PDF bytes")
        temporary = path.with_suffix(".partial")
        temporary.write_bytes(content)
        temporary.replace(path)

    def download_in_tab(self, url, path):
        """One visible PDF/reader attempt, with capped downloads and no button hunt."""
        # A prior manual verification may have completed a browser download.
        for candidate in path.parent.iterdir():
            if candidate.is_file() and candidate.suffix not in {".html", ".crdownload", ".partial"}:
                with candidate.open("rb") as stream:
                    header = stream.read(16)
                if header.lstrip().startswith(b"%PDF-"):
                    if candidate.stat().st_size > 75 * 1024 * 1024:
                        raise ValueError("PDF exceeds 75 MiB")
                    candidate.replace(path)
                    self.finish_download_session()
                    return
        self.finish_download_session()
        context = self.browser.context
        connection = context.browser.new_browser_cdp_session()
        downloads = {}
        connection.on("Browser.downloadWillBegin", lambda event: downloads.update({"guid": event["guid"]}))
        def progress(event):
            if event.get("receivedBytes", 0) > 75 * 1024 * 1024 or event.get("totalBytes", 0) > 75 * 1024 * 1024:
                connection.send("Browser.cancelDownload", {"guid": event["guid"]})
            downloads["state"] = event.get("state")
        connection.on("Browser.downloadProgress", progress)
        connection.send("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": str(path.parent), "eventsEnabled": True})
        popup = self.browser.popup
        if popup is None or popup.is_closed():
            popup = context.new_page()
        try:
            if popup.url != url:
                try:
                    popup.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass  # Direct downloads abort navigation; inspect events below.
            for _ in range(20):
                self.browser.article.wait_for_timeout(500)
                self.browser.check_challenge()
                if self.browser.waiting_for_login:
                    popup.bring_to_front()
                    raise ValueError("PDF verification is waiting in the existing browser tab")
                if downloads.get("state") == "completed":
                    candidate = path.parent / downloads["guid"]
                    if candidate.exists():
                        with candidate.open("rb") as stream:
                            header = stream.read(16)
                        if header.lstrip().startswith(b"%PDF-"):
                            candidate.replace(path)
                            popup.close()
                            return
                if not downloads and _ >= 3:
                    break
            raise ValueError("The publisher reader did not expose a PDF download automatically")
        finally:
            if self.browser.waiting_for_login:
                # Keep the capped download handler alive while the user verifies.
                self.download_session = connection
            else:
                if downloads.get("guid") and downloads.get("state") != "completed":
                    connection.send("Browser.cancelDownload", {"guid": downloads["guid"]})
                connection.send("Browser.setDownloadBehavior", {"behavior": "default"})
                connection.detach()
                if not popup.is_closed():
                    popup.close()

    def finish_download_session(self):
        if self.download_session:
            try:
                self.download_session.send("Browser.setDownloadBehavior", {"behavior": "default"})
                self.download_session.detach()
            finally:
                self.download_session = None

    def settle(self):
        page = self.browser.article
        # Bounded scrolling loads common lazy article bodies without clicking controls.
        for _ in range(4):
            page.wait_for_timeout(500)
            self.browser.check_challenge()
            if self.browser.waiting_for_login:
                return
            page.evaluate("() => window.scrollBy(0, Math.max(600, innerHeight))")
        page.evaluate("() => window.scrollTo(0, 0)")

    def acquire(self, job):
        from browser_helper import institutional_url

        first = self.active != job["id"]
        self.active = job["id"]
        self.browser.remote_job = {"id": job["id"], "state": job["state"]}
        if first:
            recovered = self.recovery.get("id") == job["id"]
            if not recovered:
                self.recovery = {"id": job["id"], "route": "direct",
                                 "navigated": self.browser.article.url == job["url"], "request_url": job["url"]}
                self.remember()
            if job["state"] in {"needs_login", "needs_review"}:
                if recovered and self.browser.article.url == "about:blank":
                    # A browser reconnect can lose the visible tab while the
                    # durable job remains paused. Restore it once, then wait.
                    target = job["url"]
                    if self.recovery.get("route") == "institutional":
                        target = institutional_url(target)
                    try:
                        self.browser.article.goto(target, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                self.browser.waiting_for_login = True
                self.browser.pause_reason = job["message"] or "Acquisition awaits manual attention"
        if job["state"] in {"needs_login", "needs_review"}:
            if self.browser.waiting_for_login:
                self.notify(job, job["message"])
                return
            # Local Resume is an explicit instruction to continue the preserved tab.
            self.report(job, "claimed", "Resuming the preserved browser page")
            if job["state"] == "needs_review":
                recovered = self.api("recover", {"id": job["id"], "lease": job["lease"]})
                if recovered.get("content_ready"):
                    self.report(job, "complete", "Retrieved the PDF saved and synced through Zotero")
                    self.active = None
                    self.remember(id=None)
                    return
        if self.browser.waiting_for_login:
            # Remote Resume may clear a worker pause, but never clear a live challenge.
            if job["state"] == "claimed" and not first:
                self.browser.waiting_for_login = False
                self.browser.check_challenge()
            if self.browser.waiting_for_login:
                self.pause(job, self.browser.pause_reason or "Browser needs attention")
                return
        if not self.recovery.get("navigated"):
            target = job["url"]
            if self.recovery["route"] == "institutional":
                target = institutional_url(self.recovery.get("resolved_url") or target)
            # Preserve a visible page on timeout; do not repeatedly navigate a login.
            self.remember(navigated=True)
            try:
                self.browser.dispatch("open", {"url": target})
            except Exception:
                self.pause(job, "Navigation did not finish; inspect the existing article tab and Resume")
                return
        self.settle()
        if self.browser.waiting_for_login:
            self.pause(job, self.browser.pause_reason or "Sign-in is needed on the iMac")
            return

        page = self.browser.article
        self.remember(resolved_url=page.url)
        fallback = page.evaluate("""() => {
            const meta = name => document.querySelector(`meta[name="${name}"]`)?.content;
            return {title:meta('citation_title') || document.title, doi:meta('citation_doi'),
                url:document.location.href, pdf:meta('citation_pdf_url')};
        }""")
        metadata = {"title": fallback["title"], "doi": fallback.get("doi"), "url": page.url}
        attachments = []
        try:
            translated = self.browser.dispatch("inspect-zotero", {})
            items = translated[0].get("items", []) if translated else []
            if len(items) == 1:
                item = items[0]
                metadata.update({k: v for k, v in item.items() if k != "attachments" and v})
                attachments = item.get("attachments", [])
        except Exception as exc:
            # HTML can still succeed without a site-specific translator.
            self.last_error = "Translator unavailable: " + str(exc)[:200]
        if self.browser.waiting_for_login:
            self.pause(job, self.browser.pause_reason or "Verification required")
            return
        if not metadata.get("doi") and job.get("doi"):
            # Server also checks citation_doi in HTML before accepting content.
            metadata["doi"] = job["doi"]
        folder = self.state / "outbox" / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        html = folder / "article.html"
        html.write_text(page.content())
        result = json.loads(job.get("result") or "{}")
        errors = []
        try:
            result = self.upload(job, "html", html, metadata)
        except ValueError as exc:
            errors.append("HTML not accepted: " + str(exc)[:300])

        candidates = [a.get("url") for a in attachments if a.get("mimeType") == "application/pdf" and a.get("url")]
        if fallback.get("pdf"):
            candidates.append(fallback["pdf"])
        # A translator-provided PDF is preferred over a generic download-button hunt.
        for url in list(dict.fromkeys(candidates))[:1]:
            try:
                pdf = folder / "article.pdf"
                if not pdf.exists():
                    try:
                        self.download_pdf(url, page.url, pdf)
                    except Exception:
                        if result.get("content_ready"):
                            raise
                        self.download_in_tab(url, pdf)
                result = self.upload(job, "pdf", pdf, metadata)
                break
            except Exception as exc:
                if self.browser.waiting_for_login:
                    self.pause(job, self.browser.pause_reason or "Complete the PDF browser verification and Resume")
                    return
                errors.append("PDF unavailable: " + str(exc)[:300])
                # No browser challenges are solved or retried by this path.
                break
        if result.get("content_ready"):
            self.finish_download_session()
            self.report(job, "complete", "; ".join(errors) or "Article and available PDF delivered to Academic")
            self.completed += 1
            self.browser.remote_job = {"id": job["id"], "state": "complete", **result}
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink()
            folder.rmdir()
            self.remember(id=None)
            self.active = None
            return
        if self.recovery["route"] == "direct":
            self.remember(route="institutional", resolved_url=page.url, navigated=False)
            return  # Next tick makes one library-proxy attempt in the same tab.
        self.pause(job, "No complete article or PDF was obtained through institutional access. "
                   "Check access in the current tab, or attach the PDF to its existing Zotero item, sync and Resume. "
                   + "; ".join(errors), review=True)

    def tick(self):
        try:
            job = self.api("claim", {"worker": self.config["worker"]})
            self.last_error = None
            if job:
                self.acquire(job)
            elif self.active:
                # Cancellation or completion by another client releases local protection.
                self.active = None
                self.browser.waiting_for_login = False
                self.browser.pause_reason = None
                self.browser.remote_job = None
                self.finish_download_session()
        except ValueError as exc:
            self.last_error = str(exc)[:400]
            if self.active and 'job' in locals() and job:
                try:
                    self.report(job, "failed", self.last_error)
                except Exception:
                    pass
                self.active = None
        except Exception as exc:
            # Network/server failures leave the durable lease/job and outbox intact.
            self.last_error = type(exc).__name__ + ": " + str(exc)[:300]
            if self.active and 'job' in locals() and job:
                try:
                    self.report(job, "retry", self.last_error)
                    self.active = None
                except Exception:
                    pass


def memory_mb(pid):
    """Approximate browser RSS including its child processes, for idle recycling."""
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="]).decode().splitlines()
    processes = [tuple(map(int, row.split())) for row in rows if len(row.split()) == 3]
    children = {pid}
    for _ in range(8):
        children.update(p for p, parent, _ in processes if parent in children)
    return sum(rss for p, _, rss in processes if p in children) / 1024
