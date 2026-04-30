"""Tests for webapp auth — health, login, logout, protected routes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "test-secret-key")
    return "test-secret-key"


@pytest.fixture
def webapp(tmp_path, monkeypatch, api_key):
    """Return a configured FastAPI test app with a temp config dir."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    # Re-initialise auth module so it picks up the temp CONFIG_DIR.
    import importlib
    import academic_mcp.http.auth as auth_mod
    importlib.reload(auth_mod)
    import academic_mcp.http.app as app_mod
    importlib.reload(app_mod)
    return app_mod.create_webapp()


@pytest.fixture
def client(webapp):
    from httpx import ASGITransport, AsyncClient
    return AsyncClient(transport=ASGITransport(app=webapp), base_url="http://test")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_no_auth(client):
    async with client as c:
        resp = await c.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_correct_password(client, api_key):
    async with client as c:
        resp = await c.post("/api/auth/login", json={"password": api_key})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert "wa_session" in resp.cookies


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    async with client as c:
        resp = await c.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401
    assert "wa_session" not in resp.cookies


@pytest.mark.asyncio
async def test_login_empty_api_key_rejects(tmp_path, monkeypatch):
    """When MCP_API_KEY is unset, any password must be rejected."""
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    import importlib
    import academic_mcp.http.auth as auth_mod
    importlib.reload(auth_mod)
    import academic_mcp.http.app as app_mod
    importlib.reload(app_mod)
    app = app_mod.create_webapp()
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/auth/login", json={"password": "anything"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Protected routes (logout as proxy for any auth-required route)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_without_cookie_is_401(client):
    async with client as c:
        resp = await c.post("/api/auth/logout")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_with_valid_cookie(client, api_key):
    async with client as c:
        login_resp = await c.post("/api/auth/login", json={"password": api_key})
        assert login_resp.status_code == 200
        # The cookie has path=/webapp (production), so pass it explicitly in tests.
        cookie_value = login_resp.cookies["wa_session"]
        resp = await c.post("/api/auth/logout", cookies={"wa_session": cookie_value})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Rate-limiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_rate_limit(client, monkeypatch):
    """After 5 failed attempts from the same IP the 6th must be rate-limited."""
    import academic_mcp.http.auth as auth_mod
    # Reset buckets so previous tests don't interfere.
    auth_mod._buckets.clear()
    # Speed up the test: disable the 250 ms failure sleep.
    monkeypatch.setattr(auth_mod, "_FAILURE_MIN_DELAY", 0.0)

    async with client as c:
        for _ in range(5):
            await c.post("/api/auth/login", json={"password": "bad"})
        resp = await c.post("/api/auth/login", json={"password": "bad"})
    assert resp.status_code == 401
