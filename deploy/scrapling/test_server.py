"""Run inside the managed image with Python's unittest (no extra packages)."""
import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/opt/scrapling")
import server


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        with patch.dict("os.environ", {"SCRAPLING_MCP_AUTH_TOKEN": "test-only"}):
            return server.ManagedScrapling()

    async def test_failed_launch_cleans_new_driver(self):
        service = self.instance()
        fail = AsyncMock(side_effect=RuntimeError("launch failed"))
        with patch.object(server, "processes", side_effect=[({}, {1}), ({}, {1, 2})]), patch.object(server, "kill_drivers") as kill:
            with self.assertRaisesRegex(Exception, "launch failed"):
                await service.guarded("browser_open", fail)()
            kill.assert_called_once_with({2})
        self.assertEqual(service.active, 0)
        self.assertFalse(service.browser_gate.locked())

    async def test_cancelled_operation_releases_gate_and_driver(self):
        service = self.instance()
        async def blocked():
            await asyncio.sleep(60)
        with patch.object(server, "processes", side_effect=[({}, set()), ({}, {2})]), patch.object(server, "kill_drivers") as kill:
            task = asyncio.create_task(service.guarded("browser_fetch_once", blocked)())
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_called_once_with({2})
        self.assertEqual(service.active, 0)

    async def test_browser_session_limit_is_explicit(self):
        service = self.instance()
        service._sessions = {str(i): SimpleNamespace(session_type="stealthy") for i in range(2)}
        with self.assertRaisesRegex(Exception, "Two browser sessions"):
            await service.browser_open()

    async def test_idle_sessions_expire_but_recent_sessions_survive(self):
        service = self.instance()
        service._sessions = {sid: SimpleNamespace(session_type="static") for sid in ["old", "recent"]}
        service.last_used = {"old": 0, "recent": time.monotonic()}
        close = AsyncMock()
        service.raw["close_session"] = close
        with patch.object(server, "processes", return_value=({}, set())):
            await service.expire()
        close.assert_awaited_once_with(session_id="old")
        self.assertIn("recent", service._sessions)
        self.assertIn("old", service.expired)

    async def test_bulk_limit_is_explicit(self):
        service = self.instance()
        with self.assertRaisesRegex(Exception, "at most 25"):
            await service.legacy("bulk_get", {"urls": ["https://example.invalid/"] * 26})

    async def test_mcp_trailing_slash_does_not_redirect_or_drop_headers(self):
        captured = []
        async def app(scope, receive, send):
            captured.append(scope)
        scope = {"type": "http", "path": "/mcp/", "raw_path": b"/mcp/", "headers": [(b"authorization", b"Bearer test")]}
        await server.NormalizePath(app)(scope, None, None)
        self.assertEqual(captured[0]["path"], "/mcp")
        self.assertEqual(captured[0]["headers"], scope["headers"])
        self.assertEqual(scope["path"], "/mcp/")
        for path in ["/", "/mcp/mcp", "/mcp/mcp/"]:
            await server.NormalizePath(app)({**scope, "path": path}, None, None)
            self.assertEqual(captured[-1]["path"], "/mcp")

    async def test_legacy_screenshot_holds_one_browser_lock(self):
        service = self.instance()
        async def fetch(url, session_id, extraction_type="text", timeout=30000, wait=0):
            self.assertTrue(service.browser_gate.locked())
            self.assertEqual(service.active, 1)
            self.assertEqual(wait, 250)
        async def screenshot(session_id):
            self.assertTrue(service.browser_gate.locked())
            return []
        service.raw["browser_fetch"] = fetch
        service.raw["browser_screenshot"] = screenshot
        with patch.object(server, "processes", return_value=({}, set())):
            self.assertEqual(await service.legacy("screenshot", {"session_id":"one", "url":"https://example.invalid", "wait":250}), [])

    async def test_legacy_session_options_survive_omitted_fetch_arguments(self):
        service = self.instance()
        service.legacy_defaults["one"] = {"wait": 250, "timeout": 45000}
        async def raw(url, session_id, wait=0, timeout=30000):
            self.assertEqual(wait, 250)
            self.assertEqual(timeout, 45000)
            return server.ResponseModel(status=200, content=["OK"], url=url)
        service.raw["browser_fetch"] = raw
        service.browser_fetch = raw
        result = await service.legacy("fetch", {"session_id":"one", "url":"https://example.invalid", "wait":None})
        self.assertEqual(result.status, 200)

    async def test_closed_session_cleans_only_its_own_driver(self):
        service = self.instance()
        service._sessions = {sid: SimpleNamespace(session_type="stealthy") for sid in ["one", "two"]}
        service.session_drivers = {"one": {10}, "two": {20}}
        with patch.object(server, "kill_drivers") as kill:
            service.forget_session("one")
        kill.assert_called_once_with({10})
        self.assertIn("two", service._sessions)
        self.assertEqual(service.session_drivers["two"], {20})

    async def test_expired_session_id_can_be_explicitly_reopened(self):
        service = self.instance()
        service.expired.append("again")
        create = AsyncMock(return_value=SimpleNamespace(session_id="again"))
        with patch.object(server, "processes", return_value=({}, set())):
            result = await service.guarded("browser_open", create)(session_id="again")
        self.assertEqual(result.session_id, "again")
        self.assertNotIn("again", service.expired)


if __name__ == "__main__":
    unittest.main()
