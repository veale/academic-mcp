# Managed Scrapling on Asahi

The service uses Scrapling 0.5.1 from a digest-pinned, multi-architecture image,
with matching Patchright Chromium installed. Startup runs Python directly:
there is no dependency resolution, download or implicit upgrade on restart.

The management wrapper keeps the upstream tools and provides the previous
`open_session`, `get`, `bulk_get`, `fetch`, `bulk_fetch`, `stealthy_fetch`,
`bulk_stealthy_fetch` and `screenshot` names. Their published input schemas and
structured response shapes are retained for existing connector clients.
Legacy session settings persist unless a fetch explicitly overrides them.
Both root and `/mcp` public connector URL styles work through the existing
gateway; duplicate prefixes and trailing slashes do not cause POST redirects.

## What was broken

The old custom 0.4.14 service could fetch with HTTP but failed browser startup.
It retained browser/driver processes even with an empty session registry. It
had suffered an OOM kill and reached 724 tasks against a PID limit of 768,
using approximately 3.5 GiB of its 4 GiB limit. Chromium failed with
`pthread_create: Resource temporarily unavailable`. It had no health check.

## Lifecycle and recovery

- One browser operation at a time; HTTP operations have a separate pool of eight.
- At most two reusable browser sessions, each with one page, plus eight HTTP sessions.
- Sessions expire after 30 minutes of inactivity and report an explicit reopen error.
- Browser operations queue for at most 30 seconds and execute for at most 120 seconds.
- Failed or cancelled one-shot launches clean up their newly created driver processes.
- Idle orphan drivers are reaped after registered browser sessions have closed.
- Legacy screenshot navigation/capture holds one browser lock, avoiding cross-call races.
- Bulk compatibility calls accept at most 25 URLs, processed without spawning an
  unbounded collection of browsers.
- A local JavaScript rendering probe runs every five minutes when browsers are idle.
  Probe failures retry every 30 seconds and trigger a worker restart after three failures.
- Docker checks `/healthz` every 30 seconds. Its checker can kill a frozen worker
  after three consecutive failures, allowing `restart: unless-stopped` to recover it.
  A failed maintenance task also restarts the worker instead of dying silently.
- Tini forwards termination to the process group and reaps child processes.

Container limits are 4 GiB RAM, no extra swap, two CPU cores, 1,024 tasks and
512 MiB shared memory. These protect the rest of the host; increasing the PID
limit alone would not have fixed the leak.

`SCRAPLING_SESSION_IDLE_SECONDS` and `SCRAPLING_CALL_TIMEOUT_SECONDS` adjust the
session idle and operation deadlines. Defaults above are installed on Asahi.

## Authentication and persistence

Scrapling requires `SCRAPLING_MCP_AUTH_TOKEN`. Academic sends it on its internal
MCP connection; the OAuth gateway adds it through `--proxy-bearer-token` after
validating the user's OAuth token. The backend token is not an end-user login.

The gateway's `/data` is now mounted persistently. Its existing private key,
session secret and database were copied consistently before replacement; signing
keys and the cookie secret were verified unchanged. This prevents replacement
of the gateway from discarding registrations and keys.

ChatGPT can independently mark an app connection as requiring reauthentication.
During the failed app check, no tool request appeared in the gateway logs.
Reconnect the app once in ChatGPT to renew its credentials; restarting a healthy
backend does not clear an app connection's reauthentication requirement.
If reconnection instead reports `invalid_client` / `Client does not exist`, the
saved registration is missing. The November 2026 reconnect attempt used an ID
absent from both the live database and the pre-maintenance gateway database.
Both installed ChatGPT entries (`Scrapling` and `Scrapling MV`) returned
reauthentication errors when called, despite one displaying as connected.
Create a fresh custom MCP plugin using `https://scrapling-auth.michae.lv/`, OAuth
and dynamic client registration (DCR), without reusing the old client ID or
secret. Verify the fresh connection before removing old entries. Never invent a
replacement registration under an unknown client ID or disable authentication
to accommodate stale credentials.

Private runtime files, tokens and rollback container specifications live under
`/home/mv/docker-binds/scrapling-mcp/managed/` and are not in this repository.
The gateway data lives in the adjacent `auth-data/` directory. Do not replace
or delete that directory when updating the image.

## Deploy and check

```sh
docker build -t local/scrapling-mcp:managed-0.5.1 deploy/scrapling
SCRAPLING_ENV_FILE=/private/backend.env bash deploy/docker-run-scrapling.sh
```

For an existing installation, `deploy/upgrade-browser-server.py` supports
`--name`, `--health-port`, `--health-path` and a private `--config-patch` file.
It preserves network addresses, mounts, environment and labels, keeps a stopped
rollback container and restores it automatically on startup failure. The Asahi
release bundle includes its deployment-specific private patches.

```sh
docker inspect scrapling-mcp --format '{{.State.Health.Status}}'
docker stats --no-stream scrapling-mcp
docker logs --tail 50 scrapling-mcp
docker exec scrapling-mcp /app/.venv/bin/python /opt/scrapling/healthcheck.py
```

The health JSON exposes active operations, session count, last browser check
and failure count without credentials. Do not publish Docker credentials or
the private rollback JSON when reporting a failure.

## Validation

The replacement was tested with MCP SDK 1.x clients and the new server runtime:
HTTP fetching, full HTML, real JavaScript rendering, legacy bulk calls, sessions,
screenshots, session close, repeated one-shot browser calls, UCL SOCKS routing
and the authenticated public OAuth gateway. A real external browser fetch and
UCL exit-IP check both returned HTTP 200. The service returned to approximately
100 MiB and two processes after repeated work.

A deliberately stopped test worker was recovered automatically by the health
checks and Docker restart policy. The lifecycle unit tests run inside the image:

```sh
docker run --rm --network none --security-opt label=disable \
  -v "$PWD/deploy/scrapling/test_server.py:/tmp/test_server.py:ro" \
  --entrypoint /app/.venv/bin/python local/scrapling-mcp:managed-0.5.1 /tmp/test_server.py
```

Academic's adapter also understands `browser_fetch_once` and native structured
responses, sends the backend credential, preserves document heads/citation
metadata and rejects empty/error envelopes rather than treating JSON as HTML.
It no longer chooses an unrelated session-management tool when tool names change.

Browser sessions live in memory; a restart requires opening them again. This
does not discard gateway OAuth registrations or signing keys. Publisher blocks
and expired institutional logins remain distinct from service health.
