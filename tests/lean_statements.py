#!/usr/bin/env python3
"""Trusted Lean statements, kept apart from the proofs and pinned.

proofs/lean has three kinds of file:

- OntologyEXModel/: the code being reasoned about, written in Lean (it mirrors Python and
  Rust; tests/conformance.py compares them on generated inputs);
- OntologyEXSpec/: the statements, as `def Name : Prop`. These are what is claimed. A
  reader trusts these and nothing else;
- OntologyEXProofs/: the proofs. Untrusted: Lean checks them.

STATEMENTS.json pins the sha256 of every Model and Spec file, so a changed statement fails
CI until it is recorded with a reason (`update --reason`, appended to STATEMENTS.md).
It also names the theorem that proves each statement. `check --lean` builds a file that
holds each theorem against its statement's exact proposition (`example : Spec.S := T`)
and prints the axioms each theorem uses; anything beyond propext, Quot.sound and
Classical.choice (a `sorry`, a new axiom) fails. The pattern follows formal-math's
Challenge/Solution split, where a comparator checks that a solution proves exactly the
frozen challenge; here the proofs import the statements, so Lean's type check is the
comparison and the pin keeps the statements themselves from drifting.

    python tests/lean_statements.py check            # hashes and file rules (no Lean needed)
    python tests/lean_statements.py check --lean     # also the audit, with lake on PATH
    python tests/lean_statements.py update --reason "why the statement changed"
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LEAN = ROOT / "proofs" / "lean"
MANIFEST = LEAN / "STATEMENTS.json"
CHANGES = LEAN / "STATEMENTS.md"
PINNED_DIRS = ("OntologyEXModel", "OntologyEXSpec")
PERMITTED_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}
# Spec and Model files define and state; they never prove or assume.
NO_PROOFS = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(theorem|lemma|axiom|instance|example)\b|\bsorry\b", re.M)
# Anything that lets compiled or unchecked code stand in for a proof.
UNSAFE = re.compile(r"\bsorry(?:Ax)?\b|^\s*axiom\b|\bnative_decide\b|implemented_by|@\[extern|\bunsafe\b|ofReduceBool", re.M)


def pinned_files() -> dict[str, Path]:
    return {p.relative_to(LEAN).as_posix(): p for d in PINNED_DIRS for p in sorted((LEAN / d).rglob("*.lean"))}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strip_comments(text: str) -> str:
    text = re.sub(r"/-.*?-/", "", text, flags=re.S)
    return re.sub(r"--[^\n]*", "", text)


def problems_without_lean() -> list[str]:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    found, problems = pinned_files(), []
    for rel in sorted(set(manifest["files"]) | set(found)):
        if rel not in found:
            problems.append(f"{rel}: pinned in STATEMENTS.json but missing")
        elif rel not in manifest["files"]:
            problems.append(f"{rel}: not pinned; run tests/lean_statements.py update --reason \"...\"")
        elif sha256(found[rel]) != manifest["files"][rel]["sha256"]:
            problems.append(f"{rel}: changed since it was pinned; a statement changed, so record why: "
                            "tests/lean_statements.py update --reason \"...\"")
    for rel, path in found.items():
        hit = NO_PROOFS.search(strip_comments(path.read_text("utf-8")))
        if hit:
            problems.append(f"{rel}: Model and Spec files state; they must not contain `{hit.group(0).strip()}`")
    for path in sorted((LEAN / "OntologyEXProofs").rglob("*.lean")):
        hit = UNSAFE.search(strip_comments(path.read_text("utf-8")))
        if hit:
            problems.append(f"{path.relative_to(LEAN).as_posix()}: `{hit.group(0).strip()}` is not allowed in a proof")
    spec_text = "\n".join(strip_comments(p.read_text("utf-8")) for r, p in found.items() if r.startswith("OntologyEXSpec/"))
    for statement, theorem in manifest["statements"].items():
        name = statement.rsplit(".", 1)[-1]
        if not re.search(rf"^\s*def\s+{re.escape(name)}\s*:\s*Prop\b", spec_text, re.M):
            problems.append(f"{statement}: no `def {name} : Prop` in OntologyEXSpec")
        if not theorem.startswith("OntologyEX.Proofs."):
            problems.append(f"{statement}: proved by {theorem}, which is not in OntologyEXProofs")
    return problems


def audit_source(manifest: dict) -> str:
    imports = sorted({"OntologyEXProofs." + t.split(".")[2] for t in manifest["statements"].values()})
    lines = [f"import {m}" for m in imports] + [""]
    for statement, theorem in manifest["statements"].items():
        lines.append(f"example : {statement} := {theorem}")
    lines.append("")
    lines += [f"#print axioms {theorem}" for theorem in manifest["statements"].values()]
    return "\n".join(lines) + "\n"


def problems_with_lean() -> list[str]:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    build = subprocess.run(["lake", "build"], cwd=LEAN, capture_output=True, text=True, timeout=1800)
    if build.returncode != 0:
        return ["lake build failed:\n" + (build.stdout + build.stderr)[-3000:]]
    if "declaration uses 'sorry'" in build.stdout + build.stderr:
        return ["a declaration uses sorry"]
    with tempfile.TemporaryDirectory() as tmp:
        audit = Path(tmp) / "Audit.lean"
        audit.write_text(audit_source(manifest), "utf-8")
        run = subprocess.run(["lake", "env", "lean", str(audit)], cwd=LEAN, capture_output=True, text=True, timeout=600)
    output = run.stdout + run.stderr
    if run.returncode != 0:
        return ["a theorem does not prove its statement exactly:\n" + output[-3000:]]
    problems = []
    for theorem in manifest["statements"].values():
        match = re.search(rf"'{re.escape(theorem)}' (does not depend on any axioms|depends on axioms: \[([^\]]*)\])", output)
        if not match:
            problems.append(f"{theorem}: no axiom report")
            continue
        used = {a.strip() for a in (match.group(2) or "").split(",") if a.strip()}
        if used - PERMITTED_AXIOMS:
            problems.append(f"{theorem}: uses {', '.join(sorted(used - PERMITTED_AXIOMS))}")
    return problems


def update(reason: str) -> int:
    if len(reason.strip()) < 10:
        print("--reason must say why the statements changed (10 characters or more)", file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {
        "schema_version": 1, "permitted_axioms": sorted(PERMITTED_AXIOMS), "files": {}, "statements": {}}
    found = pinned_files()
    changed = [rel for rel, path in found.items() if manifest["files"].get(rel, {}).get("sha256") != sha256(path)]
    removed = [rel for rel in manifest["files"] if rel not in found]
    if not changed and not removed:
        print("statements unchanged; nothing to record")
        return 0
    at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest["files"] = {rel: (manifest["files"][rel] if rel not in changed else
                               {"sha256": sha256(path), "reason": reason.strip(), "pinned": at})
                         for rel, path in found.items()}
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", "utf-8")
    entry = [f"## {at}", "", reason.strip(), ""] + [f"- changed: `{r}`" for r in changed] + \
            [f"- removed: `{r}`" for r in removed] + [""]
    head = "# Changes to the trusted Lean statements\n\nNewest first. Written by `tests/lean_statements.py update`.\n\n"
    old = CHANGES.read_text("utf-8").removeprefix(head) if CHANGES.exists() else ""
    CHANGES.write_text(head + "\n".join(entry) + "\n" + old, "utf-8")
    print(f"pinned {len(changed)} file(s), removed {len(removed)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--lean", action="store_true", help="also build and audit with lake")
    sub.add_parser("update").add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.command == "update":
        return update(args.reason)
    problems = problems_without_lean()
    if args.lean and not problems:
        problems = problems_with_lean()
    for problem in problems:
        print(problem, file=sys.stderr)
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    print(json.dumps({"status": "OK" if not problems else "FAILED", "pinned_files": len(manifest["files"]),
                      "statements": len(manifest["statements"]), "audited_with_lean": bool(args.lean and not problems)}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
