"""Tests for the API-key authentication middleware (auth.py)."""

import os

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _make_app() -> Starlette:
    """Minimal app with a /healthz route and a protected /mcp route."""

    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def mcp_handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("secret")

    return Starlette(routes=[
        Route("/healthz", endpoint=healthz),
        Route("/mcp", endpoint=mcp_handler),
    ])


# ---------------------------------------------------------------------------
# No env var set — wrap_app should be a no-op
# ---------------------------------------------------------------------------

def test_wrap_app_is_noop_without_env_var(monkeypatch):
    monkeypatch.delenv("MCP_API_KEY", raising=False)

    from academic_mcp.auth import wrap_app

    app = _make_app()
    wrapped = wrap_app(app)

    assert wrapped is app, "wrap_app should return the same app object when MCP_API_KEY is unset"

    client = TestClient(wrapped)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


# ---------------------------------------------------------------------------
# MCP_API_KEY set — middleware active
# ---------------------------------------------------------------------------

@pytest.fixture()
def protected_client(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "s3cret")

    # Re-import wrap_app so it picks up the patched env
    import importlib
    import academic_mcp.auth as auth_mod
    importlib.reload(auth_mod)

    app = _make_app()
    wrapped = auth_mod.wrap_app(app)
    return TestClient(wrapped, raise_server_exceptions=True)


def test_missing_header_returns_401_when_configured(protected_client):
    response = protected_client.get("/mcp")
    assert response.status_code == 401
    assert response.json()["error"] == "missing_or_malformed_authorization_header"


def test_wrong_key_returns_401(protected_client):
    response = protected_client.get("/mcp", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_api_key"


def test_correct_key_passes_through(protected_client):
    response = protected_client.get("/mcp", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200
    assert response.text == "secret"


def test_healthz_bypasses_auth(protected_client):
    # No Authorization header — /healthz must still return 200
    response = protected_client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"
