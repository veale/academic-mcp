# academic-mcp network deployment — step-by-step guide

This document describes how to deploy academic-mcp as a long-lived service on
a Linux server, accessible from MacBook (Claude Desktop) and phone (Claude
mobile) over Tailscale.  Follow the phases in order; getting it wrong produces
confusing failures.

---

## Architecture overview

```
MacBook (Zotero source of truth)
  └─ rsync via launchd (event-driven + 30-min fallback) ───────┐
                                                          ↓
Linux server ─────────────────────────────────────────────
  Docker network: YOUR-DOCKER-NETWORK
  ├─ llama_embed       (already running — embeddings)
  ├─ scrapling_mcp     (stealth browser sidecar — optional)
  └─ academic_mcp      (this service — port 8765)
         │
         ├─ Tailscale ──► mcp.YOUR-TAILNET.ts.net/sse  (MacBook + phone)
         └─ Reverse proxy ──► mcp.YOUR-PUBLIC-DOMAIN.tld/sse  (optional public)
```

Three containers share a Docker network.  The MCP container reads from a
bind-mounted Zotero mirror and writes only to its own cache volume.

---

## Phase 1 — Linux server preparation

```bash
# SSH into the server
ssh YOURUSER@YOURSERVER.YOUR-TAILNET.ts.net

# Create directories
mkdir -p /home/YOURUSER/zotero-mirror          # receives zotero.sqlite from Mac
mkdir -p /home/YOURUSER/ft-cache              # receives .zotero-ft-cache files from Mac
mkdir -p /home/YOURUSER/academic-mcp-config
mkdir -p /home/YOURUSER/academic-mcp-cache

# Create the config env file (see deploy/docker-run-mcp.sh for full template)
cat > /home/YOURUSER/academic-mcp-config/.env << 'EOF'
# Embedding backend
SEMANTIC_PROVIDER=openai
SEMANTIC_MODEL=qwen3-embedding-0.6b
OPENAI_BASE_URL=http://llama_embed:8080/v1
OPENAI_API_KEY=sk-embed-key-from-llama-container

# Stealth sidecar
SCRAPLING_MCP_URL=http://scrapling_mcp:8000/sse

# Reranker
CROSS_RERANKER_MODEL=BAAI/bge-reranker-v2-m3
CROSS_RERANKER_FETCH=50

# MCP server auth — leave empty for Tailscale-only
MCP_API_KEY=

# Zotero paths inside the container
ZOTERO_SQLITE_PATH=/zotero/zotero.sqlite
ZOTERO_LOCAL_STORAGE=/zotero/storage
ZOTERO_LOCAL_ENABLED=false

# Search API keys
UNPAYWALL_EMAIL=you@example.com
SEMANTIC_SCHOLAR_API_KEY=
OPENALEX_API_KEY=
CORE_API_KEY=
SERPER_API_KEY=
BRAVE_SEARCH_API_KEY=

# Proxy
GOST_PROXY_URL=
EOF

# Verify llama_embed is reachable
docker exec llama_embed curl -s http://localhost:8080/health || \
  echo "Check that llama_embed is running"
```

---

## Phase 2 — MacBook SSH and rsync bootstrap

```bash
# On MacBook: install Homebrew rsync and sqlite3 (macOS ships with rsync 2.6.9 — too old)
brew install rsync sqlite

# Generate an SSH key if you don't have one
ssh-keygen -t ed25519 -C "zotero-sync"

# Push the key to the server
ssh-copy-id YOURUSER@YOURSERVER.YOUR-TAILNET.ts.net

# Verify passwordless login works
ssh YOURSERVER.YOUR-TAILNET.ts.net echo ok

# Install the sync script
cp deploy/macbook/zotero-sync.sh ~/Library/Scripts/zotero-sync.sh
chmod +x ~/Library/Scripts/zotero-sync.sh

# Copy the plist template and fill in your values:
#   YOURUSER          → your macOS username and server username
#   TAILSCALE_HOSTNAME → Tailscale MagicDNS name of the server
#   com.YOURNAME      → your own bundle prefix (not com.example)
# Then install and load:
cp deploy/macbook/com.example.zotero-sync.plist \
   ~/Library/LaunchAgents/com.YOURNAME.zotero-sync.plist
# (edit the copy — do NOT commit the filled-in version)
launchctl load ~/Library/LaunchAgents/com.YOURNAME.zotero-sync.plist

# Run an immediate first sync (close Zotero first for a clean sqlite copy)
# This can take 15–60 min depending on library size — run in a terminal
REMOTE_HOST=YOURSERVER.YOUR-TAILNET.ts.net \
REMOTE_USER=YOURUSER \
REMOTE_DIR=/home/YOURUSER/zotero-mirror \
REMOTE_FT=/home/YOURUSER/ft-cache \
~/Library/Scripts/zotero-sync.sh

# Verify from the server
ssh YOURUSER@YOURSERVER.YOUR-TAILNET.ts.net \
  "ls -la /home/YOURUSER/zotero-mirror/zotero.sqlite && \
   find /home/YOURUSER/ft-cache -name .zotero-ft-cache | wc -l"

# Check the launchd agent is scheduled
launchctl list | grep zotero-sync
```

