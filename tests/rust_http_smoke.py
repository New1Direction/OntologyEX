"""Opt-in Rust control-room HTTP parity across local, bounded read-only routes.

Run after cargo build --bin ontologyex-view. Never touches public GitHub or
production telemetry. Python remains the fallback *configured backend*, not an
automatic response fallback after Rust failure.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "ontology-extraction/scripts/control_room.py"
BIN = ROOT / "engine/target/debug/ontologyex-view"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def read(port, path):
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=6) as response:
        return json.load(response)


def normalized(value):
    """Different live request instants aren't a semantic mismatch."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()
                if k not in {"generated_at", "discovery_scanned_at", "evaluated_at"}}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def main():
    if not BIN.is_file():
        raise RuntimeError("compile Rust ontologyex-view before HTTP parity")
    with tempfile.TemporaryDirectory(prefix="ontologyex-native-http-") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / "compose.yml").write_text(
            "services:\n  web:\n    depends_on: [api]\n  api:\n    depends_on: [db]\n  db: {}\n" +
            "".join(f"  s{i:03d}: {{}}\n" for i in range(140)), encoding="utf-8")
        processes = []
        ports = {}
        try:
            for engine in ("python", "rust"):
                port = free_port()
                cmd = [sys.executable, str(CONTROL), "--repo", str(repo),
                       "--port", str(port), "--view-engine", engine]
                proc = subprocess.Popen(
                    cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True,
                    env={**os.environ, "ONTOLOGYEX_GITHUB_TOKEN":"TEST_PARENT_SECRET"},
                )
                processes.append(proc)
                ports[engine] = port
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError(f"{engine} backend exited on startup")
                    try:
                        value = read(port, "/api/state")
                        assert value["execution_authorized"] is False
                        break
                    except (OSError, AssertionError):
                        time.sleep(.08)
                else:
                    raise RuntimeError(f"{engine} backend did not start")
            server, native = ports["python"], ports["rust"]
            endpoints = [
                "/api/state",
                "/api/view?lens=all&q=&offset=0",
                "/api/view?lens=all&q=&offset=90",
                "/api/view?lens=services&q=web&offset=0",
                "/api/view?lens=database&q=&offset=0",
                "/api/view?lens=all&q=missing&offset=0",
            ]
            root_id = "discovery:service:compose.yml:web"
            db_id = "discovery:service:compose.yml:db"
            for component in (root_id, db_id):
                endpoints.append("/api/incident?id=" + quote(component, safe=""))
                endpoints.append("/api/node?id=" + quote(component, safe=""))
                for depth in (1, 2):
                    endpoints.append(
                        "/api/neighborhood?id=" + quote(component, safe="") +
                        "&depth=" + str(depth))
            start = time.perf_counter()
            for endpoint in endpoints:
                py, rs = read(server, endpoint), read(native, endpoint)
                if normalized(py) != normalized(rs):
                    keys = set(py) | set(rs)
                    different = [k for k in sorted(keys)
                                 if normalized(py.get(k)) != normalized(rs.get(k))]
                    raise AssertionError(
                        f"Rust HTTP differs for {endpoint}: fields {different}")
                assert rs["execution_authorized"] is False
                if rs.get("nodes") is not None:
                    assert len(rs["nodes"]) <= 90
                if rs.get("edges") is not None:
                    assert len(rs["edges"]) <= (
                        40 if rs["kind"] == "BOUNDED_NODE_DETAIL" else 300)
            elapsed = (time.perf_counter() - start)*1000
            for port in (server, native):
                try:
                    read(port, "/api/view?lens=not-a-lens&q=&offset=0")
                except HTTPError as exc:
                    assert exc.code == 400
                else:
                    raise AssertionError("unsupported lens succeeded")
            print(f"Rust control-room HTTP parity: {len(endpoints)} real local "
                  f"queries, {len(endpoints)*2} responses; duration={elapsed:.1f}ms; "
                  "read-only, UNKNOWN health preserved")
        finally:
            for proc in processes:
                proc.terminate()
            for proc in processes:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)


if __name__ == "__main__":
    main()
