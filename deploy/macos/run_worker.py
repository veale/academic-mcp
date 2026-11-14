#!/usr/bin/env python3
"""Login-session supervisor: SSH tunnel, ordinary Chrome, then browser worker."""
import ipaddress
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

STATE = Path.home() / "Library/Application Support/Academic Browser"
HERE = Path(__file__).resolve().parent
UV = Path.home() / ".local/bin/uv"
children = []
stopping = False


def stop(*unused):
    global stopping
    stopping = True
    for process in reversed(children):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)


def remember_children():
    (STATE / "supervisor-children.json").write_text(json.dumps([p.pid for p in children]))


def clean_orphans(config):
    try:
        pids = json.loads((STATE / "supervisor-children.json").read_text())
    except FileNotFoundError:
        return
    for pid in pids:
        try:
            command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], stderr=subprocess.DEVNULL).decode()
            owns = str(HERE / "browser_helper.py") in command or f"127.0.0.1:{config['tunnel_port']}:" in command
            if owns and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, subprocess.CalledProcessError):
            pass


def browser_ready():
    try:
        descriptor = json.loads((STATE / "browser.json").read_text())
        command = subprocess.check_output(["ps", "-p", str(descriptor["pid"]), "-o", "command="], stderr=subprocess.DEVNULL).decode()
        if str(STATE / "profile") not in command or not descriptor.get("port"):
            return False
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{descriptor['port']}/json/version", timeout=2):
            return True
    except Exception:
        return False


def main():
    os.umask(0o077)
    config = json.loads((STATE / "worker.json").read_text())
    lock = (STATE / "supervisor.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clean_orphans(config)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        tunnel = helper = None
        try:
            # Remote Docker DNS is container-local; resolve the current address
            # on every reconnect instead of pinning an ephemeral container IP.
            remote = "docker inspect academic_mcp --format '{{(index .NetworkSettings.Networks \"mcp\").IPAddress}}'"
            address = subprocess.check_output(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                               config["ssh_host"], remote], timeout=20).decode().strip()
            ipaddress.ip_address(address)
            tunnel = subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-L",
                f"127.0.0.1:{config['tunnel_port']}:{address}:8765", config["ssh_host"]], start_new_session=True)
            children.append(tunnel)
            remember_children()
            if not browser_ready():
                startup = "https://www.ucl.ac.uk/library/"
                try:
                    recovery = json.loads((STATE / "active-job.json").read_text())
                    startup = recovery.get("resolved_url") or recovery.get("request_url") or startup
                except FileNotFoundError:
                    pass
                subprocess.run([str(UV), "run", "--script", str(HERE / "browser_helper.py"), "launch",
                                "--proxy", config["proxy"], "--control", "--url", startup], check=True)
            helper = subprocess.Popen([str(UV), "run", "--script", str(HERE / "browser_helper.py"), "serve"], start_new_session=True)
            children.append(helper)
            remember_children()
            while not stopping and tunnel.poll() is None and helper.poll() is None:
                time.sleep(1)
        except Exception as exc:
            print("Worker supervisor: " + type(exc).__name__ + ": " + str(exc), flush=True)
        finally:
            for process in [helper, tunnel]:
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                if process in children:
                    children.remove(process)
            remember_children()
        if not stopping:
            time.sleep(5)


if __name__ == "__main__":
    main()
