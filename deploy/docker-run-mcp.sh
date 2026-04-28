#!/usr/bin/env bash
# Canonical `docker run` for the academic-mcp container.
# Edit the path variables below to match your server setup, then run this
# script once to start the container.  On subsequent updates, stop + remove
# the old container and re-run.
set -euo pipefail

# ---------------------------------------------------------------------------
# Edit these paths to match your Linux server.
# ---------------------------------------------------------------------------

# Full mirror of ~/Zotero from the MacBook (Mode A), or at minimum the
# sqlite directory (Mode B with Nextcloud WebDAV).
ZOTERO_MIRROR=/home/YOURUSER/zotero-mirror

# Nextcloud-on-disk path for Zotero attachments (Mode B only).
# If using Mode A only, you can remove the corresponding -v line below.
NEXTCLOUD_ZOTERO=/home/YOURUSER/nextcloud-data/YOURUSER/files/Zotero

# Drop-in config directory.  Place your .env file here.
CONFIG_DIR=/home/YOURUSER/academic-mcp-config

# Writable cache for Chroma index, PDFs, article JSON cache.
CACHE_DIR=/home/YOURUSER/academic-mcp-cache

# Docker network shared with llama_embed, scrapling_mcp, and your reverse proxy.
NETWORK=YOUR-DOCKER-NETWORK

# ---------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR" "$CACHE_DIR"

docker run -d \
    --name academic_mcp \
    --restart always \
    --network "$NETWORK" \
    --memory 8g \
    --memory-swap 16g \
    --label project=academic-mcp \
    -v "$ZOTERO_MIRROR":/zotero:ro \
    -v "$NEXTCLOUD_ZOTERO":/nextcloud/data/YOURUSER/files/Zotero:ro \
    -v "$CONFIG_DIR":/config \
    -v "$CACHE_DIR":/var/cache/academic-mcp \
    academic-mcp:latest
