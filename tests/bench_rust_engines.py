#!/usr/bin/env python3
"""Benchmark the Rust engines against the Python reference, as people run them.

- discovery: `discover_repo.py --repo` vs `ontologyex-discover --repo` on a
  generated repository near the discovery budgets (CLI to CLI, startup included),
- live receiver: OTLP/HTTP protobuf ingest through `feeds.py run` vs
  `ontologyex-feeds run` with concurrent clients (spans per second, resident memory).

    python tests/bench_rust_engines.py --discover BIN --feeds BIN [--seconds 8]

Prints one JSON document. Numbers depend on the machine; compare them side by side.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_feeds import proto_request  # noqa: E402


def build_repo(root: Path) -> None:
    """~5,900 files, 125 manifests: compose files with 40 services each, workflows, packages."""
    for i in range(35):
        services = "".join(f"  s{j}:\n    image: app:{j}\n    depends_on: [s{(j + 1) % 40}, s{(j + 7) % 40}]\n"
                           f"    environment: {{A: '1', B: \"two\", C: 3}}\n" for j in range(40))
        (root / f"svc{i}").mkdir(parents=True)
        (root / f"svc{i}" / "compose.yml").write_text("services:\n" + services)
        (root / f"svc{i}" / "package.json").write_text(json.dumps({"name": f"svc{i}", "dependencies": {f"d{k}": "1" for k in range(50)}}))
        (root / f"svc{i}" / "pyproject.toml").write_text(f'[project]\nname = "svc{i}"\ndependencies = ["a", "b"]\n')
        for k in range(165):
            (root / f"svc{i}" / f"module_{k}.py").write_text("x = 1\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    for w in range(20):
        (root / ".github" / "workflows" / f"w{w}.yml").write_text(f"name: Workflow {w}\non: [push]\njobs: {{}}\n")


def timed(command: list[str], runs: int) -> float:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rss_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def receiver(command: list[str], workdir: Path, port: int, seconds: float, clients: int) -> dict:
    config = {"schema_version": 1, "kind": "ONTOLOGYEX_FEEDS", "output": "obs.jsonl",
              "otlp": {"port": port, "services": {"payments": "discovery:service:compose.yml:payments"},
                       "window_seconds": 60, "emit_seconds": 5}}
    (workdir / "feeds.json").write_text(json.dumps(config))
    process = subprocess.Popen([*command, "run", "--config", str(workdir / "feeds.json")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    body = proto_request("payments", [(2, i % 7 == 0 and 2 or 0, float(i % 300)) for i in range(1000)])
    sent = [0] * clients
    stop = time.perf_counter() + seconds
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        def client(n):
            while time.perf_counter() < stop:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/traces", data=body, method="POST",
                                                 headers={"Content-Type": "application/x-protobuf"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read()
                sent[n] += 1

        threads = [threading.Thread(target=client, args=(n,)) for n in range(clients)]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started
        memory = rss_kib(process.pid)
    finally:
        process.terminate()
        process.wait(timeout=10)
    return {"requests_per_second": round(sum(sent) / elapsed, 1),
            "spans_per_second": round(sum(sent) * 1000 / elapsed), "rss_mib": round(memory / 1024, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover", type=Path, required=True)
    parser.add_argument("--feeds", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    python = sys.executable
    scripts = ROOT / "ontology-extraction" / "scripts"
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        build_repo(repo)
        files = sum(len(f) for _, _, f in os.walk(repo))
        py = timed([python, str(scripts / "discover_repo.py"), "--repo", str(repo)], args.runs)
        rs = timed([str(args.discover.resolve()), "--repo", str(repo)], args.runs)
        report = {"discovery": {"files": files, "manifests": 125, "python_seconds": round(py, 3),
                                "rust_seconds": round(rs, 3), "speedup": round(py / rs, 1)}}
        receivers = {}
        for name, command in (("python", [python, str(scripts / "feeds.py")]), ("rust", [str(args.feeds.resolve())])):
            work = Path(tmp) / name
            work.mkdir()
            receivers[name] = receiver(command, work, free_port(), args.seconds, args.clients)
        receivers["speedup"] = round(receivers["rust"]["spans_per_second"] / receivers["python"]["spans_per_second"], 1)
        report["otlp_receiver"] = {"clients": args.clients, "spans_per_request": 1000, **receivers}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
