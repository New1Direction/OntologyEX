#!/usr/bin/env python3
"""Differential test: Rust `ontologyex-discover` vs Python `discover_repo.py`.

Builds repositories from a fixed edge-case corpus plus seeded mutations of real
manifests (compose files, workflows, package.json, Cargo.toml, pyproject.toml),
runs both implementations, and requires byte-identical output. Usage:

    python tests/rust_discover_parity.py BINARY [--seeds N]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction" / "scripts"))
import discover_repo  # noqa: E402

COMPOSE = """services:
  web:
    image: nginx:1.27
    depends_on:
      - api
      - cache
  api:
    build: ./api
    environment:
      MODE: "prod"
      RETRIES: 3
      DEBUG: false
    depends_on:
      db:
        condition: service_healthy
  db:
    image: postgres:16
    ports: ["5432:5432"]
  cache: {image: redis}
"""
WORKFLOW = """name: CI checks
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          python -m pip install -r requirements.txt
          python -m unittest
      - name: 'Quoted: name'
        run: echo "done" # comment
"""
PACKAGE = """{
  "name": "web-app",
  "version": "1.2.3",
  "private": true,
  "scripts": {"build": "vite build", "test": "vitest"},
  "dependencies": {"react": "^18.3.1", "zod": "3.23.8"},
  "nested": [1, 2.5, -3e2, null, {"a": "\\u00e9\\n"}]
}
"""
CARGO = """[package]
name = "engine"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1", features = ["derive"] }
"""
PYPROJECT = """[project]
name = "service"
version = "0.3.0"
dependencies = ["requests>=2", 'pyyaml==6.0.3']

[tool.ruff]
line-length = 100
"""

# Each case: {relative path: text or bytes}. Paths ending in "@link" become symlinks.
EDGE_CASES: list[dict] = [
    {"compose.yml": COMPOSE, ".github/workflows/ci.yml": WORKFLOW, "package.json": PACKAGE,
     "engine/Cargo.toml": CARGO, "pyproject.toml": PYPROJECT, "go.mod": "module x\n", "api/Dockerfile": "FROM x\n"},
    {"compose.yaml": "services:\n  a: {}\n  b:\n    depends_on: [a, ghost, a]\n"},
    {"docker-compose.yml": "services:\n  'bad name': {}\n"},
    {"docker-compose.yaml": "services: []\n"},
    {"compose.yml": "services:\n  a: x\n"},
    {"compose.yml": "services:\n  a:\n    depends_on: a\n"},
    {"compose.yml": "services:\n  a:\n    depends_on: [1]\n"},
    {"compose.yml": "services:\n  a:\n    depends_on:\n"},
    {"compose.yml": "services:\n  a: &x {}\n  b: *x\n"},
    {"compose.yml": "services:\n  a: !reset {}\n"},
    {"compose.yml": "services:\n  a: {}\n  a: {}\n"},
    {"compose.yml": "services:\n  1: {}\n"},
    {"compose.yml": "services:\n  true: {}\n"},
    {"compose.yml": "services:\n  yes: {}\n  on: {}\n  null_: {}\n"},
    {"compose.yml": "services:\n  a:\n    labels: {when: 2024-02-30}\n"},
    {"compose.yml": "services:\n  a:\n    x: 2024-02-29 23:59:59.123456789 +23:59\n"},
    {"compose.yml": "services:\n  a:\n    x: 1:30\n    y: 0b_\n"},
    {"compose.yml": "services:\n  a:\n    x: <<\n"},
    {"compose.yml": "--- \nservices: {}\n--- \nservices: {}\n"},
    {"compose.yml": ""},
    {"compose.yml": "# only a comment\n"},
    {"compose.yml": b"\xef\xbb\xbfservices:\n  a: {}\n"},
    {"compose.yml": "\ufeffservices:\n  a: {}\n".encode("utf-16")},
    {"compose.yml": b"services:\n  a: {}\n\x00"},
    {"compose.yml": b"services:\n  \xff: {}\n"},
    {"compose.yml": "services:\n" + "".join(f"  s{i}:\n    depends_on: [s{i + 1}]\n" for i in range(150))},
    {"compose.yml": "x: " + "[" * 64 + "]" * 64 + "\nservices: {}\n"},
    {"compose.yml": "x: " + "[" * 65 + "]" * 65 + "\nservices: {}\n"},
    {"compose.yml": "services:\n  a:\n    depends_on: [" + ", ".join(f"b{i}" for i in range(129)) + "]\n"},
    {".github/workflows/a.yml": "name: 5\n", ".github/workflows/b.YML": "name: x\n",
     ".github/workflows/.c.yaml": "name: [x]\n", ".github/workflows/sub/d.yaml": "{}\n",
     ".github/workflows/e.yml": "- a\n", ".github/workflows/f.yml": "name: 'x: y'\n",
     ".github/workflows/package.json": '{"name": "from-json", "name": "last"}',
     ".github/workflows/pyproject.toml": 'name = "toml-name"\n', "x/.github/workflows/g.yml": "name: no\n"},
    {"package.json": "{\"a\": NaN, \"b\": [-Infinity, \"\\ud800\"], \"a\": 1}"},
    {"package.json": "[" * 512 + "]" * 512},
    {"package.json": "[" * 513 + "]" * 513},
    {"package.json": "{\"a\": 1,}"},
    {"package.json": "\ufeff{}"},
    {"package.json": b"{\"a\": \"\xff\"}"},
    {"Cargo.toml": "a = {b = 1,}\n"},
    {"Cargo.toml": "\ufeffa = 1\n"},
    {"Cargo.toml": "a = \"\\e\"\n"},
    {"Cargo.toml": "a.b.c = 1\n[a]\nd = 2\n"},
    {"Cargo.toml": "t = 07:32\n"},
    {"Cargo.toml": "a = 1e400\nb = inf\nc = -nan\nd = 1e-400\n"},
    {"Cargo.toml": "d = 1979-05-27T07:32:00-08:00\ne = 0x_1\nf = 1__0\n"},
    {"node_modules/package.json": "{}", ".hidden/package.json": "{}", "dist/compose.yml": "services: {}\n",
     "a/b/c/d/e/f/package.json": "{}", "a/b/c/d/e/f/g/package.json": "{}", "ok/package.json": "{}"},
    {"real/package.json": "{}", "link@link": "real", "linked.json@link": "real/package.json",
     "dangling@link": "nowhere"},
    {"spaced dir/ü/compose.yml": "services:\n  web: {}\n", "pct%/package.json": "{}"},
    {"big/package.json": "{\"pad\": \"" + "x" * (256 * 1024) + "\"}"},
    {f"m{i:03d}/package.json": "{}" for i in range(129)},
    {f"f{i:04d}.txt": "" for i in range(6001)},
]


def mutate(text: str, rng: random.Random) -> str:
    specials = list(":-?[]{},#&*!|>'\"%@`\t\n ") + ["- ", ": ", "\n  ", "---\n", "...\n", "<<", "~",
                                                   "0x_", "1:30", "2024-02-30", "yes", "!!str ", "\\u00e9",
                                                   "\ufeff", "\u00a0", "\u0085", "é", "\x7f", "1e400",
                                                   '"""', "'''", "1979-05-27", "07:32:00", "+inf", "nan",
                                                   "0x1F", "0o7", "0b1", "\\x41", "\\e", "=", ".", "[[", "]]"]
    chars = list(text)
    for _ in range(rng.randint(1, 4)):
        op = rng.random()
        pos = rng.randrange(len(chars) + 1)
        if op < 0.35 and chars:
            del chars[min(pos, len(chars) - 1)]
        elif op < 0.75:
            chars[pos:pos] = list(rng.choice(specials))
        elif op < 0.9 and chars:
            span = chars[pos:pos + rng.randint(1, 20)]
            at = rng.randrange(len(chars) + 1)
            chars[at:at] = span
        else:
            lines = "".join(chars).split("\n")
            rng.shuffle(lines)
            chars = list("\n".join(lines))
    return "".join(chars)


