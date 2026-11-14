#!/usr/bin/env bash
# Canonical `docker run` for the Scrapling stealth-browser sidecar.
#
# Build the pinned, managed sidecar for either ARM64 or AMD64:
#
#   docker build -t local/scrapling-mcp:managed-0.5.1 deploy/scrapling
#
# Then run this script to start the sidecar:
set -euo pipefail

NETWORK="${DOCKER_NETWORK:-mcp}"
: "${SCRAPLING_ENV_FILE:?Set SCRAPLING_ENV_FILE to a private env file containing SCRAPLING_MCP_AUTH_TOKEN}"

docker run -d \
    --name scrapling-mcp \
    --restart unless-stopped \
    --network "$NETWORK" \
    --memory 4g --memory-swap 4g --cpus 2 \
    --pids-limit 1024 --shm-size 512m \
    --env-file "$SCRAPLING_ENV_FILE" \
    --label project=academic-mcp \
    local/scrapling-mcp:managed-0.5.1
