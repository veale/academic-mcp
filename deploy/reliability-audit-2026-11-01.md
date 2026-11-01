# Academic MCP reliability audit — 1 November 2026

The fixes are deployed to `academic_mcp` on `ssh asahi`. The existing Zotero
key was granted write access during the audit. No credentials were copied
into this report or the source changes.

## Findings and changes

- Zotero returned `403 Write access denied` for the configured Web API key.
  Reads worked, giving an impression of a functioning integration. The key
  now passes an empty-batch write probe with HTTP 200.
- The import worker deleted failed jobs after eight attempts. All 53 abandoned
  DOIs retained in the service logs have been recovered and their Zotero
  attachment hashes verified against local PDFs. Nine lost file paths were
  recovered by matching extracted PDF content to the cached article text.
- Failed jobs now survive indefinitely, with slower retries and PDF copies
  protected from cache eviction in `pending_imports/`. Denied permissions
  are reprobed every five minutes. Persistent pending errors are exposed by
  `zotero_import_status`. Replacing a key in the environment needs a restart.
- Cached PDF reads can enqueue a missed import. New PDF extractions save the
  file path. Citation metadata is also queued for DOI-bearing HTML retrievals;
  it can receive a PDF later. A metadata-only job cannot overwrite a queued PDF.
- An empty PDF attachment record was incorrectly considered a successful
  upload. A remote PDF now requires uploaded-file metadata; retries reuse
  incomplete attachment records instead of creating another one.
- Zotero web quicksearch does not search DOI fields, even with
  `qmode=everything`. Import duplicate detection now checks the correct
  library in the synced mirror, verifies candidate keys against the live
  library, and scans changed records since that mirror's synced version.
  Without a usable mirror it scans the target library with pagination. A
  failed duplicate check blocks creation rather than risking another item.
- Metadata, duplicate checks, and PDF uploads use the Web API consistently
  when configured. Local unsynced item keys are not mixed with web uploads.
  Successful imports update the discovery DOI index immediately. New arXiv
  DOI imports use the preprint item type even when Crossref has no record.
- Direct HTTP fetching required an explicit proxy flag, while browser calls
  always used the proxy and sometimes lost the flag altogether. Configured
  institutional access is now an automatic fallback; the browser receives
  the route decision. `INSTITUTIONAL_PROXY_AUTO=false` restores explicit
  opt-in. URL-driven PDF downloads also try the institutional route.
- The configured university proxy worked during testing: external requests
  exited through `144.82.238.246`, distinct from the direct connection.

## SQLite and Zotero versions

The Zotero application installed on this Mac reports version 9. Its local
API should not be treated as a writable Web API. Current Zotero documentation
specifies local writes in Zotero 10+, with a separately authorized local key
and server identity header. The old `allowWriteAccess` assumption is not the
current supported authorization protocol. The running Asahi service now
uses the authenticated Web API for imports and does not depend on that path.

The server's mirror uses WAL and was readable, with a recent `.last-sync`.
Removing the Mac-to-server mirror would reduce availability when the source
Mac is asleep or unavailable. An application upgrade does not remove that
architectural requirement. The mirror is retained. Redundant shadow backups
are skipped for published snapshots marked by `.last-sync`; live databases
retain the existing lock fallback.

The repository's Mac sync script contained a second legacy implementation
appended after the current one, including a plain-copy fallback and full
storage mirroring. That duplicate code is removed. The backup now opens the
source read-only, waits briefly for locks, and retains the previous remote
snapshot on failure. Its debounce timestamp advances only after success.
No Mac launch agent was replaced: this workspace's standard LaunchAgents
folder contains no Zotero sync agent, while the server's mirror is actively
updated by an existing source. The repository script and documentation are
prepared for that source's next installation/update.

