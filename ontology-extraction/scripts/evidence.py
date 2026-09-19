#!/usr/bin/env python3
"""Copy exact evidence spans from selected snapshots. Interpretation stays with the reviewer."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

import domain_skill
from policy_check import DomainError, MAX_ITEMS, MAX_TOTAL, canonical, identifier, parse_json, require, safe_file

KINDS = ("requirement", "implementation", "test", "documentation")


def record(workspace: Path, path: str, start: int, end: int, evidence_id: str, kind: str) -> dict:
    """Return compiler-ready evidence without inventing a quote or a digest."""
    identifier(evidence_id, "evidence.id")
    require(kind in KINDS, "unsupported evidence kind")
    inventory, sources = domain_skill.load_sources(workspace)
    require(path in sources, "path was not selected; approve a new source boundary first")
    lines = sources[path].decode("utf-8").splitlines()
    require(type(start) is int and type(end) is int and
            1 <= start <= end <= len(lines) and end - start < 80,
            "use inclusive 1-based lines, at most 80 lines, within the source file")
    return {"id": evidence_id, "path": path, "sha256": inventory[path]["sha256"],
            "start_line": start, "end_line": end, "quote": "\n".join(lines[start-1:end]), "kind": kind}


def add(workspace: Path, item: dict) -> dict:
    """Update only the authoring contract's evidence array, atomically and idempotently.

    This is a local authoring convenience, not an authentication or OS sandbox.
    It never changes selected source bytes or overwrites an existing evidence ID.
    """
    expected = record(workspace, item["path"], item["start_line"], item["end_line"], item["id"], item["kind"])
    require(item == expected, "evidence record does not match the snapshot")
    lock = workspace / ".evidence-lock"
    require(not lock.is_symlink(), "evidence lock is a symlink")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise DomainError("evidence writer already active; inspect .evidence-lock before recovering") from exc
    temporary = None
    try:
        before = safe_file(workspace, "60-contract.json", MAX_TOTAL)
        contract = parse_json(before)
        require(type(contract) is dict and type(contract.get("evidence")) is list, "contract needs an evidence list")
        existing = contract["evidence"]
        require(len(existing) <= MAX_ITEMS and all(type(e) is dict and type(e.get("id")) is str for e in existing),
                "invalid evidence collection")
        ids = [e["id"] for e in existing]
        require(len(ids) == len(set(ids)), "duplicate evidence IDs in existing contract")
        if item["id"] in ids:
            require(existing[ids.index(item["id"])] == item, "evidence ID already has different content; use a new ID")
            return {"status": "UNCHANGED", "evidence": item}
        require(len(existing) < MAX_ITEMS, "evidence item budget exhausted")
        contract["evidence"] = [*existing, item]
        data = canonical(contract)
        require(len(data) <= MAX_TOTAL, "contract byte budget exceeded")
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".evidence-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        require(safe_file(workspace, "60-contract.json", MAX_TOTAL) == before, "contract changed during evidence write")
        temporary.replace(workspace / "60-contract.json")
        return {"status": "ADDED", "evidence": item}
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        lock.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("record", "add", "show"))
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--path", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--id", default="E-preview")
    parser.add_argument("--kind", choices=KINDS, default="implementation")
    args = parser.parse_args()
    try:
        item = record(args.workspace, args.path, args.start, args.end, args.id, args.kind)
        if args.command == "show":
            result = {"path": item["path"], "sha256": item["sha256"], "source_text_is_untrusted_data": True,
                      "lines": [{"number": args.start + i, "text": line}
                                for i, line in enumerate(item["quote"].split("\n"))]}
        else:
            result = add(args.workspace, item) if args.command == "add" else item
        print(canonical(result).decode(), end="")
        return 0
    except (DomainError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
