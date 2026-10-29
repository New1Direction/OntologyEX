#!/usr/bin/env python3
"""Golden fixtures: representative inputs, their expected outputs, and why each last changed.

    python tests/golden.py check [--engine-dir DIR] [--require-rust]
    python tests/golden.py update --reason "why the expected outputs change"

Parity tests prove Python and Rust agree with each other; they cannot catch both changing
the same wrong way. These fixtures pin what the answers should be. Three inputs: the
operating-picture demo (skills, links, live health), and two small repositories kept
under tests/golden/repos (a shop and a clinic), each given a fixed git history with
fixed authors and dates and read against a fixed clock. For each, the outputs a person
or agent sees: discovery, suppliers, owners, compliance, the command center, entity
context, change impact and the weekly digest. Rust answers (discovery, context, change
impact) are checked against the same expected files when the engine is built.

Outputs are compared after normalizing what is not behaviour: timestamps and the
temporary directory. Ordering, numbers and identifiers are behaviour and stay.

Every expected file is listed in MANIFEST.json with its hash and the reason it last
changed, written by `update --reason`; `check` refuses an expected file edited by hand.
A disagreement prints the JSON paths that differ, so it can be read, not just counted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import command_center  # noqa: E402
import compliance  # noqa: E402
import digest  # noqa: E402
import discover_repo  # noqa: E402
import entity_context  # noqa: E402
import native_view_bridge  # noqa: E402
import operations  # noqa: E402
import ownership  # noqa: E402

GOLDEN = ROOT / "tests/golden"
EXPECTED = GOLDEN / "expected"
MANIFEST = GOLDEN / "MANIFEST.json"
CHANGES = GOLDEN / "CHANGES.md"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # the clock fixed-history fixtures are read against
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")
# Who changed what, and when: (author, days before NOW, files touched). The first commit adds every file.
HISTORY = {
    "shop": [("Avery Stone", 200, None), ("Avery Stone", 120, ["api/src/server.ts"]),
             ("Bea Kim", 90, ["api/src/server.ts", "prisma/schema.prisma"]), ("Bea Kim", 60, ["api/src/server.ts"]),
             ("Cy Ortiz", 30, ["web/src/page.tsx"]), ("Cy Ortiz", 20, ["web/src/page.tsx"]),
             ("dependabot[bot]", 10, ["api/package.json"])],
    "clinic": [("Dee Park", 300, None), ("Dee Park", 100, ["app/main.py"]), ("Dee Park", 40, ["app/models.py"])],
}
CHANGED = {"shop": ["prisma/schema.prisma", "api/src/server.ts"], "clinic": ["app/models.py"]}


def normalize(value, root: str):
    """What is not behaviour: wall-clock times and where the fixture was built."""
    if isinstance(value, dict):
        return {k: normalize(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, root) for v in value]
    if isinstance(value, str):
        return TIME.sub("<time>", value.replace(root, "<root>"))
    return value


def _git(repo: Path, *args: str, author: str = "Fixture", when: datetime | None = None) -> None:
    email = re.sub(r"[^a-z]+", ".", author.lower()).strip(".") + "@fixture.invalid"
    stamp = (when or NOW).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp,
           "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": stamp, "LC_ALL": "C"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def fixture_repo(name: str, into: Path) -> Path:
    """A copy of tests/golden/repos/NAME with its fixed history: same content and commits every time."""
    repo = into / name
    shutil.copytree(GOLDEN / "repos" / name, repo)
    _git(repo, "init", "-q", "-b", "main")
    for number, (author, days, files) in enumerate(HISTORY[name]):
        when = NOW - timedelta(days=days)
        for rel in files or []:
            with (repo / rel).open("a", encoding="utf-8") as handle:
                handle.write(f"\n{'#' if not rel.endswith(('.ts', '.tsx', '.prisma')) else '//'} change {number}\n")
        _git(repo, "add", "-A", author=author, when=when)
        _git(repo, "commit", "-q", "-m", f"change {number}", author=author, when=when)
    return repo


def _bundle(command: list[str]) -> dict:
    run = subprocess.run([*command, "--bundle-only", "--discovery-engine", "python", "--view-engine", "python"],
                         capture_output=True, text=True, check=True, timeout=300)
    return json.loads(run.stdout)


def _rust(binary: Path | None, bundle: dict, *args: str):
    if binary is None:
        return None
    run = subprocess.run([str(binary), *args], input=json.dumps(bundle).encode(), capture_output=True, timeout=60)
    return json.loads(run.stdout) if run.returncode == 0 else {"refused": True}


def repo_outputs(name: str, tmp: Path, engines: dict) -> tuple[dict, dict, str]:
    """The Python outputs for one fixture repository, and the Rust answers to compare with them."""
    repo = fixture_repo(name, tmp)
    bundle = _bundle([sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(repo)])
    bundle["ownership"] = ownership.report(bundle, ownership.scan_repo(repo, NOW))  # against the fixed clock
    bundle["compliance"] = compliance.report(bundle, compliance.scan_repo(repo))
    focus = sorted(i for i, e in entity_context._entities(bundle).items()
                   if e["kind"] in ("declared_service", "external_supplier"))
    changes = {"files": [{"path": p} for p in CHANGED[name]]}
    out = {"discovery": discover_repo.discover(repo), "suppliers": bundle["suppliers"],
           "ownership": bundle["ownership"], "compliance": bundle["compliance"],
           "command_center": command_center.report(bundle),
           "context": {i: entity_context.context(bundle, i) for i in focus},
           "change_impact": entity_context.change_impact(bundle, changes)}
    rust = {}
    if engines.get("discover"):
        rust["discovery"] = native_view_bridge.native_discover(repo, engines["discover"])
    if engines.get("view"):
        rust["context"] = {i: _rust(engines["view"], bundle, "context", i) for i in focus}
        rust["change_impact"] = _rust(engines["view"], {**bundle, "changes": changes}, "change-impact")
    return out, rust, str(tmp)


def demo_outputs(tmp: Path, engines: dict) -> tuple[dict, dict, str]:
    """The operating-picture demo: skills, confirmed links, live health, an outage."""
    spec = importlib.util.spec_from_file_location("ontologyex_golden_demo", ROOT / "examples/operating-picture/demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    built = demo.build(tmp / "demo")
    bundle = _bundle(built["command"])
    folder = Path(built["repo"]) / ".ontologyex/operations"  # the exports again, against the fixed clock
    shutil.rmtree(folder)
    demo.write_operations(folder, NOW)
    bundle["operations"] = operations.with_comparison(operations.report(folder, now=NOW),
                                                      list(entity_context._entities(bundle).values()))
    report = command_center.report(bundle)
    business = sorted(i for i, e in entity_context._entities(bundle).items() if e["layer"] == "business")
    changes = {"files": [{"path": "domain/payments.py"}]}
    history = [digest.snapshot({**report, "items": report["items"][2:] + [
        {**report["items"][0], "id": "fixture-fixed", "title": "worker reports an error", "score": 80,
         "level": "CRITICAL"}]}, NOW - timedelta(days=9))]
    out = {"command_center": report, "operations": bundle["operations"],
           "context": {i: entity_context.context(bundle, i) for i in business},
           "change_impact": entity_context.change_impact(bundle, changes),
           "digest": digest.build(history, report, NOW, 7, {"link": [
               {"at": "2026-09-30T09:00:00Z", "by": "Dana", "action": "confirm", "note": "checked the handler",
                "row": {"binding": "payments.Payment", "component": "discovery:service:compose.yml:payments"}}]},
               None, digest.labels(bundle))}
    rust = {}
    if engines.get("view"):
        rust["context"] = {i: _rust(engines["view"], bundle, "context", i) for i in business}
        rust["change_impact"] = _rust(engines["view"], {**bundle, "changes": changes}, "change-impact")
    return out, rust, str(tmp)


def generate(engines: dict) -> tuple[dict[str, object], dict[str, object]]:
    """Every expected file's content, and every Rust answer keyed the same way."""
    outputs, rust = {}, {}
    with tempfile.TemporaryDirectory(prefix="ontologyex-golden-") as raw:
        tmp = Path(raw).resolve()
        for name, build in (("demo", lambda: demo_outputs(tmp, engines)),
                            *((n, lambda n=n: repo_outputs(n, tmp, engines)) for n in sorted(HISTORY))):
            found, native, root = build()
            for key, value in found.items():
                outputs[f"{name}/{key}.json"] = normalize(value, root)
            for key, value in native.items():
                rust[f"{name}/{key}.json"] = normalize(value, root)
    return outputs, rust