def write_repo(root: Path, files: dict) -> None:
    for rel, content in files.items():
        if rel.endswith("@link"):
            link = root / rel[:-5]
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(content, link)
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def python_output(repo: Path) -> str:
    try:
        return json.dumps(discover_repo.discover(repo), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    except (OSError, discover_repo.DiscoveryError, ValueError, TypeError, RecursionError) as exc:
        return json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True)


def rust_output(binary: Path, repo: Path) -> str:
    run = subprocess.run([str(binary), "--repo", str(repo)], capture_output=True, timeout=60)
    if run.stderr:
        raise AssertionError(f"rust wrote to stderr: {run.stderr[:500]!r}")
    return run.stdout.decode("utf-8").removesuffix("\n")


def compare(binary: Path, repo: Path, label: str) -> bool:
    py, rs = python_output(repo), rust_output(binary, repo)
    if py == rs:
        return True
    print(f"MISMATCH {label}")
    try:
        a, b = json.loads(py), json.loads(rs)
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                print(f"  {key}:\n    python {json.dumps(a.get(key))[:600]}\n    rust   {json.dumps(b.get(key))[:600]}")
    except ValueError:
        print(f"  python {py[:300]}\n  rust   {rs[:300]}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--seeds", type=int, default=400, help="mutation repositories per manifest kind")
    parser.add_argument("--start", type=int, default=0)
    args = parser.parse_args()
    binary = args.binary.resolve()
    failures = cases = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, files in enumerate(EDGE_CASES):
            repo = Path(tmp) / f"edge{i}" / "repo"
            write_repo(repo, files)
            cases += 1
            failures += not compare(binary, repo, f"edge case {i}: {sorted(files)[:3]}")
        seeds = {"compose.yml": COMPOSE, ".github/workflows/ci.yml": WORKFLOW, "package.json": PACKAGE,
                 "Cargo.toml": CARGO, "pyproject.toml": PYPROJECT}
        for target, seed in seeds.items():
            for s in range(args.start, args.start + args.seeds):
                rng = random.Random(f"{target}:{s}")
                # Eight mutated copies per repository keep the run fast; each is its own manifest.
                place = (lambda k: f".github/workflows/w{k}.yml") if target.startswith(".github") \
                    else (lambda k: f"d{k}/{target}")
                files = {place(k): mutate(seed, rng) for k in range(8)}
                repo = Path(tmp) / f"fuzz-{target.replace('/', '_')}-{s}" / "repo"
                write_repo(repo, files)
                cases += 1
                if not compare(binary, repo, f"{target} seed {s}"):
                    failures += 1
                    for rel, text in files.items():
                        print(f"    {rel}: {text!r}"[:400])
    print(json.dumps({"cases": cases, "mismatches": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
