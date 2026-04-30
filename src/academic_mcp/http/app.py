"""FastAPI webapp application factory.

Mounts under /webapp (set by the Starlette parent).  The API lives at
/webapp/api/*.  Static assets (Phase 4+) are served from
src/academic_mcp/webapp_dist/ at /webapp/.

Auth: signed session cookie.  All /api/* routes except /api/health and
/api/auth/login require a valid cookie (checked by ``require_auth``).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import (
    _COOKIE_NAME,
    _SESSION_MAX_AGE,
    attempt_login,
    get_session_cookie,
    make_session_cookie,
    verify_session_cookie,
)
from .persistence import init_db

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def require_auth(
    request: Request,
    wa_session: Annotated[str | None, Cookie()] = None,
) -> None:
    """FastAPI dependency: 401 unless the request carries a valid session cookie."""
    value = wa_session or get_session_cookie(request)
    if not value or not verify_session_cookie(value):
        raise HTTPException(status_code=401, detail="unauthenticated")


AuthRequired = Depends(require_auth)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    await init_db()
    yield


def create_webapp() -> FastAPI:
    """Return the FastAPI instance that mounts under /webapp."""
    app = FastAPI(
        title="Academic MCP Webapp",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=_lifespan,
    )

    # ------------------------------------------------------------------
    # /api/health  — no auth
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    # ------------------------------------------------------------------
    # /api/auth/login  — no auth
    # ------------------------------------------------------------------

    @app.post("/api/auth/login")
    async def login(body: LoginRequest, request: Request, response: Response) -> dict:
        ok = await attempt_login(request, body.password)
        if not ok:
            raise HTTPException(status_code=401, detail="invalid_password")
        token = make_session_cookie()
        response.set_cookie(
            key=_COOKIE_NAME,
            value=token,
            max_age=_SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=True,
            path="/webapp",
        )
        return {"ok": True}

    # ------------------------------------------------------------------
    # /api/auth/logout  — auth required
    # ------------------------------------------------------------------

    @app.post("/api/auth/logout", dependencies=[AuthRequired])
    async def logout(response: Response) -> dict:
        response.delete_cookie(key=_COOKIE_NAME, path="/webapp")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Domain routers (after auth endpoints so auth paths take precedence)
    # ------------------------------------------------------------------

    from .routes_search import router as search_router
    from .routes_article import router as article_router
    from .routes_citations import router as citations_router
    from .routes_zotero import router as zotero_router

    app.include_router(search_router)
    app.include_router(article_router)
    app.include_router(citations_router)
    app.include_router(zotero_router)

    # Static / SPA fallback — must come last so it doesn't shadow API routes.
    from .static import mount_static
    mount_static(app)

    return app
