#!/usr/bin/env python3
"""Decisions on suggestions: confirm, reject, undo and re-confirm, one or many at a time,
previewed before they apply, and undone a batch at a time.

Both decision files (links and dependencies, and rules) hold confirmed rows and rejected
rows. Every decision is one compare-and-set on one item, named by its decision ID:

    {"id": ID, "expect": VERSION or None, "set": {"list": "confirmed" | "rejected", "row": ROW} or None}

`expect` is the version of the item the person decided on (None: not decided); a version
is the first 24 hex digits of the sha256 of the item's decided entry, which list and the
row. `commit` applies a batch only if every item is still exactly as expected, otherwise
nothing changes and the refusal names what changed. A batch touches only its own items,
so decisions on other items made in between never make it stale, and two batches on
different items give the same file in either order.

Applying records, for each item it changed, the entry before and after, under one batch
ID in the history file beside the decisions file. Undoing a batch is the same batch with
each `expect` and `set` swapped: it puts back exactly what that batch changed, keeps every
later decision on other items, and is refused when a later decision changed one of its
items. An undo is itself a batch, so it can be undone.

A preview (`preview=True`) computes the batch without writing and returns one digest per
operation; applying with `plan=` those digests refuses (StalePlan) unless the batch
computed now is the same operation for operation: the same items, the same versions
expected, the same rows written.

The laws are stated in proofs/lean/OntologyEXSpec/Decisions.lean and proved about a model
of `commit`. engine/src/decide.rs implements the same `commit` (tests/rust_decide_parity.py
compares the two); the control room uses it with --view-engine rust.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Callable

from policy_check import MAX_TOTAL, canonical, digest, require, safe_file

MAX_BATCH = 100
LISTS = ("confirmed", "rejected")
MAX_HISTORY_BYTES = 8 * 1024 * 1024
MAX_HISTORY_LINES = 50_000


def decision_id(*key: str) -> str:
    """The ID of a suggestion and of the decided row it becomes."""
    return digest(canonical(list(key)))[:24]


def version(entry: dict | None) -> str | None:
    """The version of an item's decided entry ({"list", "row"}); None when it is not decided."""
    return None if entry is None else digest(canonical({"list": entry["list"], "row": entry["row"]}))[:24]


def op_digest(op: dict) -> str:
    """What a preview binds: the item, the version expected and what will be written."""
    return digest(canonical({"id": op["id"], "expect": op["expect"], "set": op["set"]}))[:24]


class Refused(ValueError):
    """A batch that was not applied, and the items that stopped it. Nothing was written."""

    def __init__(self, reason: str, ids: list[str]):
        super().__init__(f"{reason}: {', '.join(ids)}")
        self.reason, self.ids = reason, ids


class StalePlan(Refused):
    """The batch computed now is not the one the person previewed."""


def _entry(value) -> bool:
    return type(value) is dict and set(value) == {"list", "row"} and value["list"] in LISTS and type(value["row"]) is dict


def commit(entries: list[dict], ops: list[dict]) -> dict:
    """Apply a batch of compare-and-set operations to decided entries, whole or not at all.

    `entries` are {"id", "list", "row"} in file order. Returns the entries after the batch
    (an entry replaced in its own list keeps its place; a new or moved one goes last) and
    the changes ({"id", "before", "after"}, operations that change nothing left out).
    Raises Refused("DUPLICATE_ITEM" | "STALE", ids) and changes nothing otherwise.
    """
    require(type(entries) is list and all(type(e) is dict and set(e) == {"id", "list", "row"} and type(e["id"]) is str
                                           and _entry({"list": e["list"], "row": e["row"]}) for e in entries),
            "invalid decided entries")
    require(len({e["id"] for e in entries}) == len(entries), "an item is decided twice")
    require(type(ops) is list and 0 < len(ops) <= MAX_BATCH and all(
        type(op) is dict and set(op) == {"id", "expect", "set"} and type(op["id"]) is str
        and (op["expect"] is None or type(op["expect"]) is str) and (op["set"] is None or _entry(op["set"]))
        for op in ops), f"a batch is 1 to {MAX_BATCH} operations")
    ids = [op["id"] for op in ops]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise Refused("DUPLICATE_ITEM", twice)
    current = {e["id"]: {"list": e["list"], "row": e["row"]} for e in entries}
    stale = [op["id"] for op in ops if version(current.get(op["id"])) != op["expect"]]
    if stale:
        raise Refused("STALE", stale)
    out = [dict(e) for e in entries]
    changes = []
    for op in ops:
        before, after = current.get(op["id"]), op["set"]
        if version(before) == version(after):
            continue
        changes.append({"id": op["id"], "before": before, "after": after})
        at = next((i for i, e in enumerate(out) if e["id"] == op["id"]), None)
        if after is not None and at is not None and out[at]["list"] == after["list"]:
            out[at] = {"id": op["id"], "list": after["list"], "row": after["row"]}
            continue
        if at is not None:
            del out[at]
        if after is not None:
            out.append({"id": op["id"], "list": after["list"], "row": after["row"]})
    return {"entries": out, "changes": changes}


