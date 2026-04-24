"""Minimal healthcheck probe for Docker.  Returns non-zero if /healthz
is not 200 on the local port."""
import os
import sys
import urllib.request

port = os.getenv("MCP_PORT", "8765")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