### What the sync script does

The script performs three jobs on each run:

1. **SQLite snapshot** — uses `sqlite3 .backup` (safe while Zotero is open, uses
   read locks only). If the backup API fails the script exits non-zero and ships
   nothing — it never falls back to `cp`, which would capture an inconsistent
   mid-transaction state.
2. **Ship the snapshot** as `zotero.sqlite.new` then atomically `mv` it into
   place server-side, so the container never reads a half-written file.
3. **Ship `.zotero-ft-cache` files** — rsyncs only the plain-text extracts
   that Zotero's full-text indexer generates in `~/Zotero/storage/<key>/`.
   No PDFs or other attachments are transferred. These files feed the MCP
   server's semantic index, enabling chunk-level full-text search instead of
   abstract-only.

### SQLite lock note

Zotero holds an exclusive SQLite write lock while running but the `.backup` API
uses read locks and succeeds concurrently. On the rare failure (mid-transaction),
the script exits 3 and leaves the previous good copy in place server-side.

---

## Phase 3 — Build the Docker image

On the Linux server (must be ARM64 — do NOT build on x86 Mac):

```bash
# Clone or pull the repo on the server
git clone https://github.com/you/academic-mcp /opt/academic-mcp
# or: cd /opt/academic-mcp && git pull

cd /opt/academic-mcp
docker build -f docker/Dockerfile -t academic-mcp:latest .
# Takes ~3–5 min; image ~800 MB–1.2 GB
```

---

## Phase 4 — Start the MCP container

The container requires three host bind mounts:

| Host path | Container path | Notes |
|---|---|---|
| `/home/YOURUSER/zotero-mirror` | `/zotero` | SQLite mirror; read-only |
| `/home/YOURUSER/ft-cache` | `/zotero/storage` | ft-cache extracts; read-only |
| `/home/YOURUSER/academic-mcp-cache` | `/var/cache/academic-mcp` | Chroma index + PDF cache; writable |

The ft-cache bind (`/zotero/storage`) is what `ZOTERO_LOCAL_STORAGE` points to
inside the container.  It must be populated by the Mac-side sync (Phase 2)
before a semantic index rebuild will include full-text chunks.

```bash
# Edit deploy/docker-run-mcp.sh: set ZOTERO_MIRROR, FT_CACHE_DIR, CONFIG_DIR,
# CACHE_DIR to your actual paths, then:
bash deploy/docker-run-mcp.sh

# Check it started
docker logs academic_mcp --tail 30
# Should show uvicorn startup lines

# Verify healthcheck
docker exec academic_mcp curl -s http://127.0.0.1:8765/healthz
# → ok

# After ~30s Docker's HEALTHCHECK should flip to healthy
docker inspect academic_mcp --format '{{.State.Health.Status}}'
# → healthy
```

---

## Phase 5 — Scrapling sidecar (optional)

The MCP server works without the sidecar.  OA fetches (Unpaywall, CORE,
Semantic Scholar, Zotero-local) continue to function.  Only anti-bot
stealth fetches require Scrapling.

```bash
# Build Scrapling ARM64 image (do this once on Asahi)
git clone https://github.com/D4Vinci/Scrapling /tmp/scrapling
cd /tmp/scrapling
docker build -t scrapling-mcp:arm64 .

# Start the sidecar
bash deploy/docker-run-scrapling.sh

# Verify reachability from the MCP container
docker exec academic_mcp curl -s http://scrapling_mcp:8000/healthz || \
  echo "Check sidecar — endpoint may differ"
```

If the sidecar proves flaky on ARM64, set `SCRAPLING_MCP_URL=` (empty) in
`/home/YOURUSER/academic-mcp-config/.env` and restart `academic_mcp`.

---

## Phase 6 — Reverse proxy

See `deploy/reverse-proxy.md` for the full routing configuration.

Summary:
- Route `mcp.YOUR-TAILNET.ts.net` → `http://academic_mcp:8765` (Tailscale, no auth)
- Route `mcp.YOUR-PUBLIC-DOMAIN.tld` → `http://academic_mcp:8765` (public, API key required)

---

## Phase 7 — Client configuration