References: [Zotero local API](https://www.zotero.org/support/dev/web_api/v3/local_api),
[direct SQLite access](https://www.zotero.org/support/dev/client_coding/direct_sqlite_database_access),
[Web API search](https://www.zotero.org/support/dev/web_api/v3/basics),
[file uploads](https://www.zotero.org/support/dev/web_api/v3/file_upload).

## Verification

- 272 tests passed in the running container's Python environment, using an
  isolated source tree. Test processes disabled automatic imports and the
  real institutional proxy; new routing tests explicitly stub their endpoints.
- Git whitespace checks and `bash -n deploy/macbook/zotero-sync.sh` passed.
- All 53 historical abandoned DOI imports have a live Zotero record and a
  PDF attachment whose reported MD5 matches the recovered local file.
- Three PDFs were downloaded back from Zotero and their bytes independently
  hashed, confirming that the attachment records contain accessible PDFs.
- Real MCP transport checks against the restarted service successfully called
  `zotero_import_status` and `fetch_fulltext` for a cached paper. Status showed
  a ready write backend and an empty recovery queue. Further ordinary imports
  were also observed succeeding after deployment.
- The Academic MV app connection in this chat returned a reauthentication
  error. It was bypassed for this audit through SSH and direct MCP checks;
  it still needs reconnecting in the client.

Server receipts are persisted outside the source repository:

- `/home/mv/docker-binds/academic-mcp/cache/import-recovery-20261101.json`
- `/home/mv/docker-binds/academic-mcp/cache/import-recovery-verification-20261101.json`

## Initial incremental deployment and rollback

The patched source is in the running container and in a derived image named
`academic-mcp:reliability-20261101`. The currently configured deployment tag,
`academic-mcp:f493de6`, also points to the patched image, so recreating the
service from that existing local tag retains the fix. The original image is
preserved as `academic-mcp:before-reliability-20261101`. The running container
was restarted after copying the tested files; its original container image ID
therefore remains unchanged, while its source and future recreation tag are
patched. Libraries, indexes, and persistent caches were not rebuilt or wiped.

The derived-image build context is at
`/home/mv/docker-binds/academic-mcp/deploy-reliability-20261101/`.
A normal source build should replace this incremental deployment in the next
release. To roll back the current container, restore `/app/src/academic_mcp/`
from the preserved original image and restart it; also retag the preserved
image as `academic-mcp:f493de6` before any future container recreation.
Retagging alone does not change files in the currently running container.

## Remaining limits

University IP access is subject to subscriptions, publisher authentication,
and anti-bot systems. It cannot guarantee every download. URL-only documents
without DOIs are still excluded from auto-import. Personal-library uploads now honor configured WebDAV storage; groups and
libraries without WebDAV use Zotero File Storage and its quota. Users still
need normal Zotero data/file sync to see server-created records on desktop.
The active source machine's installed sync job was not located or modified.


## Follow-up: desktop file-sync backend

The initial attachment verification checked Zotero File Storage, but the
MacBook uses WebDAV. This allowed records and working web downloads while
desktop attachments reported missing files. The reported attachment key
`4P79NDN4` was repaired in place, and all 60 recent auto-imported attachments were checked and repaired
without creating duplicate records (59 in the batch and the reported
attachment repaired beforehand). All 60 were downloaded back through WebDAV
and matched their Zotero metadata hashes.

Uploads now publish the WebDAV ZIP before its XML properties and then update
the attachment's matching filename, MD5 and millisecond timestamp with an
optimistic version precondition. Existing remote revisions are not overwritten.
Retries reuse existing keys, including records whose bytes were previously
uploaded to the wrong backend. Successful attachment checks inspect WebDAV,
not just the presence of an MD5 in Zotero metadata. Group libraries continue
using Zotero File Storage. The status tool identifies the selected backend.

The repaired PDFs are downloaded back through WebDAV and compared against
their Zotero metadata hash. The receipt is stored on Asahi at
`/home/mv/docker-binds/academic-mcp/cache/webdav-repair-20261101.json`.
The full suite passes 272 tests after integrating the current upstream main.


## Release deployment

The fixes are released through the repository's GitHub Actions multi-arch
Docker workflow. Deployment uses the immutable published commit tag under
`ghcr.io/veale/academic-mcp` rather than retaining the incremental source patch.
The existing container configuration, mounts and networks must be preserved,
with the previous container retained for rollback until health and MCP checks
pass. The released container replaces the initial incremental deployment
recorded above.