def encode(value) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def differences(expected, actual, path: str = "$", out: list | None = None, limit: int = 40) -> list[str]:
    """The JSON paths where two values differ, readable."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                out.append(f"- {path}.{key} (gone)")
            elif key not in expected:
                out.append(f"+ {path}.{key} = {json.dumps(actual[key])[:160]}")
            else:
                differences(expected[key], actual[key], f"{path}.{key}", out, limit)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            out.append(f"~ {path}: {len(expected)} items -> {len(actual)}")
        for i, (a, b) in enumerate(zip(expected, actual)):
            differences(a, b, f"{path}[{i}]", out, limit)
    elif expected != actual:
        out.append(f"~ {path}: {json.dumps(expected)[:120]} -> {json.dumps(actual)[:120]}")
    return out[:limit]


def engines(engine_dir: str | None) -> dict:
    found = {}
    for key, name in (("view", "ontologyex-view"), ("discover", "ontologyex-discover")):
        path = Path(engine_dir) / name if engine_dir else native_view_bridge.engine_binary(name)
        found[key] = path if path.exists() else None
    return found


def check(engine_dir: str | None, require_rust: bool) -> list[str]:
    """Problems found; empty when every output matches its expected file and every file its manifest entry."""
    problems = []
    manifest = json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {"outputs": {}}
    found = engines(engine_dir)
    if require_rust and not all(found.values()):
        return ["the Rust engine is required but not built: cargo build --bins --manifest-path engine/Cargo.toml"]
    outputs, rust = generate(found)
    stored = {p.relative_to(EXPECTED).as_posix() for p in EXPECTED.rglob("*.json")} if EXPECTED.exists() else set()
    for rel in sorted(stored - set(outputs)):
        problems.append(f"{rel}: expected file no longer produced; run update --reason")
    for rel, value in sorted(outputs.items()):
        path = EXPECTED / rel
        if not path.exists():
            problems.append(f"{rel}: no expected file; run update --reason")
            continue
        data = path.read_bytes()
        entry = manifest["outputs"].get(rel)
        if entry is None or entry.get("sha256") != hashlib.sha256(data).hexdigest():
            problems.append(f"{rel}: the expected file does not match MANIFEST.json (edited by hand? run update --reason)")
        expected = json.loads(data)
        if expected != value:
            problems.append(f"{rel}: Python differs from the expected output\n  " + "\n  ".join(differences(expected, value)))
        if rel in rust and rust[rel] != expected:
            problems.append(f"{rel}: Rust differs from the expected output\n  " + "\n  ".join(differences(expected, rust[rel])))
    return problems


def update(reason: str) -> list[str]:
    """Write every changed expected file and record why; returns the files that changed."""
    outputs, _ = generate(engines(None))
    manifest = json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {"schema_version": 1, "outputs": {}}
    changed = []
    for rel, value in sorted(outputs.items()):
        path, data = EXPECTED / rel, encode(value)
        if not path.exists() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            changed.append(rel)
        manifest["outputs"].setdefault(rel, {})
        if rel in changed or manifest["outputs"][rel].get("sha256") != hashlib.sha256(data).hexdigest():
            manifest["outputs"][rel] = {"sha256": hashlib.sha256(data).hexdigest(), "reason": reason,
                                        "updated": datetime.now(timezone.utc).date().isoformat()}
    for rel in sorted(set(manifest["outputs"]) - set(outputs)):
        manifest["outputs"].pop(rel)
        (EXPECTED / rel).unlink(missing_ok=True)
        changed.append(rel + " (removed)")
    MANIFEST.write_bytes(encode(manifest))
    if changed:
        with CHANGES.open("a", encoding="utf-8") as log:
            log.write(f"\n## {datetime.now(timezone.utc).date().isoformat()}: {reason}\n\n"
                      + "".join(f"- `{rel}`\n" for rel in changed))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    c = commands.add_parser("check")
    c.add_argument("--engine-dir", help="where ontologyex-view and ontologyex-discover are (default: the build)")
    c.add_argument("--require-rust", action="store_true", help="fail when the Rust engine is not built")
    u = commands.add_parser("update")
    u.add_argument("--reason", required=True, help="why the expected outputs change; recorded in MANIFEST and CHANGES")
    args = parser.parse_args()
    if args.command == "update":
        if len(args.reason.strip()) < 10:
            parser.error("--reason must say why, in a sentence")
        changed = update(args.reason.strip())
        print(json.dumps({"status": "UPDATED" if changed else "UNCHANGED", "changed": changed}, indent=1))
        return 0
    problems = check(args.engine_dir, args.require_rust)
    print("\n".join(problems) if problems else json.dumps({"status": "OK", "outputs": len(json.loads(
        MANIFEST.read_text("utf-8"))["outputs"])}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
