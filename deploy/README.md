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
mkdir -p /home/YOURUSER/zotero-mirror
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
# On MacBook: install Homebrew rsync (macOS ships with rsync 2.6.9 — too old)
brew install rsync

# Generate an SSH key if you don't have one
ssh-keygen -t ed25519 -C "zotero-sync"

# Push the key to the server
ssh-copy-id YOURUSER@YOURSERVER.YOUR-TAILNET.ts.net

# Verify passwordless login works
ssh YOURSERVER.YOUR-TAILNET.ts.net ls /home/YOURUSER/zotero-mirror

# Install the sync script
cp deploy/macbook/zotero-sync.sh ~/Library/Scripts/zotero-sync.sh
chmod +x ~/Library/Scripts/zotero-sync.sh

# Edit deploy/macbook/com.example.zotero-sync.plist:
#   - Replace YOURUSER with your macOS and server username
#   - Replace YOURSERVER.YOUR-TAILNET.ts.net with your Tailscale hostname
# Then install and load:
cp deploy/macbook/com.example.zotero-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.zotero-sync.plist

# Run an immediate first sync (close Zotero first for a clean sqlite copy)
# This can take 15–60 min depending on library size — run in a terminal
REMOTE_HOST=YOURSERVER.YOUR-TAILNET.ts.net \
REMOTE_USER=YOURUSER \
REMOTE_DIR=/home/YOURUSER/zotero-mirror \
~/Library/Scripts/zotero-sync.sh

# Verify from the server
ssh YOURUSER@YOURSERVER.YOUR-TAILNET.ts.net ls -la /home/YOURUSER/zotero-mirror/zotero.sqlite

# Check the launchd agent is scheduled
launchctl list com.example.zotero-sync
```

### SQLite lock note

Zotero holds an exclusive SQLite lock while open.  If rsync runs while
Zotero is open, rsync will fail on `zotero.sqlite` but succeed on
everything else.  The MCP container's shadow-copy mechanism handles this
transparently: it reads the previous clean copy until the next successful
sync.

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

```bash
# Edit deploy/docker-run-mcp.sh: set ZOTERO_MIRROR, CONFIG_DIR, CACHE_DIR
# to your actual paths, then:
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
- [ ] launchd sync ran at least once: `ls -la /home/YOURUSER/zotero-mirror/zotero.sqlite` shows recent mtime
- [ ] `semantic_index_rebuild(force=True)` completes successfully
- [ ] `ps aux | grep academic_mcp` on MacBook shows **zero** local MCP processes

---

## Zotero mirror modes

### Mode A — Full folder mirror (rsync everything)

Default. The launchd agent rsyncs all of `~/Zotero/` including `storage/`.
Both `ZOTERO_SQLITE_PATH` and `ZOTERO_LOCAL_STORAGE` point into `/zotero`.

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

Chroma index and PDF cache survive updates (they are on the cache volume).
No index rebuild required unless the embedding model changed.
