# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.62.0"]
# ///
"""Local, single-browser acquisition prototype. Run with `uv run --script`.

This does not yet connect to Asahi's fetch queue or save into Academic's cache.
Browser credentials and captured HTML stay in the user's application directory.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

STATE = Path.home() / "Library/Application Support/Academic Browser"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def challenge_reason(title, text):
    """A mention of CAPTCHA in an article is not itself a browser challenge."""
    title = title.lower().strip()
    text = text.lower()
    if any(marker in title for marker in (
        "just a moment", "checking your browser", "security verification",
        "robot check", "access denied", "attention required", "captcha",
    )):
        return "Publisher browser/security check"
    if any(marker in text for marker in (
        "verify you are human", "verify that you are human",
        "performing security verification", "checking if the site connection is secure",
        "complete the captcha", "unusual traffic from your computer network",
    )):
        return "Publisher human verification"
    return None


def launch_chrome(args):
    """Launch Chrome directly, without Playwright launch/automation switches."""
    args.state.mkdir(parents=True, mode=0o700, exist_ok=True)
    profile = args.state / "profile"
    lock = profile / "SingletonLock"
    if lock.exists() or lock.is_symlink():
        try:
            owner = int(os.readlink(lock).rsplit("-", 1)[1])
            os.kill(owner, 0)
        except ProcessLookupError:
            pass  # Chrome itself recovers its stale lock; do not delete it here.
        except (ValueError, OSError):
            raise ValueError("Cannot verify ownership of the dedicated Chrome profile") from None
        else:
            raise ValueError("Close this dedicated Chrome profile before launching it again")
    parsed = urlsplit(args.proxy)
    if parsed.scheme != "socks5" or not parsed.hostname or not parsed.port or parsed.username:
        raise ValueError("Supply an unauthenticated socks5://host:port proxy")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [CHROME, f"--user-data-dir={profile}", f"--proxy-server={args.proxy}",
               "--proxy-bypass-list=127.0.0.1;localhost", "--no-first-run",
               "--no-default-browser-check", validate_url(args.url)]
    if args.control:
        command[1:1] = [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]
    with (args.state / "chrome.log").open("ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
    descriptor = {"pid": process.pid, "port": port if args.control else None,
                  "proxy": args.proxy}
    (args.state / "browser.json").write_text(json.dumps(descriptor))
    print(json.dumps({"launched": True, "control_enabled": args.control,
                      "attached": False, "profile": str(profile)}))


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("An http(s) article URL is required")
    if parsed.username or parsed.password:
        raise ValueError("Credentials must not be embedded in article URLs")
    return url


def institutional_url(url):
    validate_url(url)
    host = urlsplit(url).hostname.lower()
    if host == "libproxy.ucl.ac.uk" or host.endswith(".libproxy.ucl.ac.uk"):
        return url
    return "https://libproxy.ucl.ac.uk/login?" + urlencode({"url": url})


class AcquisitionBrowser:
    def __init__(self, context, state):
        self.context = context
        self.state = state
        self.waiting_for_login = False
        self.pause_reason = None
        self.remote_job = None
        self.article = context.pages[0] if context.pages else context.new_page()
        self.popup = context.pages[1] if len(context.pages) > 1 else None
        for page in context.pages[2:]:
            page.close()
        context.on("page", self.on_page)

    def check_challenge(self):
        if self.waiting_for_login or self.article.is_closed():
            return
        try:
            reason = None
            for page in self.context.pages:
                if page.is_closed() or page.url.startswith("chrome"):
                    continue
                reason = challenge_reason(page.title(), page.locator("body").inner_text(timeout=500))
                host = urlsplit(page.url).hostname or ""
                if not reason and (host == "login.microsoftonline.com" or host.startswith("shib-idp.")
                                   or host.endswith(".okta.com") or (host == "libproxy.ucl.ac.uk"
                                   and urlsplit(page.url).path.startswith("/login"))):
                    reason = "Institutional sign-in required"
                if not reason and page.locator('input[type="password"]').count():
                    reason = "Institutional or publisher sign-in required"
                if reason:
                    break
        except Exception:
            return
        if reason:
            self.waiting_for_login = True
            self.pause_reason = reason
            print("Academic browser paused: " + reason + "; complete manually, then resume", flush=True)

    def on_page(self, page):
        # Keep the article and at most one authentication/Connector popup.
        if self.popup is not None and not self.popup.is_closed():
            page.close()
        else:
            self.popup = page

    def status(self):
        return {
            "waiting_for_login": self.waiting_for_login,
            "pause_reason": self.pause_reason,
            "acquisition": self.remote_job,
            "worker_error": getattr(self, "remote_error", None),
            "pages": [{"url": p.url} for p in self.context.pages],
            "page_limit": 2,
            "profile": str(self.state / "profile"),
        }

    def worker(self, wake=True):
        for worker in self.context.service_workers:
            if worker.url.startswith("chrome-extension://"):
                try:
                    if worker.evaluate("() => typeof Zotero !== 'undefined' && !!Zotero.Connector_Browser"):
                        return worker
                except Exception:
                    continue
        if wake and (self.popup is None or self.popup.is_closed()):
            # MV3 workers sleep. A fresh CDP attachment may have no worker
            # handle until an extension page wakes it; no publisher reload.
            page = self.context.new_page()
            try:
                page.goto("chrome-extension://ekhagklcjbdpajgpjgmbionohlpdbjgc/preferences/preferences.html",
                          wait_until="domcontentloaded", timeout=10000)
                for _ in range(10):
                    page.wait_for_timeout(100)
                    try:
                        return self.worker(wake=False)
                    except ValueError:
                        pass
            except Exception:
                pass
            finally:
                page.close()
        raise ValueError("No running Zotero Connector worker found; install/activate Connector in this profile")

    def dispatch(self, action, data):
        if action == "status":
            return self.status()
        if action == "resume":
            self.waiting_for_login = False
            self.pause_reason = None
            self.check_challenge()
            return self.status()
        if action == "pause":
            self.waiting_for_login = True
            self.pause_reason = "Manual pause"
            return self.status()
        if action == "open":
            if self.waiting_for_login:
                raise ValueError("Login is pending or a browser check is paused. Complete it and run resume before navigating elsewhere")
            url = validate_url(data["url"])
            if data.get("institutional"):
                url = institutional_url(url)
            if self.article.is_closed():
                if self.popup is not None and not self.popup.is_closed():
                    self.popup.close()
                self.article = self.context.new_page()
                # new_page triggers on_page; this page is the article, not a popup.
                if self.popup == self.article:
                    self.popup = None
            self.waiting_for_login = bool(data.get("wait_for_login"))
            self.pause_reason = "Manual login requested" if self.waiting_for_login else None
            try:
                self.article.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                # Preserve a partially loaded page so the user can inspect it.
                self.waiting_for_login = True
                raise
            self.article.bring_to_front()
            self.check_challenge()
            return self.status()
        if action == "capture":
            if self.article.is_closed():
                raise ValueError("The article tab has been closed")
            # Capture rendered DOM, not a claim that the full article is present.
            output = self.state / "captures"
            output.mkdir(mode=0o700, exist_ok=True)
            stem = secrets.token_hex(8)
            html = output / (stem + ".html")
            html.write_text(self.article.content())
            metadata = {"url": self.article.url, "title": self.article.title(), "html": str(html),
                        "completeness": "unverified"}
            (output / (stem + ".json")).write_text(json.dumps(metadata, indent=2))
            return metadata
        if action in {"connector-status", "inspect-zotero", "save-zotero"}:
            self.check_challenge()
            if action != "connector-status" and self.waiting_for_login:
                raise ValueError("Resolve the browser check/login and resume before saving or translating")
            worker = self.worker()
            expression = """async ({url, save, inspect}) => {
                const tabs = await chrome.tabs.query({});
                const matching = tabs.filter(t => t.url === url);
                if (matching.length !== 1) throw new Error('Cannot uniquely identify article tab');
                const tab = matching[0];
                // Installation on an already open page does not necessarily inject scripts.
                await Zotero.Connector_Browser.injectTranslationScripts(tab);
                if (inspect) {
                    const results = await chrome.scripting.executeScript({
                        target: {tabId: tab.id}, func: async () => {
                            await Zotero.PageSaving.onPageLoad(true);
                            const translators = Zotero.PageSaving.translators.filter(t => t.itemType !== 'multiple');
                            if (!translators.length) throw new Error('No single-article translator detected');
                            let translated;
                            if (Zotero.RemoteTranslate) {
                                translated = await Zotero.RemoteTranslate.translate({document, translators});
                            } else {
                                const translate = await Zotero.PageSaving._initTranslate(translators[0].itemType);
                                translated = await Zotero.TranslateWeb.translate({translate, translators});
                            }
                            // Translation only: never instantiate ItemSaver or call saveItems.
                            return {saved: false, items: translated.items.map(item => ({
                                title: item.title, doi: item.DOI, url: item.url,
                                attachments: (item.attachments || []).map(a => ({
                                    title: a.title, url: a.url, mimeType: a.mimeType || a.contentType
                                }))
                            }))};
                        }
                    });
                    return results.map(result => result.result);
                }
                const info = Zotero.Connector_Browser.getTabInfo(tab.id);
                const translators = (info.translators || []).map(t => ({
                    id: t.translatorID, label: t.label, itemType: t.itemType
                }));
                if (save) {
                    if (!translators.length) throw new Error('No translator detected for this page');
                    await Zotero.Connector_Browser.saveWithTranslator(tab, 0, {fallbackOnFailure: true});
                }
                return {translators, save_requested: save,
                    attachment_completion: 'not independently verified'};
            }"""
            return worker.evaluate(expression, {"url": self.article.url,
                                                "save": action == "save-zotero",
                                                "inspect": action == "inspect-zotero"})
        raise ValueError("Unknown action")


def serve(args):
    from playwright.sync_api import sync_playwright

    os.umask(0o077)
    args.state.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (args.state / "helper.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        descriptor = json.loads((args.state / "browser.json").read_text())
        if not descriptor.get("port"):
            raise ValueError("Chrome is in manual mode. Close it, then launch with --control before attaching")
        address = f"http://127.0.0.1:{descriptor['port']}"
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 30
        while True:
            try:
                with opener.open(address + "/json/version", timeout=1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("Chrome's local control endpoint did not start") from None
                time.sleep(0.2)
        with sync_playwright() as pw:
            connection = pw.chromium.connect_over_cdp(address, no_defaults=True)
            context = connection.contexts[0]
            browser = AcquisitionBrowser(context, args.state)
            worker = None
            if (args.state / "worker.json").exists():
                from acquisition_worker import AcquisitionWorker
                worker = AcquisitionWorker(browser, args.state)
            token = secrets.token_urlsafe(32)

            class Handler(BaseHTTPRequestHandler):
                timeout = 5

                def log_message(self, *unused):
                    pass  # Do not log article URLs or access tokens.

                def do_POST(self):
                    if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                        self.reply(401, {"error": "Unauthorized"})
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 16384:
                            raise ValueError("Invalid request size")
                        data = json.loads(self.rfile.read(length))
                        self.reply(200, browser.dispatch(data.pop("action"), data))
                    except Exception as exc:
                        self.reply(400, {"error": str(exc)})

                def reply(self, status, result):
                    body = json.dumps(result).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            server = HTTPServer(("127.0.0.1", 0), Handler)
            server.timeout = 0.1
            endpoint = args.state / "endpoint.json"
            endpoint.write_text(json.dumps({"port": server.server_port, "token": token}))
            print("Academic browser ready; use status/open/capture/connector-status commands", flush=True)
            try:
                last_check = 0
                last_poll = 0
                while context.pages:
                    server.handle_request()
                    # Pump browser events to enforce the popup cap while idle.
                    live = context.pages
                    if live:
                        live[0].wait_for_timeout(100)
                    if time.monotonic() - last_check > 2:
                        browser.check_challenge()
                        last_check = time.monotonic()
                    if worker and time.monotonic() - last_poll > 5:
                        worker.tick()
                        last_poll = time.monotonic()
                        browser.remote_error = worker.last_error
                        if not worker.active and not browser.waiting_for_login and len(context.pages) == 1:
                            from acquisition_worker import memory_mb
                            if worker.completed >= 20 or (worker.completed > 0 and memory_mb(descriptor["pid"]) > 1500):
                                print("Recycling idle browser to release memory", flush=True)
                                connection.new_browser_cdp_session().send("Browser.close")
                                break
            finally:
                endpoint.unlink(missing_ok=True)
                server.server_close()
                # Disconnecting the helper leaves the user's Chrome window open.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("launch")
    start.add_argument("--proxy", required=True)
    start.add_argument("--control", action="store_true", help="Enable loopback CDP for a later attach")
    start.add_argument("--url", default="https://www.ucl.ac.uk/library/")
    commands.add_parser("serve")
    for action in ["status", "pause", "resume", "capture", "connector-status", "inspect-zotero", "save-zotero"]:
        commands.add_parser(action)
    navigate = commands.add_parser("open")
    navigate.add_argument("url")
    navigate.add_argument("--institutional", action="store_true")
    navigate.add_argument("--wait-for-login", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "launch":
        launch_chrome(args)
        return
    if args.action == "serve":
        serve(args)
        return
    try:
        endpoint = json.loads((args.state / "endpoint.json").read_text())
    except FileNotFoundError:
        raise SystemExit("Browser helper is not running; start the serve command first") from None
    payload = vars(args).copy()
    payload.pop("state")
    request = Request(
        f"http://127.0.0.1:{endpoint['port']}/", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + endpoint["token"], "Content-Type": "application/json"},
    )
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            print(response.read().decode())
    except HTTPError as exc:
        raise SystemExit(exc.read().decode()) from None


if __name__ == "__main__":
    main()
