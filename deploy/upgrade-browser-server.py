#!/usr/bin/env python3
"""Replace academic_mcp with a tested image, retaining an offline rollback.

Run on the Docker host. All environment, mounts, labels, restart policy and
network memberships and aliases are copied from the existing container;
addresses are assigned dynamically (see --keep-static-ips). Nothing is printed
from its secret-bearing configuration.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener


class Docker(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


def api(method, path, body=None):
    conn = Docker()
    conn.request(method, path, json.dumps(body).encode() if body is not None else None,
                 {"Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    if response.status >= 300 and response.status != 304:  # 304: already in that state
        raise RuntimeError("Docker API operation failed: " + data.decode()[:400])
    return json.loads(data) if data else None


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--name", default="academic_mcp")
    parser.add_argument("--health-port", type=int, default=8765)
    parser.add_argument("--health-path", default="/healthz")
    parser.add_argument("--config-patch", type=Path)
    parser.add_argument("--keep-static-ips", action="store_true",
                        help="retain addresses that were explicitly pinned (default: assign dynamically)")
    args = parser.parse_args()
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    # Pull before stopping anything: a missing image used to fail only after
    # the running server had been stopped.
    try:
        api("GET", f"/images/{quote(args.image, safe='')}/json")
    except RuntimeError:
        subprocess.run(["docker", "pull", "-q", args.image], check=True)
    original = api("GET", f"/containers/{quote(args.name, safe='')}/json")
    (args.backup_dir / "original-container.json").write_text(json.dumps(original))
    original_id = original["Id"]
    backup_name = args.name + "-before-maintenance-" + str(int(time.time()))
    networks = original["NetworkSettings"]["Networks"]
    endpoints = {}
    for name, info in networks.items():
        endpoint = {"Aliases": [a for a in (info.get("Aliases") or [])
                                if a not in {original_id, original_id[:12]}]}
        # Never turn a dynamically assigned address into a static one.  On
        # 13 November 2026 a Docker upgrade restarted every container; another
        # container took this one's former address first, and the pinned
        # address made academic_mcp fail with "Address already in use" and
        # stay down.  Peers reach it by name, so no address is needed.
        pinned = (info.get("IPAMConfig") or {}).get("IPv4Address")
        if pinned and args.keep_static_ips:
            endpoint["IPAMConfig"] = {"IPv4Address": pinned}
        endpoints[name] = endpoint
    config = original["Config"].copy()
    config["Image"] = args.image
    payload = {**config, "HostConfig": original["HostConfig"], "NetworkingConfig": {"EndpointsConfig": endpoints}}
    if args.config_patch:
        patch = json.loads(args.config_patch.read_text())
        host_patch = patch.pop("HostConfig", {})
        payload.update(patch)
        payload["HostConfig"].update(host_patch)
    new_id = None
    api("POST", f"/containers/{original_id}/stop?t=30")
    api("POST", f"/containers/{original_id}/rename?name={backup_name}")
    try:
        for name in networks:
            api("POST", f"/networks/{quote(name, safe='')}/disconnect", {"Container": original_id, "Force": True})
        new_id = api("POST", "/containers/create?name=" + quote(args.name, safe=''), payload)["Id"]
        api("POST", f"/containers/{new_id}/start")
        ip = api("GET", f"/containers/{new_id}/json")["NetworkSettings"]["Networks"]["mcp"]["IPAddress"]
        opener = build_opener(ProxyHandler({}))
        for _ in range(60):
            try:
                with opener.open(f"http://{ip}:{args.health_port}{args.health_path}", timeout=2) as response:
                    if response.status == 200:
                        print("Server healthy; rollback container: " + backup_name)
                        return
            except OSError:
                time.sleep(1)
        raise RuntimeError("Updated server did not become healthy")
    except Exception:
        if new_id:
            api("DELETE", f"/containers/{new_id}?force=true")
        for name in networks:
            api("POST", f"/networks/{quote(name, safe='')}/connect", {"Container": original_id,
                                                                    "EndpointConfig": endpoints[name]})
        api("POST", f"/containers/{original_id}/rename?name={quote(args.name, safe='')}")
        api("POST", f"/containers/{original_id}/start")
        raise


if __name__ == "__main__":
    main()
