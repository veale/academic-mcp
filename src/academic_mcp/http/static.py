from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

_DIST = Path(__file__).parent.parent / "webapp_dist"
_INDEX = _DIST / "index.html"

router = APIRouter()


def mount_static(app) -> None:
    """Mount the built Vite assets and add the SPA fallback route.

    Call this after all API routers are registered so the wildcard doesn't
    shadow them.  No-op when the dist directory does not exist (dev mode).
    """
    if not _DIST.exists():
        return

    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        static_file = _DIST / full_path
        if static_file.exists() and static_file.is_file():
            return FileResponse(static_file)
        if _INDEX.exists():
            return FileResponse(_INDEX)
        return HTMLResponse("<p>Frontend not built. Run <code>npm run build</code> inside webapp/.</p>", status_code=503)
