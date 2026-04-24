#!/usr/bin/env bash
# Canonical `docker run` for the Scrapling stealth-browser sidecar.
#
# Scrapling is an MCP server in its own right.  There is no maintained
# ARM64 image on Docker Hub; build locally from the Scrapling project:
#
#   git clone https://github.com/D4Vinci/Scrapling /tmp/scrapling
#   cd /tmp/scrapling && docker build -t scrapling-mcp:arm64 .
#
# Then run this script to start the sidecar:
set -euo pipefail

NETWORK=YOUR-DOCKER-NETWORK

docker run -d \
    --name scrapling_mcp \
    --restart always \
    --network "$NETWORK" \
    --memory 3g \
    --label project=academic-mcp \
    scrapling-mcp:arm64 \
        mcp --http --host 0.0.0.0 --port 8000
