# Mac institutional acquisition worker

Academic hands unavailable articles to a visible Chrome browser on the Mac.
The browser uses institutional SOCKS5 access, retains its profile, and uses
Zotero Connector translators to discover metadata and PDFs. UCL library-proxy
navigation is a second route. Credentials stay in the browser; sign-in and
human verification remain manual.

## Using it

The MCP tool accepts:

```text
browser_acquisition(action="request", doi="10.…", url="https://publisher/…")
browser_acquisition(action="status", id="job ID")
browser_acquisition(action="resume", id="job ID")
browser_acquisition(action="cancel", id="job ID")
```

`fetch_fulltext` automatically queues this fallback when its usual sources fail.
An explicit browser request skips waiting for those sources. Refresh the
client's tool list after installing the server update.

Requests return immediately and repeated pending requests for a DOI coalesce.
When attention is needed, one Mac notification and Resume/Later dialog appear.
Complete sign-in or verification in the existing Chrome page, then choose
Resume. You can also resume through MCP or the local command. A challenge that
is still present remains paused even if Resume is pressed.

`content_ready` means Academic can read the cached text: repeat `fetch_fulltext`.
It does not mean a PDF was obtained or saved. `html_received`, `pdf_received`,
the job message and separate `zotero` status expose those outcomes. Zotero
status checks its durable queue and target library. Group attachments use
Zotero Storage; personal libraries use configured WebDAV or Zotero Storage.

## Flow and limits

1. Reuse the article tab without navigating if it is already the requested URL.
2. Perform bounded rendering/scrolling; pause for sign-in or browser checks.
3. Run Zotero's publisher translator without creating library items.
4. Deliver HTML to Asahi for article identity checks, extraction and caching.
5. Try the translator's PDF URL with Connector's cookie-aware downloader.
6. If HTML is unavailable, allow one visible PDF/reader attempt in the spare tab.
7. Try UCL's library-proxy entry once if necessary, or pause for review.
8. Persist library writes on Academic's existing DOI-deduplicated import queue.

Good HTML becomes readable before the optional PDF attempt finishes. An
unavailable PDF does not discard it or trigger unnecessary prompts; the failure
remains in the job message. Short articles rejected by the existing HTML
heuristic fall back to PDF or review. Extraction checks are heuristic: hidden
sections, figures, equations and supplementary files may need inspection.

There is one article tab plus one popup. Helper restarts preserve a login popup.
No CAPTCHA is automatically solved or retried. An idle browser is recycled
after 20 acquisitions or approximately 1.5 GiB aggregate RSS. Human-paused
sessions are retained: this is idle recycling, not a hard per-download memory
cap. PDFs are capped at 75 MiB; HTML uploads at 25 MiB. Server browser artifacts
default to a 2 GiB storage quota. Pending jobs are capped at 100 and temporary
network retries at four claims.

Do not use this profile for unrelated browsing: excess pages are closed.
Chrome is launched directly; Playwright attaches later through a nonzero
loopback CDP port. Sites can still challenge the IP/browser. Silk is retained
but is not assumed to eliminate challenges. Keep CDP bound to loopback.

## Components on this Mac

- `run_worker.py`: supervises the SSH tunnel and browser helper.
- `browser_helper.py`: tab control and challenge detection.
- `acquisition_worker.py`: queue client, content delivery and notifications.
- `~/Library/LaunchAgents/lv.michae.academic-browser.plist`: starts at GUI login.
- `~/Library/Application Support/Academic Browser/`: private profile, credentials,
  recovery state, outbox and logs, outside the repository.

The launch agent references this checkout; update its paths if moving it.
State files/captures are private. The helper API is authenticated and bound to
loopback; `endpoint.json` and `worker.json` contain distinct private credentials.
Neither belongs in Git.

```sh
uv run --script deploy/macos/browser_helper.py status
uv run --script deploy/macos/browser_helper.py pause
uv run --script deploy/macos/browser_helper.py resume
uv run --script deploy/macos/browser_helper.py connector-status
uv run --script deploy/macos/browser_helper.py inspect-zotero
```

Stop the worker with `launchctl bootout gui/$(id -u)/lv.michae.academic-browser`.
Chrome remains open. Start it again with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/lv.michae.academic-browser.plist`.
Quit Chrome after stopping the worker if you also want the window closed.

For manual diagnostics without automation:

```sh
uv run --script deploy/macos/browser_helper.py launch --proxy socks5://HOST:PORT
```

For control, close that profile, launch with `--control`, then run `serve`.
Install Zotero Connector and Silk in this profile; verify both are enabled in
Chrome's extension manager. Files alone do not prove extension registration.

`inspect-zotero` translates without saving. Diagnostic `save-zotero` invokes
Connector's library-saving pipeline in Zotero's selected destination. The worker
does not use it because the server owns reconciliation; avoid it for unattended
jobs.

## Server deployment and recovery

The durable queue/artifacts live under `PDF_CACHE_DIR/../browser-acquisition`,
overridable with `BROWSER_ACQUISITION_DIR`. Provision a random token in its
private `worker-token` file or set `BROWSER_WORKER_TOKEN`. Worker endpoints
remain unauthorized without it. `BROWSER_ACQUISITION_ENABLED=false` disables
the feature; `BROWSER_ARTIFACT_QUOTA_MB` adjusts storage.

The worker uses an SSH local forward directly to the backend. On reconnect it
resolves `academic_mcp` on Docker's `mcp` network, avoiding an ephemeral pinned
IP. Worker credentials never traverse public OAuth or UCL. Every upload also
requires a job-specific, expiring lease.

`worker.json` has `server` (local tunnel URL), `token`, `worker` (stable ID),
`ssh_host`, `tunnel_port`, and `proxy`. This Mac is already provisioned; SSH
must work noninteractively.

The deployed local image overlays changed source on the previously running
image. Its bundle and private original container configuration live in the
server's `docker-binds/academic-mcp/browser-release/`. The script
`deploy/upgrade-browser-server.py` preserves mounts, environment, labels, restart
policy, networks and addresses, retains a stopped rollback container and rolls
back automatically on startup failure. Future images must include these source
changes before replacing the local release.

Queued jobs and delivered artifacts survive sleep/outages/restarts. Expired
leases are recoverable; cancelled or stale workers cannot publish content.
Human-paused jobs block further browser jobs until resumed/cancelled. The Mac
supervisor reconnects and cleans its orphan process groups after a crash while
leaving Chrome intact on ordinary worker restarts.

If you manually obtain a PDF, attach it to the existing Zotero item, sync data
and files, then Resume. The server checks for the synced attachment. Avoid
creating a second parent through Connector. Automatic imports currently require
a canonical DOI and use Academic's configured library. URL-only documents can
be read/cached but are not automatically added to Zotero.

The bridge uses internal APIs tested with Connector 5.0.217. The newer
RemoteTranslate branch remains untested. Publisher coverage and subscription
availability require real access tests; failures are reported as review states.
