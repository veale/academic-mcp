"""Managed Scrapling: bounded browsers, legacy connector tools and recovery."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from functools import wraps
import inspect
import json
import logging
import os
from pathlib import Path
import signal
import time
from typing import Any

import uvicorn
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from scrapling.core.ai.server import ScraplingMCPServer, ResponseModel
from mcp.server.mcpserver.exceptions import ToolError

log = logging.getLogger("scrapling.lifecycle")


def processes():
    """Driver process roots owned by this server, excluding unrelated processes."""
    entries = {}
    for folder in Path("/proc").iterdir():
        if not folder.name.isdigit():
            continue
        try:
            stat = (folder / "stat").read_text()
            fields = stat[stat.rfind(")") + 2:].split()
            command = (folder / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            entries[int(folder.name)] = (int(fields[1]), command)
        except (OSError, ValueError):
            continue
    owned = {os.getpid()}
    for _ in range(12):
        owned.update(pid for pid, (parent, _) in entries.items() if parent in owned)
    drivers = {pid for pid in owned if pid in entries and
               ("/patchright/driver/node" in entries[pid][1] or "/playwright/driver/node" in entries[pid][1])}
    return entries, drivers


def kill_drivers(roots):
    entries, drivers = processes()
    doomed = set(roots) & drivers
    for _ in range(12):
        doomed.update(pid for pid, (parent, _) in entries.items() if parent in doomed)
    born = {}
    for pid in doomed:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            born[pid] = stat[stat.rfind(")") + 2:].split()[19]
        except OSError:
            pass
    for pid in sorted(doomed, reverse=True):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if doomed:
        time.sleep(0.1)
    for pid, started in born.items():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            if stat[stat.rfind(")") + 2:].split()[19] == started:
                os.kill(pid, signal.SIGKILL)
        except (OSError, IndexError):
            pass
    return len(doomed)


class ManagedScrapling(ScraplingMCPServer):
    def __init__(self):
        super().__init__()
        self.browser_gate = asyncio.Semaphore(1)
        self.http_gate = asyncio.Semaphore(8)
        self.active = 0
        self.last_used = {}
        self.legacy_defaults = {}
        self.session_drivers = {}
        self.expired = deque(maxlen=64)
        self.raw = {}
        self.last_browser_check = None
        self.check_failures = 0
        self.idle_seconds = int(os.getenv("SCRAPLING_SESSION_IDLE_SECONDS", "1800"))
        self.deadline = float(os.getenv("SCRAPLING_CALL_TIMEOUT_SECONDS", "120"))
        for name in dir(ScraplingMCPServer):
            method = getattr(self, name)
            if not name.startswith("_") and inspect.iscoroutinefunction(method):
                self.raw[name] = method
                setattr(self, name, self.guarded(name, method))

    def guarded(self, name, method):
        browser = name.startswith("browser_") or name == "close_session"
        @wraps(method)
        async def call(*args, **kwargs):
            gate = self.browser_gate if browser else self.http_gate
            try:
                await asyncio.wait_for(gate.acquire(), timeout=30)
            except TimeoutError:
                raise ToolError("Scrapling is busy with another browser operation; retry this request shortly") from None
            before = processes()[1] if browser else set()
            self.active += 1
            try:
                sid = kwargs.get("session_id")
                if sid in self.expired and name not in {"browser_open", "open_request_session"}:
                    raise ToolError("This session expired after inactivity. Open a new browser/request session")
                if sid and sid in self._sessions:
                    self.last_used[sid] = time.monotonic()
                if name == "browser_open" and sum(e.session_type != "static" for e in self._sessions.values()) >= 2:
                    raise ToolError("Two browser sessions are already open. Close one before opening another")
                if name == "open_request_session" and sum(e.session_type == "static" for e in self._sessions.values()) >= 8:
                    raise ToolError("Eight HTTP sessions are already open. Close one before opening another")
                async with asyncio.timeout(self.deadline):
                    result = await method(*args, **kwargs)
                if name in {"browser_open", "open_request_session"}:
                    self.last_used[result.session_id] = time.monotonic()
                    self.expired = deque((old for old in self.expired if old != result.session_id), maxlen=64)
                if name == "browser_open":
                    self.session_drivers[result.session_id] = processes()[1] - before
                if browser and getattr(result, "status", None) is not None:
                    self.last_browser_check = time.time()
                    self.check_failures = 0
                return result
            except TimeoutError:
                if browser and kwargs.get("session_id"):
                    self.forget_session(kwargs["session_id"])
                raise ToolError(f"Scrapling stopped this operation after {self.deadline:g} seconds; its temporary browser is cleaned up") from None
            except ToolError:
                raise
            except Exception as exc:
                if browser and kwargs.get("session_id") and any(marker in str(exc).lower()
                    for marker in ["has been closed", "no longer alive", "target closed"]):
                    self.forget_session(kwargs["session_id"])
                log.exception("%s failed", name)
                reason = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
                raise ToolError(f"Scrapling {name} failed: {reason}") from None
            finally:
                self.active -= 1
                if name == "close_session":
                    self.forget_session(kwargs.get("session_id"), expired=False)
                elif browser and kwargs.get("session_id") and asyncio.current_task().cancelling():
                    self.forget_session(kwargs["session_id"])
                # A completed one-shot or failed startup must not leave a driver.
                # A successful browser_open deliberately keeps its new driver.
                if browser and name != "browser_open":
                    new = processes()[1] - before
                    if new:
                        log.warning("Reaped %s temporary browser processes", kill_drivers(new))
                elif browser and name == "browser_open" and 'result' not in locals():
                    kill_drivers(processes()[1] - before)
                gate.release()
        return call

    def forget_session(self, sid, expired=True):
        """Only kill drivers created by this session, even if another is open."""
        self._sessions.pop(sid, None)
        self.last_used.pop(sid, None)
        self.legacy_defaults.pop(sid, None)
        roots = self.session_drivers.pop(sid, set())
        if roots:
            kill_drivers(roots)
        if sid and expired:
            self.expired.append(sid)

    async def expire(self):
        if self.active or self.browser_gate.locked():
            return
        async with self.browser_gate:
            now = time.monotonic()
            for sid in list(self._sessions):
                if now - self.last_used.get(sid, now) > self.idle_seconds:
                    try:
                        await asyncio.wait_for(self.raw["close_session"](session_id=sid), timeout=10)
                    except Exception:
                        log.exception("Failed closing expired session %s", sid)
                    self.forget_session(sid)
                    log.info("Expired inactive session %s", sid)
            if not any(e.session_type != "static" for e in self._sessions.values()):
                leaked = processes()[1]
                if leaked:
                    log.warning("Reaped %s orphan browser processes", kill_drivers(leaked))

    async def maintenance(self):
        last_check = 0
        while True:
            await asyncio.sleep(5)
            await self.expire()
            interval = 30 if self.check_failures else 300
            browser_sessions = any(e.session_type != "static" for e in self._sessions.values())
            if self.active or browser_sessions or time.monotonic() - last_check < interval:
                continue
            last_check = time.monotonic()
            try:
                result = await self.browser_fetch_once(url="http://127.0.0.1:8000/healthz/render",
                    extraction_type="html", main_content_only=False, timeout=10000, google_search=False)
                ok = result.status == 200 and "render-ok" in "".join(result.content)
                if not ok:
                    raise RuntimeError("JavaScript rendering probe did not complete")
                self.last_browser_check = time.time()
                self.check_failures = 0
            except Exception:
                self.check_failures += 1
                log.exception("Browser health probe failed (%s/3)", self.check_failures)
                if self.check_failures >= 3:
                    log.error("Restarting unhealthy worker; Docker will recover it automatically")
                    os._exit(1)

    def register_legacy(self, server):
        schemas = json.loads(Path("/opt/scrapling/legacy-schemas.json").read_text())
        for name in ["open_session", "get", "bulk_get", "fetch", "bulk_fetch", "stealthy_fetch", "bulk_stealthy_fetch", "screenshot"]:
            schema = schemas[name]
            async def alias(_name=name, **kwargs):
                return await self.legacy(_name, kwargs)
            parameters = []
            required = set(schema.get("required", []))
            for key, prop in schema.get("properties", {}).items():
                # None distinguishes omitted options from explicit overrides.
                # The advertised legacy schema still carries the real defaults.
                default = inspect.Parameter.empty if key in required else None
                parameters.append(inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=Any))
            output = (dict[str, Any] if name == "open_session" else list[ResponseModel]
                      if name.startswith("bulk_") else Any if name == "screenshot" else ResponseModel)
            alias.__signature__ = inspect.Signature(parameters, return_annotation=output)
            alias.__annotations__ = {"return": output}
            alias.__name__ = name
            alias.__doc__ = "Compatibility tool for existing connectors; uses the managed Scrapling browser lifecycle."
            server.add_tool(alias, name=name, structured_output=name != "screenshot")
            # Keep the exact published input schema for already-connected clients.
            server._tool_manager._tools[name].parameters = schema

    async def legacy(self, name, values):
        values = {key: value for key, value in values.items() if value is not None}
        if name == "open_session":
            kind = values.pop("session_type", "stealthy")
            if kind not in {"stealthy", "dynamic"}:
                raise ToolError("Expected a dynamic or stealthy browser session")
            result = await self.browser_open(**accepted(self.raw["browser_open"], values))
            self.legacy_defaults[result.session_id] = accepted(self.raw["browser_fetch"], values)
            data = result.model_dump(mode="json")
            data["settings"] = {"max_pages": 1, "idle_timeout_seconds": self.idle_seconds}
            return data
        if name == "screenshot":
            async def sequence(**options):
                fetch_options = {**self.legacy_defaults.get(options["session_id"], {}),
                                 **accepted(self.raw["browser_fetch"], options), "extraction_type": "text"}
                fetch_options["timeout"] = min(fetch_options.get("timeout", 30000), 60000)
                await self.raw["browser_fetch"](**fetch_options)
                return await self.raw["browser_screenshot"](**accepted(self.raw["browser_screenshot"], options))
            return await self.guarded("browser_screenshot", sequence)(**values)
        bulk = name.startswith("bulk_")
        urls = values.pop("urls") if bulk else [values.pop("url")]
        if len(urls) > 25:
            raise ToolError("A bulk call supports at most 25 URLs; split this request into smaller batches")
        results = []
        for url in urls:
            args = {**values, "url": url}
            if name in {"get", "bulk_get"}:
                result = await self.make_request(**accepted(self.raw["make_request"], args))
            elif args.get("session_id"):
                args = {**self.legacy_defaults.get(args["session_id"], {}), **args}
                result = await self.browser_fetch(**accepted(self.raw["browser_fetch"], args))
            else:
                result = await self.browser_fetch_once(**accepted(self.raw["browser_fetch_once"], args))
            results.append(result)
        return results if bulk else results[0]


def accepted(method, values):
    return {key: value for key, value in values.items() if key in inspect.signature(method).parameters}


class NormalizePath:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # The OAuth gateway historically targets /mcp itself. Accept both
        # connector URL styles without a POST redirect or doubled-prefix 404.
        if scope.get("path") in {"/", "/mcp/", "/mcp/mcp", "/mcp/mcp/"}:
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
        await self.app(scope, receive, send)


def main():
    logging.basicConfig(level=logging.INFO)
    Path("/tmp/scrapling-server.pid").write_text(str(os.getpid()))
    Path("/tmp/scrapling-health-failures").write_text("0")
    instance = ManagedScrapling()
    if not instance._auth_token:
        raise RuntimeError("SCRAPLING_MCP_AUTH_TOKEN must be configured")
    server = instance._build_server("0.0.0.0", 8000)
    instance.register_legacy(server)
    app = server.streamable_http_app(stateless_http=True, host="0.0.0.0")
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            maintenance = asyncio.create_task(instance.maintenance())
            def supervise(task):
                if not task.cancelled() and task.exception():
                    log.error("Maintenance failed; restarting the worker", exc_info=task.exception())
                    os._exit(1)
            maintenance.add_done_callback(supervise)
            try:
                yield
            finally:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
                for sid in list(instance._sessions):
                    try:
                        await asyncio.wait_for(instance.raw["close_session"](session_id=sid), timeout=5)
                    except Exception:
                        pass
                kill_drivers(processes()[1])
    app.router.lifespan_context = lifespan

    async def health(request):
        ok = instance.check_failures == 0 and instance.last_browser_check is not None
        return JSONResponse({"ok": ok, "active": instance.active,
                             "sessions": len(instance._sessions), "browser_checked_at": instance.last_browser_check,
                             "browser_failures": instance.check_failures}, status_code=200 if ok else 503)

    async def render(request):
        return HTMLResponse('<html><head><title>Local browser probe</title></head><body><p id="probe"></p><script>document.getElementById("probe").textContent="render-"+"ok";</script></body></html>')
    app.routes.extend([Route("/healthz", health), Route("/healthz/render", render)])
    uvicorn.run(NormalizePath(app), host="0.0.0.0", port=8000, access_log=False)


if __name__ == "__main__":
    main()
