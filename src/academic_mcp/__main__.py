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
        "--transport", choices=["stdio", "sse"], default="stdio",
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

    try:
        from .server import _ensure_semantic_background_sync
    except ImportError:
        from academic_mcp.server import _ensure_semantic_background_sync

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
        try:
            from .server import _ensure_semantic_background_sync, _semantic_sync_task
        except ImportError:
            from academic_mcp.server import _ensure_semantic_background_sync, _semantic_sync_task

        already_running = bool(_semantic_sync_task and not _semantic_sync_task.done())
        _ensure_semantic_background_sync(max_age_hours=0)

        info = {
            "already_running_before_call": already_running,
            "task_running_now": bool(_semantic_sync_task and not _semantic_sync_task.done()),
        }
        return PlainTextResponse(_json.dumps(info))

    from contextlib import asynccontextmanager

    async def _startup_sync():
        # Give uvicorn a moment to finish binding before we start background work.
        await asyncio.sleep(5)
        try:
            from .server import _ensure_semantic_background_sync
        except ImportError:
            from academic_mcp.server import _ensure_semantic_background_sync
        import logging as _logging
        _logging.getLogger(__name__).info("Startup: triggering semantic index sync check")
        _ensure_semantic_background_sync(max_age_hours=0)

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


if __name__ == "__main__":
    main()