### Claude Desktop (macOS)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "academic": {
      "url": "https://mcp.YOUR-TAILNET.ts.net/sse",
      "transport": "sse"
    }
  }
}
```

For public access with API key:

```json
{
  "mcpServers": {
    "academic": {
      "url": "https://mcp.YOUR-PUBLIC-DOMAIN.tld/sse",
      "transport": "sse",
      "headers": {
        "Authorization": "Bearer sk-the-public-mcp-key"
      }
    }
  }
}
```

Remove the old `stdio`-based entry entirely. Claude Desktop will no longer
spawn local `uv run python -m academic_mcp` subprocesses.

### Claude mobile

Settings → MCP Connectors → Add remote server. Paste the URL. If using public
access, enter the Bearer token. Tailscale must be connected for the Tailscale
URL to resolve.

---

## Phase 8 — Initial semantic index rebuild

After the first Zotero sync and container start, rebuild the semantic index
against the mirrored data.

From Claude Desktop (after reconfiguring per Phase 7):
```
semantic_index_rebuild(force=True)
```

Or directly from the server shell:
```bash
docker exec academic_mcp uv run python -c "
import asyncio
from academic_mcp.semantic_index import get_semantic_index
asyncio.run(get_semantic_index().sync(force_rebuild=True))
"
```

Duration is roughly equivalent to the initial MacBook build, modulated by
Asahi's CPU.  Monitor progress:

```bash
docker exec academic_mcp watch -n 5 \
  'cat /var/cache/academic-mcp/chroma/status.json | python3 -m json.tool'
```

---

## Verification checklist

- [ ] `docker ps` shows `academic_mcp`, `scrapling_mcp`, `llama_embed` all healthy
- [ ] `docker exec academic_mcp curl -s http://127.0.0.1:8765/healthz` prints `ok`
- [ ] `curl -sH "Authorization: Bearer $MCP_API_KEY" https://mcp.YOUR-PUBLIC/sse` returns SSE stream (or 401 without header when auth is on)
- [ ] Claude Desktop on MacBook can run `list_zotero_libraries` and get a real response
- [ ] Claude Desktop can run `semantic_index_status` and see the expected provider/model/count
- [ ] launchd sync ran at least once: `.last-sync` timestamp in zotero-mirror is recent
- [ ] ft-cache files landed: `find /home/YOURUSER/ft-cache -name .zotero-ft-cache | wc -l` > 0
- [ ] `docker exec academic_mcp ls /zotero/storage/ | head` shows `<item_key>` directories
- [ ] `semantic_index_rebuild(force=True)` completes successfully
- [ ] Chroma index lives on the host: `du -sh /home/YOURUSER/academic-mcp-cache/chroma` is non-trivial
- [ ] `ps aux | grep academic_mcp` on MacBook shows **zero** local MCP processes

---

## Zotero mirror modes

### Mode A — ft-cache only (default)

The sync script ships `zotero.sqlite` (via SQLite `.backup`) and just the
`.zotero-ft-cache` plain-text extracts from `~/Zotero/storage/`. PDFs are not
transferred. The ft-cache files are mounted into the container at
`/zotero/storage` (separate bind from the sqlite mirror). This is the
default and recommended mode.

### Mode B — Nextcloud WebDAV for attachments

Attachments sync through Nextcloud on the server; only `zotero.sqlite`
needs rsync.

1. Zotero → Settings → Sync → File Syncing → select **WebDAV**
2. URL: `https://nextcloud.YOURDOMAIN/remote.php/dav/files/YOURUSER/Zotero/`
3. Username / app-password from Nextcloud
4. Click **Verify Server**

In the plist, add `SYNC_MODE=B` to `EnvironmentVariables` to skip `storage/`.

In `/config/.env` on the server, set:
```
ZOTERO_WEBDAV_LOCAL_PATH=/nextcloud/data/YOURUSER/files/Zotero
```

And add the corresponding bind-mount in `docker-run-mcp.sh`:
```
-v /home/YOURUSER/nextcloud-data/YOURUSER/files/Zotero:/nextcloud/data/YOURUSER/files/Zotero:ro
```

### Mode C — SSHFS direct mount (don't use this)

Mounting the MacBook's `~/Zotero/` over SSHFS directly into the container
fails whenever the MacBook is asleep or off-network.  That is exactly the
condition this whole setup is designed to handle.  Don't use SSHFS.

---

## Updating the image

```bash
# On the Linux server
cd /opt/academic-mcp
git pull
docker build -f docker/Dockerfile -t academic-mcp:latest .
docker stop academic_mcp && docker rm academic_mcp
bash deploy/docker-run-mcp.sh
```

The Chroma index and PDF cache survive updates (they are on the persistent
cache volume at `/var/cache/academic-mcp`). No index rebuild is required
unless the embedding model or provider changed.

> **Note:** The Chroma index is written to `SEMANTIC_CACHE_DIR`, which the
> Dockerfile sets to `/var/cache/academic-mcp/chroma`. Ensure that path is
> on the persistent volume bind (not the container overlay layer) or the
> index will be lost on every container recreate.
