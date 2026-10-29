#!/usr/bin/env python3
"""Decisions on suggestions: confirm, reject, and undo, one or many at a time.

Both decision files (links and dependencies) hold confirmed rows and rejected rows.
A batch is checked whole and written with one atomic replace, so it lands entirely
or not at all, and a file changed by someone else since it was read is refused.
Undo removes decided rows, so their suggestions can come back. Every change is also
appended to a history file beside the decisions file (what, who, when); it is not
needed to load the decisions (entity_context.py reads it to say who decided what).
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


def decision_id(*key: str) -> str:
    """The ID of a suggestion and of the decided row it becomes."""
    return digest(canonical(list(key)))[:24]


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


def apply(path: Path, *, read: Callable[[Path], tuple[list[dict], list[dict]]], kind: str,
          confirmed_field: str, key: Callable[[dict], tuple], name: str, max_rows: int, by: str,
          note: str = "", confirm: list[dict] = (), reject: list[dict] = (), undo: list[str] = (),
          reconfirm: list[dict] = ()) -> dict:
    """Confirm or reject new rows, undo decided ones (by decision ID), and re-confirm
    confirmed ones (replacing the row, e.g. with a fresh evidence watermark), in one write.

    Rows already decided are left as they are and reported as unchanged. An undo ID
    that names no decided row, or a re-confirm of a row that is not confirmed, refuses
    the whole batch.
    """
    require(type(by) is str and 0 < len(by.strip()) <= 120, "a decision needs the deciding person")
    require(type(note) is str and len(note) <= 500, "invalid note")
    require(0 < len(confirm) + len(reject) + len(undo) + len(reconfirm) <= MAX_BATCH,
            f"a batch decides between 1 and {MAX_BATCH} suggestions")
    target = path.absolute()
    if target.exists():
        before = safe_file(target.parent, target.name, MAX_TOTAL)
        confirmed, rejected = read(target)
    else:
        before, confirmed, rejected = None, [], []
    by_id = {decision_id(*key(r)): r for r in confirmed + rejected}
    missing = sorted(set(undo) - set(by_id))
    require(not missing, f"not decided, so nothing to undo: {', '.join(missing)}")
    removed = [by_id[i] for i in dict.fromkeys(undo)]
    gone = {key(r) for r in removed}
    confirmed = [r for r in confirmed if key(r) not in gone]
    rejected = [r for r in rejected if key(r) not in gone]
    position = {key(r): i for i, r in enumerate(confirmed)}
    refreshed = []
    for row in reconfirm:
        require(key(row) in position, "only a confirmed row can be re-confirmed")
        confirmed[position[key(row)]] = row = {**row, "declared_by": by.strip(), "note": note}
        refreshed.append(row)
    decided = {key(r) for r in confirmed + rejected}
    added, unchanged = [], []
    for action, rows in (("confirm", confirm), ("reject", reject)):
        for row in rows:
            row = {**row, "declared_by": by.strip(), "note": note}
            if key(row) in decided or key(row) in gone:
                unchanged.append(row)
                continue
            decided.add(key(row))
            (confirmed if action == "confirm" else rejected).append(row)
            added.append((action, row))
    if not added and not removed and not refreshed:
        return {"status": "UNCHANGED", "applied": [], "unchanged": unchanged, "authenticated": False}
    require(len(confirmed) <= max_rows and len(rejected) <= max_rows,
            f"{name}: at most {max_rows} confirmed and {max_rows} rejected; undo some first")
    document = {"schema_version": 1, "kind": kind, confirmed_field: confirmed}
    if rejected:
        document["rejected"] = rejected
    replace_checked(target, canonical(document), before, name)
    read(target)  # the written file must load back
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    events = [{"at": at, "action": action, "by": by.strip(), "note": note, "row": row} for action, row in added]
    events += [{"at": at, "action": "undo", "by": by.strip(), "note": note, "row": row} for row in removed]
    events += [{"at": at, "action": "reconfirm", "by": by.strip(), "note": note, "row": row} for row in refreshed]
    try:  # the decisions are written; a log that cannot be appended to must not hide that
        with history_path(target).open("a", encoding="utf-8") as log:
            log.write("".join(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in events))
        logged = history_path(target).name
    except OSError as exc:
        logged = f"NOT_WRITTEN: {exc.strerror or exc}"
    actions = {e["action"] for e in events}
    status = ({"confirm": "CONFIRMED", "reject": "REJECTED", "undo": "UNDONE", "reconfirm": "RECONFIRMED"}[actions.pop()]
              if len(actions) == 1 else "APPLIED")
    return {"status": status,
            "applied": [{"id": decision_id(*key(e["row"])), "action": e["action"], "row": e["row"]} for e in events],
            "unchanged": unchanged, "history": logged, "authenticated": False}


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