def replace_checked(target: Path, data: bytes, before: bytes | None, name: str) -> None:
    """Write a decisions file atomically, refusing if it changed since it was read."""
    require(len(data) <= MAX_TOTAL, f"{name} byte budget exceeded")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{name}-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        now = safe_file(target.parent, target.name, MAX_TOTAL) if target.exists() else None
        require(now == before, f"{name} file changed during write")
        temporary.replace(target)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def history_path(path: Path) -> Path:
    return path.with_suffix(".history.jsonl")


def entries_of(confirmed: list[dict], rejected: list[dict], key: Callable[[dict], tuple]) -> list[dict]:
    return ([{"id": decision_id(*key(r)), "list": "confirmed", "row": r} for r in confirmed] +
            [{"id": decision_id(*key(r)), "list": "rejected", "row": r} for r in rejected])


def plan(entries: list[dict], *, key: Callable[[dict], tuple], by: str, note: str, confirm: list[dict] = (),
         reject: list[dict] = (), undo: list[str] = (), reconfirm: list[dict] = ()) -> tuple[list[dict], list[dict]]:
    """The operations a request asks for, against the entries as they are now, and the rows
    left as they are because they are already decided (or named twice).

    An undo ID that names no decided row, or a re-confirm of a row that is not confirmed,
    refuses the whole request."""
    current = {e["id"]: e for e in entries}
    ops, unchanged = [], []
    missing = sorted(set(undo) - set(current))
    require(not missing, f"not decided, so nothing to undo: {', '.join(missing)}")
    for i in dict.fromkeys(undo):
        ops.append({"id": i, "expect": version(current[i]), "set": None})
    planned = {op["id"] for op in ops}
    for row in reconfirm:
        i = decision_id(*key(row))
        require(i in current and current[i]["list"] == "confirmed" and i not in planned,
                "only a confirmed row can be re-confirmed")
        ops.append({"id": i, "expect": version(current[i]),
                    "set": {"list": "confirmed", "row": {**row, "declared_by": by.strip(), "note": note}}})
        planned.add(i)
    for action, rows in (("confirm", confirm), ("reject", reject)):
        for row in rows:
            row = {**row, "declared_by": by.strip(), "note": note}
            i = decision_id(*key(row))
            if i in current or i in planned:
                unchanged.append(row)
                continue
            ops.append({"id": i, "expect": None,
                        "set": {"list": "confirmed" if action == "confirm" else "rejected", "row": row}})
            planned.add(i)
    return ops, unchanged


def batches(path: Path) -> list[dict]:
    """The decision batches in a history file, newest first: who, when, what, and whether undone."""
    found: dict[str, dict] = {}
    for event in read_jsonl_tail(history_path(path), MAX_HISTORY_BYTES, MAX_HISTORY_LINES):
        if type(event) is not dict or type(event.get("batch")) is not str or "before" not in event:
            continue  # decisions recorded before batches were, which can still be undone one by one
        b = found.setdefault(event["batch"], {"batch": event["batch"], "at": event.get("at"), "by": event.get("by"),
                                               "note": event.get("note", ""), "actions": {}, "changes": 0,
                                               "undoes": event.get("undoes"), "undone_by": None})
        b["actions"][event.get("action")] = b["actions"].get(event.get("action"), 0) + 1
        b["changes"] += 1
        if event.get("undoes") in found:
            found[event["undoes"]]["undone_by"] = event["batch"]
    return list(reversed(found.values()))  # the file is append-only: newest last


