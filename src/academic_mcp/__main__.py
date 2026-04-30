"""Entry point: python -m academic_mcp"""

import argparse
import asyncio
import logging

from mcp.server.stdio import stdio_server

try:
    from .server import server
    from . import zotero_import
except ImportError:
    from academic_mcp.server import server
    from academic_mcp import zotero_import


def main():
    parser = argparse.ArgumentParser(description="Academic Research MCP Server")
    parser.add_argument(
        "--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Port for SSE transport (default: 8080)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.transport == "stdio":
        asyncio.run(_run_stdio())
    elif args.transport == "sse":
        _run_sse(args.port)
    elif args.transport == "streamable-http":
        _run_streamable_http(args.port)


async def _run_stdio():
    await zotero_import.ensure_auto_import_initialized()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def _nightly_sync_loop() -> None:
    """Run semantic sync once per day at ~02:00 local time if there are unprocessed items.

    Disabled by setting SEMANTIC_NIGHTLY_SYNC=false (default: true).
    """
    import os
    from datetime import datetime, timezone

    if os.getenv("SEMANTIC_NIGHTLY_SYNC", "true").lower() not in ("true", "1", "yes"):
        return

    logger = logging.getLogger(__name__)

    from .core.background import _ensure_semantic_background_sync

    try:
        from .semantic_index import SemanticIndexUnavailable, get_semantic_index
    except ImportError:
        from academic_mcp.semantic_index import SemanticIndexUnavailable, get_semantic_index

    while True:
        now = datetime.now()
        # Next 02:00 local time
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=target.day + 1)
        wait_seconds = (target - now).total_seconds()
        logger.debug("Nightly sync scheduler: sleeping %.0fs until %s", wait_seconds, target)
        await asyncio.sleep(wait_seconds)

        try:
            idx = get_semantic_index()
            status = await idx.status()
            done = status.get("chunks_done", 0)
            total = status.get("chunks_total", 0)
            if total > 0 and done < total:
                logger.info(
                    "Nightly sync: %d/%d chunks unprocessed — starting sync", total - done, total
                )
                _ensure_semantic_background_sync(max_age_hours=0)  # force trigger
            else:
                logger.debug("Nightly sync: index complete (%d/%d), skipping", done, total)
        except SemanticIndexUnavailable:
            pass
        except Exception as e:
            logger.warning("Nightly sync check failed: %s", e)


def _run_sse(port: int):
    import os
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.responses import PlainTextResponse
    import uvicorn

    try:
        from .auth import wrap_app
    except ImportError:
        from academic_mcp.auth import wrap_app

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        await zotero_import.ensure_auto_import_initialized()
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1],
                server.create_initialization_options(),
            )

    async def handle_healthz(request):
        return PlainTextResponse("ok")

    async def handle_trigger_sync(request):
        import json as _json
        from .core import background as _bg
        from .core.background import _ensure_semantic_background_sync

        already_running = bool(_bg._semantic_sync_task and not _bg._semantic_sync_task.done())
        _ensure_semantic_background_sync(max_age_hours=0)

        info = {
            "already_running_before_call": already_running,
            "task_running_now": bool(_bg._semantic_sync_task and not _bg._semantic_sync_task.done()),
        }
        return PlainTextResponse(_json.dumps(info))

    from contextlib import asynccontextmanager

    async def _startup_sync():
        # Give uvicorn a moment to finish binding before we start background work.
        await asyncio.sleep(5)
        from .core.background import _ensure_semantic_background_sync as _sync
        import logging as _logging
        _logging.getLogger(__name__).info("Startup: triggering semantic index sync check")
        _sync(max_age_hours=0)

    @asynccontextmanager
    async def lifespan(app):
        asyncio.create_task(_startup_sync())
        asyncio.create_task(_nightly_sync_loop())
        yield

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/healthz", endpoint=handle_healthz),
            Route("/trigger-sync", endpoint=handle_trigger_sync),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
    app = wrap_app(app)

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", str(port)))
    uvicorn.run(app, host=host, port=port)


def _run_streamable_http(port: int):
    """Run the server over MCP streamable HTTP transport.

    Single-endpoint chunked-HTTP transport (one POST per request, response
    streams back on the same connection). Avoids the SSE dual-channel
    session-loss failure mode.

    Mount path: /mcp  (clients should connect to https://host/mcp).
    """
    import os
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import PlainTextResponse, Response
    import uvicorn

    try:
        from .auth import wrap_app
        from .http.app import create_webapp
    except ImportError:
        from academic_mcp.auth import wrap_app
        from academic_mcp.http.app import create_webapp

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    async def handle_mcp(scope, receive, send):
        await zotero_import.ensure_auto_import_initialized()
        await session_manager.handle_request(scope, receive, send)

    async def handle_healthz(request):
        return PlainTextResponse("ok")

    async def handle_trigger_sync(request):
        import json as _json
        from .core import background as _bg
        from .core.background import _ensure_semantic_background_sync

        already_running = bool(_bg._semantic_sync_task and not _bg._semantic_sync_task.done())
        _ensure_semantic_background_sync(max_age_hours=0)

        info = {
            "already_running_before_call": already_running,
            "task_running_now": bool(_bg._semantic_sync_task and not _bg._semantic_sync_task.done()),
        }
        return PlainTextResponse(_json.dumps(info))

    async def _startup_sync():
        await asyncio.sleep(5)
        from .core.background import _ensure_semantic_background_sync as _sync
        import logging as _logging
        _logging.getLogger(__name__).info("Startup: triggering semantic index sync check")
        _sync(max_age_hours=0)

    @asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            asyncio.create_task(_startup_sync())
            asyncio.create_task(_nightly_sync_loop())
            yield

    # Both `/mcp` and `/mcp/` should reach the streamable-http handler so
    # clients work with or without the trailing slash. Starlette's Mount
    # with prefix `/mcp` only matches `/mcp/...`, not `/mcp` exactly, so
    # we add an explicit Route for the bare path.
    #
    # `handle_mcp` is a raw ASGI callable — it sends the response directly
    # via the `send` callable rather than returning a Starlette Response
    # object.  Starlette's `Route` machinery expects an endpoint to *return*
    # a Response, so we wrap it in a sentinel that satisfies that contract
    # without trying to send a second response.
    class _AlreadySentResponse(Response):
        async def __call__(self, scope, receive, send) -> None:
            pass  # response already sent by the session manager

    async def handle_mcp_root(request):
        await handle_mcp(request.scope, request.receive, request._send)
        return _AlreadySentResponse()

    # Mount the webapp when WEBAPP_ENABLED=true (default: true when MCP_API_KEY is set).
    _api_key_set = bool(os.getenv("MCP_API_KEY", "").strip())
    _webapp_enabled = os.getenv("WEBAPP_ENABLED", "true" if _api_key_set else "false").lower() in ("true", "1", "yes")
    _routes = [
        Route("/healthz", endpoint=handle_healthz),
        Route("/trigger-sync", endpoint=handle_trigger_sync),
        Route("/mcp", endpoint=handle_mcp_root, methods=["GET", "POST", "DELETE"]),
        Mount("/mcp/", app=handle_mcp),
    ]
    if _webapp_enabled:
        _routes.insert(0, Mount("/webapp", app=create_webapp()))

    app = Starlette(
        lifespan=lifespan,
        routes=_routes,
    )
    app = wrap_app(app)

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", str(port)))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
