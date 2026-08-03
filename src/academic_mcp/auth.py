"""API-key authentication for HTTP MCP deployments.

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

import json
import os
import secrets

from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send


class _ApiKeyMiddleware:
    """Pure ASGI auth middleware.

    ``BaseHTTPMiddleware`` wraps responses in an extra task/stream pair.  That
    is a poor fit for MCP's long-lived SSE and streaming HTTP responses and can
    turn disconnects into intermittent cancelled tool calls.  A small ASGI
    middleware avoids buffering or otherwise touching the response stream.
    """

    def __init__(self, app: ASGIApp, expected_key: str) -> None:
        self.app = app
        self._expected = expected_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Protect every MCP transport endpoint and the resource-intensive
        # manual sync trigger.  Health and the separately authenticated webapp
        # remain public to this middleware.
        protected = (
            path == "/mcp"
            or path.startswith("/mcp/")
            or path == "/sse"
            or path.startswith("/messages")
            or path == "/trigger-sync"
        )
        if not protected:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        header = headers.get(b"authorization", b"").decode("latin-1")
        if not header.startswith("Bearer "):
            await self._reject(send, "missing_or_malformed_authorization_header")
            return
        presented = header[len("Bearer "):].strip()
        if not secrets.compare_digest(presented, self._expected):
            await self._reject(send, "invalid_api_key")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, error: str) -> None:
        body = json.dumps({"error": error}, separators=(",", ":")).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="academic-mcp"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def wrap_app(app: Starlette) -> ASGIApp:
    """Wrap *app* with API-key auth if ``MCP_API_KEY`` is set."""
    key = os.getenv("MCP_API_KEY", "").strip()
    if not key:
        return app
    return _ApiKeyMiddleware(app, expected_key=key)
