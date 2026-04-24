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

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/healthz", endpoint=handle_healthz),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
    app = wrap_app(app)

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", str(port)))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
