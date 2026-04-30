"""API-key authentication for SSE deployments.

Activated when ``MCP_API_KEY`` is set in the environment.  Every HTTP
request (SSE connect and message POST) must carry
``Authorization: Bearer <key>`` matching ``MCP_API_KEY``.

When ``MCP_API_KEY`` is unset, :func:`wrap_app` is a no-op.  This is the
correct mode for Tailscale-only deployments where network reachability
already provides authentication.

Constant-time comparison (``secrets.compare_digest``) to avoid timing
side-channels.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class _ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, expected_key: str) -> None:
        super().__init__(app)
        self._expected = expected_key

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        # Only enforce Bearer auth on MCP transport paths; let /healthz,
        # /webapp/*, and /trigger-sync through without a Bearer token.
        if not (path == "/mcp" or path.startswith("/mcp/") or path.startswith("/messages")):
            return await call_next(request)

        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse(
                {"error": "missing_or_malformed_authorization_header"},
                status_code=401,
            )
        presented = header[len("Bearer "):].strip()
        if not secrets.compare_digest(presented, self._expected):
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        return await call_next(request)


def wrap_app(app: Starlette) -> Starlette:
    """Wrap *app* with API-key auth if ``MCP_API_KEY`` is set."""
    key = os.getenv("MCP_API_KEY", "").strip()
    if not key:
        return app
    app.add_middleware(_ApiKeyMiddleware, expected_key=key)
    return app
