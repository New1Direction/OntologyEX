#!/usr/bin/env python3
"""Bounded, offline diagnostics for an OntologyEX domain skill. Never executes a tool.

This file is copied into generated skills as scripts/check.py. Standard library only.
CHECKS_PASS means only that the modeled checks passed for supplied values; it is not
an authorization, a verified live state, or evidence that the model is complete.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import operator
import re
import sys
from pathlib import Path, PurePosixPath

MAX_FILE = 262144
MAX_TOTAL = 2097152
MAX_FILES = 128
MAX_ITEMS = 256
MAX_INTEGER = 2**63 - 1
OPS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
       "lte": operator.le, "gt": operator.gt, "gte": operator.ge}
TYPES = {"integer": int, "string": str, "boolean": bool}
ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


class DomainError(ValueError):
    """Expected input/verification failure; no partial success."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise DomainError(message)


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(data: bytes) -> object:
    require(len(data) <= MAX_TOTAL, "JSON exceeds 2 MiB budget")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=object_pairs,
                           parse_constant=lambda x: (_ for _ in ()).throw(DomainError(f"invalid number: {x}")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DomainError(f"invalid JSON: {exc}") from exc
    return value


def relative(path: str) -> str:
    require(isinstance(path, str) and bool(path), "path must be a nonempty relative string")
    p = PurePosixPath(path)
    require(not p.is_absolute() and all(part not in ("", ".", "..") for part in path.split("/"))
            and "\\" not in path and ":" not in path and "\x00" not in path,
            f"unsafe relative path: {path!r}")
    return path


def safe_file(root: Path, path: str, limit: int = MAX_FILE) -> bytes:
    """Local regular files only. This is not an OS sandbox against concurrent writers."""
    relative(path)
    p = root
    require(root.is_dir() and not root.is_symlink(), f"invalid root: {root}")
    for part in path.split("/"):
        p = p / part
        require(not p.is_symlink(), f"symlink rejected: {path}")
    require(p.is_file(), f"missing regular file: {path}")
    require(p.stat().st_size <= limit, f"file too large: {path}")
    with p.open("rb") as handle:
        data = handle.read(limit + 1)
    require(len(data) <= limit, f"file too large: {path}")
    return data


def keys(value: object, expected: set, location: str) -> None:
    require(type(value) is dict, f"{location}: expected object")
    require(set(value) == expected, f"{location}: expected keys {sorted(expected)}")


def entries(value: object, location: str) -> list:
    require(type(value) is list and len(value) <= MAX_ITEMS, f"{location}: expected bounded list")
    return value


def text(value: object, location: str) -> None:
    require(isinstance(value, str) and 0 < len(value.strip()) <= 4096, f"{location}: expected nonempty text")


def identifier(value: object, location: str) -> None:
    require(isinstance(value, str) and ID.fullmatch(value) is not None, f"{location}: invalid identifier")


def scalar_type(value: object) -> str:
    for label, typ in TYPES.items():
        if type(value) is typ:
            if label == "integer":
                require(abs(value) <= MAX_INTEGER, "integer outside signed 63-bit magnitude budget")
            if label == "string":
                require(len(value) <= 4096, "string exceeds budget")
            return label
    raise DomainError("values must be integers, strings, or booleans; no floats/null")


def validate_check(check: object, fields: dict) -> None:
    keys(check, {"left", "op", "right"}, "check")
    require(type(check["left"]) is str and check["left"] in fields, "check.left: unknown field")
    require(type(check["op"]) is str and check["op"] in OPS, "check.op: unsupported operator")
    right = check["right"]
    require(type(right) is dict and len(right) == 1, "check.right: expected one operand")
    kind = next(iter(right))
    if kind == "value":
        right_type = scalar_type(right[kind])
    elif kind == "field":
        require(type(right[kind]) is str and right[kind] in fields, "check.right: unknown field")
        right_type = fields[right[kind]]
    elif kind == "subtract":
        refs = right[kind]
        require(type(refs) is list and len(refs) == 2 and
                all(type(f) is str and fields.get(f) == "integer" for f in refs),
                "subtract: expected two declared integer fields")
        right_type = "integer"
    else:
        raise DomainError("check.right: unsupported operand; no expressions or code")
    require(fields[check["left"]] == right_type, "check: operand types differ")
    require(check["op"] in ("eq", "ne") or right_type == "integer", "ordering requires integer operands")


def validate_policy(model: dict) -> None:
    require(type(model) is dict, "model must be an object")
    inputs = model.get("inputs")
    require(type(inputs) is dict and 0 < len(inputs) <= MAX_ITEMS, "inputs: expected task schemas")
    for task, fields in inputs.items():
        identifier(task, "task")
        require(type(fields) is dict and len(fields) <= 64, "task fields: expected bounded object")
        for field, kind in fields.items():
            identifier(field, "field")
            require(type(kind) is str and kind in TYPES, "unsupported field type")
    ids = set()
    covered = set()
    for rule in entries(model.get("rules"), "rules"):
        keys(rule, {"id", "task", "statement", "status", "evidence", "check"}, "rule")
        identifier(rule["id"], "rule.id")
        require(rule["id"] not in ids, "duplicate rule/blocker id")
        ids.add(rule["id"])
        require(type(rule["task"]) is str and rule["task"] in inputs, "rule references unknown task")
        covered.add(rule["task"])
        text(rule["statement"], "rule.statement")
        require(rule["status"] in ("observed", "inferred"), "rule.status must be observed or inferred")
        require(bool(entries(rule["evidence"], "rule.evidence")), "rule requires evidence")
        for ref in rule["evidence"]:
            identifier(ref, "rule.evidence")
        if rule["check"] is not None:
            validate_check(rule["check"], inputs[rule["task"]])
    for kind in ("unknowns", "conflicts"):
        for item in entries(model.get(kind), kind):
            keys(item, {"id", "task", "statement", "evidence"}, kind)
            identifier(item["id"], kind + ".id")
            require(item["id"] not in ids, "duplicate rule/blocker id")
            ids.add(item["id"])
            require(type(item["task"]) is str and item["task"] in inputs, f"{kind}: unknown task")
            covered.add(item["task"])
            text(item["statement"], kind + ".statement")
            refs = entries(item["evidence"], kind + ".evidence")
            for ref in refs:
                identifier(ref, kind + ".evidence")
            require(kind != "conflicts" or len(set(refs)) >= 2, "conflicts require two distinct evidence references")
    require(covered == set(inputs), "each task needs at least one rule or explicit blocker")


def check_value(check: dict, values: dict) -> bool:
    right = check["right"]
    if "value" in right:
        operand = right["value"]
    elif "field" in right:
        operand = values[right["field"]]
    else:
        a, b = right["subtract"]
        operand = values[a] - values[b]
        scalar_type(operand)
    return OPS[check["op"]](values[check["left"]], operand)


def evaluate(model: dict, task: str, values: dict) -> dict:
    validate_policy(model)
    require(type(task) is str and task in model["inputs"], "unknown task")
    fields = model["inputs"][task]
    require(type(values) is dict and set(values) == set(fields), "input fields must exactly match the task schema")
    for field, kind in fields.items():
        require(scalar_type(values[field]) == kind, f"{field}: expected {kind}")
    passed, failed, blockers = [], [], []
    for kind in ("unknowns", "conflicts"):
        blockers.extend(item["id"] for item in model[kind] if item["task"] == task)
    for rule in model["rules"]:
        if rule["task"] != task:
            continue
        if rule["status"] != "observed" or rule["check"] is None:
            blockers.append(rule["id"])
        elif check_value(rule["check"], values):
            passed.append(rule["id"])
        else:
            failed.append(rule["id"])
    status = "CHECKS_FAIL" if failed else "NEEDS_REVIEW" if blockers else "CHECKS_PASS"
    return {"status": status, "task": task, "passed": passed, "failed": failed,
            "blockers": blockers, "execution_authorized": False,
            "scope": "Supplied values and modeled conditions only; no live-state or authorization guarantee."}


def file_inventory(root: Path) -> dict:
    result, queue, total, directories = {}, [root], 0, 0
    while queue:
        folder = queue.pop()
        directories += 1
        require(directories <= MAX_FILES, "directory budget exceeded")
        # Bound even the directory entries inspected, not just files retained.
        import os
        with os.scandir(folder) as iterator:
            for index, entry in enumerate(iterator):
                require(index < MAX_FILES, "directory entry budget exceeded")
                require(not entry.is_symlink(), "bundle contains a symlink")
                p = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    queue.append(p)
                else:
                    path = p.relative_to(root).as_posix()
                    if path == "manifest.json":
                        continue
                    data = safe_file(root, path, MAX_TOTAL)
                    total += len(data)
                    require(total <= 8 * MAX_TOTAL and len(result) < MAX_FILES, "bundle budget exceeded")
                    result[path] = digest(data)
    return dict(sorted(result.items()))


def verify_bundle(root: Path) -> dict:
    manifest = parse_json(safe_file(root, "manifest.json"))
    keys(manifest, {"schema_version", "bundle_id", "files"}, "manifest")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1, "unsupported manifest version")
    files = manifest["files"]
    require(type(files) is dict and 0 < len(files) <= MAX_FILES, "invalid manifest files")
    for path, sha in files.items():
        relative(path)
        require(type(sha) is str and SHA.fullmatch(sha) is not None, "invalid file digest")
    require(manifest["bundle_id"] == digest(canonical(files)), "manifest identity mismatch")
    require(files == file_inventory(root), "bundle changed: missing, added, or modified files")
    require({"SKILL.md", "model.json", "scripts/check.py", "report.html", "references/domain.md"} <= set(files),
            "incomplete skill bundle")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--input", type=Path, required=True, help="JSON object of supplied task inputs")
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parent.parent
        verify_bundle(root)
        model = parse_json(safe_file(root, "model.json", MAX_TOTAL))
        values = parse_json(safe_file(args.input.parent, args.input.name))
        result = evaluate(model, args.task, values)
        print(canonical(result).decode(), end="")
        return {"CHECKS_PASS": 0, "CHECKS_FAIL": 2, "NEEDS_REVIEW": 3}[result["status"]]
    except (DomainError, OSError, UnicodeError, TypeError, RecursionError) as exc:
        print(canonical({"status": "INVALID_INPUT", "execution_authorized": False, "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
