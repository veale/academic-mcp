"""Cheap local health check; the service separately verifies real JS rendering."""
import json
import os
from pathlib import Path
import signal
from urllib.request import build_opener, ProxyHandler

counter = Path("/tmp/scrapling-health-failures")
try:
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:8000/healthz", timeout=5) as response:
        if not json.load(response)["ok"]:
            raise RuntimeError("Browser health check is failing")
    counter.write_text("0")
except Exception:
    try:
        failures = int(counter.read_text()) + 1
    except (ValueError, FileNotFoundError):
        failures = 1
    counter.write_text(str(failures))
    if failures >= 3:
        print("Scrapling is unresponsive or its browser health check is failing; restarting the worker", flush=True)
        pid = int(Path("/tmp/scrapling-server.pid").read_text())
        if b"/opt/scrapling/server.py" in Path(f"/proc/{pid}/cmdline").read_bytes():
            os.kill(pid, signal.SIGKILL)
    raise SystemExit(1)