def undo_batch_ops(path: Path, batch: str, entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """The inverse of a recorded batch, and the items a later decision changed since (which
    stop it: they are reported with who changed them, and nothing is overwritten)."""
    events = [e for e in read_jsonl_tail(history_path(path), MAX_HISTORY_BYTES, MAX_HISTORY_LINES)
              if type(e) is dict and e.get("batch") == batch and "before" in e and "after" in e]
    require(events, f"no recorded batch {batch} to undo (older decisions are undone one by one)")
    later = [e for e in read_jsonl_tail(history_path(path), MAX_HISTORY_BYTES, MAX_HISTORY_LINES)
             if type(e) is dict and e.get("undoes") == batch]
    require(not later, f"batch {batch} was already undone by batch {later[0]['batch'] if later else ''}")
    ops = [{"id": e["id"], "expect": version(e["after"]), "set": e["before"]} for e in events]
    current = {e["id"]: {"list": e["list"], "row": e["row"]} for e in entries}
    conflicts = []
    for op in ops:
        if version(current.get(op["id"])) != op["expect"]:
            changed = [e for e in read_jsonl_tail(history_path(path), MAX_HISTORY_BYTES, MAX_HISTORY_LINES)
                       if type(e) is dict and e.get("id") == op["id"]]
            last = changed[-1] if changed else {}
            conflicts.append({"id": op["id"], "by": last.get("by"), "at": last.get("at"), "action": last.get("action")})
    return ops, conflicts


def _action(before: dict | None, after: dict | None, undoing: bool) -> str:
    if after is None:
        return "undo"
    if undoing:
        return "restore"
    if before is None:
        return "confirm" if after["list"] == "confirmed" else "reject"
    return "reconfirm"


def describe(ops: list[dict], entries: list[dict], undoing: bool = False) -> list[dict]:
    """What a batch will do, item by item, for a preview."""
    current = {e["id"]: {"list": e["list"], "row": e["row"]} for e in entries}
    return [{"id": op["id"], "action": _action(current.get(op["id"]), op["set"], undoing),
             "before": current.get(op["id"]), "after": op["set"], "digest": op_digest(op)} for op in ops]


def apply(path: Path, *, read: Callable[[Path], tuple[list[dict], list[dict]]], kind: str,
          confirmed_field: str, key: Callable[[dict], tuple], name: str, max_rows: int, by: str,
          note: str = "", confirm: list[dict] = (), reject: list[dict] = (), undo: list[str] = (),
          reconfirm: list[dict] = (), undo_batch: str | None = None, preview: bool = False,
          plan_digests: dict | None = None, expect: dict | None = None,
          engine: Callable[[list[dict], list[dict]], dict] | None = None) -> dict:
    """Confirm or reject new rows, undo decided ones (by decision ID), re-confirm confirmed
    ones (replacing the row, e.g. with a fresh evidence watermark), or undo a recorded batch,
    in one write.

    `expect` maps decision IDs to the versions the person saw (undo and re-confirm of rows
    a page showed); a different version now refuses the batch. `preview` returns the batch
    and writes nothing; `plan_digests` applies it only if it is still that batch. Rows already
    decided are left as they are and reported as unchanged. `engine` replaces `commit` (the
    Rust kernel).
    """
    require(type(by) is str and 0 < len(by.strip()) <= 120, "a decision needs the deciding person")
    require(type(note) is str and len(note) <= 500, "invalid note")
    asked = len(confirm) + len(reject) + len(undo) + len(reconfirm)
    require((undo_batch is None and 0 < asked <= MAX_BATCH) or (type(undo_batch) is str and asked == 0),
            f"a batch decides between 1 and {MAX_BATCH} suggestions, or undoes one recorded batch")
    target = path.absolute()
    if target.exists():
        before = safe_file(target.parent, target.name, MAX_TOTAL)
        confirmed, rejected = read(target)
    else:
        before, confirmed, rejected = None, [], []
    entries = entries_of(confirmed, rejected, key)
    conflicts, unchanged = [], []
    if undo_batch is not None:
        ops, conflicts = undo_batch_ops(target, undo_batch, entries)
    else:
        ops, unchanged = plan(entries, key=key, by=by, note=note, confirm=confirm, reject=reject,
                              undo=undo, reconfirm=reconfirm)
    if expect:
        current = {e["id"]: e for e in entries}
        moved = [i for i, v in expect.items() if version(current.get(i)) != v]
        if moved:
            raise Refused("STALE", moved)
    digests = {op["id"]: op_digest(op) for op in ops}
    if preview:
        return {"status": "PREVIEW", "plan": digests, "changes": describe(ops, entries, undo_batch is not None),
                "unchanged": unchanged, "conflicts": conflicts, "undoes": undo_batch, "authenticated": False}
    if plan_digests is not None and plan_digests != digests:
        raise StalePlan("STALE", sorted(i for i in set(plan_digests) | set(digests)
                                        if plan_digests.get(i) != digests.get(i)))
    if not ops:
        return {"status": "UNCHANGED", "applied": [], "unchanged": unchanged, "authenticated": False}
    result = (engine or commit)(entries, ops)
    if not result["changes"]:
        return {"status": "UNCHANGED", "applied": [], "unchanged": unchanged, "authenticated": False}
    confirmed = [e["row"] for e in result["entries"] if e["list"] == "confirmed"]
    rejected = [e["row"] for e in result["entries"] if e["list"] == "rejected"]
    require(len(confirmed) <= max_rows and len(rejected) <= max_rows,
            f"{name}: at most {max_rows} confirmed and {max_rows} rejected; undo some first")
    document = {"schema_version": 1, "kind": kind, confirmed_field: confirmed}
    if rejected:
        document["rejected"] = rejected
    replace_checked(target, canonical(document), before, name)
    read(target)  # the written file must load back
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    batch = digest(canonical({"at": at, "by": by.strip(), "plan": digests}))[:24]
    events = []
    for change in result["changes"]:
        action = _action(change["before"], change["after"], undo_batch is not None)
        event = {"at": at, "batch": batch, "action": action, "by": by.strip(), "note": note, "id": change["id"],
                 "row": (change["after"] or change["before"])["row"], "before": change["before"], "after": change["after"]}
        if undo_batch is not None:
            event["undoes"] = undo_batch
        events.append(event)
    try:  # the decisions are written; a log that cannot be appended to must not hide that
        with history_path(target).open("a", encoding="utf-8") as log:
            log.write("".join(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in events))
        logged = history_path(target).name
    except OSError as exc:
        logged = f"NOT_WRITTEN: {exc.strerror or exc}"
    actions = {e["action"] for e in events}
    status = ("UNDONE_BATCH" if undo_batch is not None else
              {"confirm": "CONFIRMED", "reject": "REJECTED", "undo": "UNDONE", "reconfirm": "RECONFIRMED"}[actions.pop()]
              if len(actions) == 1 else "APPLIED")
    return {"status": status, "batch": batch, "undoes": undo_batch,
            "applied": [{"id": e["id"], "action": e["action"], "row": e["row"]} for e in events],
            "unchanged": unchanged, "history": logged, "authenticated": False}


def read_jsonl_tail(path: Path | None, max_bytes: int, max_lines: int) -> list:
    """The newest JSON lines of an append-only file; torn or malformed lines are skipped.

    A long file is read from its newest end, never refused whole, so one writer cannot
    make the file unreadable by appending to it.
    """
    if path is None or not path.is_file() or path.is_symlink():
        return []
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - max_bytes))
        text = handle.read(max_bytes).decode("utf-8", "replace")
    rows = []
    for line in text.splitlines()[1 if size > max_bytes else 0:][-max_lines:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    """Append one line and make it durable. Nothing here rewrites or removes a line."""
    require(not path.is_symlink(), f"{path.name} must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def select(suggestions: list[dict], ids: list[str], all_strong: bool) -> list[dict]:
    """The current suggestions a command line names, by ID or as every STRONG one."""
    require(ids or all_strong, "pass --id (repeatable) or --all-strong")
    by_id = {s["id"]: s for s in suggestions}
    unknown = [i for i in ids if i not in by_id]
    require(not unknown, f"no current suggestion has ID {', '.join(unknown)}; run suggest again")
    chosen = [by_id[i] for i in dict.fromkeys(ids)]
    chosen += [s for s in suggestions if all_strong and s["strength"] == "STRONG" and s["id"] not in ids]
    require(chosen, "no STRONG suggestion to decide")
    return chosen


def decided(confirmed: list[dict], rejected: list[dict], key: Callable[[dict], tuple], limit: int = 50) -> list[dict]:
    """Decided rows, newest of each kind first, with the ID that undoes each."""
    return ([{**r, "id": decision_id(*key(r)), "decision": "confirm"} for r in reversed(confirmed)][:limit] +
            [{**r, "id": decision_id(*key(r)), "decision": "reject"} for r in reversed(rejected)][:limit])
