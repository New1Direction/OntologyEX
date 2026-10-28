#!/usr/bin/env python3
"""Propose candidate conflicts: pairs of cited spans that may contradict each other.

OntologyEX records conflicts but never decides them. This asks the configured
decision model, task by task, whether two distinct evidence spans contradict each
other about that task, and lists the likely pairs with a ready-to-edit conflict
entry. It never edits the contract: the author reads both spans, writes the
statement, and adds the entry only if the sources really disagree.
"""
from __future__ import annotations

import argparse
from itertools import combinations
import json
from pathlib import Path

import decide
from policy_check import DomainError, MAX_TOTAL, parse_json, require, safe_file

MAX_PAIRS_PER_TASK = 120
MIN_PROBABILITY = 0.5


def load_contract(target: Path) -> dict:
    if (target / "model.json").is_file():
        return parse_json(safe_file(target, "model.json", MAX_TOTAL))
    return parse_json(safe_file(target, "60-contract.json", MAX_TOTAL))


def scan(target: Path, client) -> dict:
    contract = load_contract(target)
    require(type(contract) is dict and type(contract.get("evidence")) is list, "no evidence to scan")
    records = {e["id"]: e for e in contract["evidence"] if type(e) is dict and "quote" in e}
    recorded = {frozenset(c["evidence"]) for c in contract.get("conflicts", []) if len(c.get("evidence", [])) >= 2}
    tasks: dict[str, dict] = {}
    for collection in ("rules", "unknowns", "conflicts"):
        for item in contract.get(collection, []):
            t = tasks.setdefault(item["task"], {"statements": [], "evidence": []})
            t["statements"].append(f"{collection[:-1]} {item['id']}: {item['statement']}")
            t["evidence"] += [ref for ref in item.get("evidence", []) if ref in records]
    asks, keys, truncated = [], [], 0
    for task, info in sorted(tasks.items()):
        ids = sorted(set(info["evidence"]))
        pairs = [(a, b) for a, b in combinations(ids, 2)
                 if frozenset((a, b)) not in recorded and records[a]["quote"] != records[b]["quote"]]
        truncated += max(0, len(pairs) - MAX_PAIRS_PER_TASK)
        pairs = pairs[:MAX_PAIRS_PER_TASK]
        if not pairs:
            continue
        state = "\n\n".join([f"Task: {task}", *info["statements"]] + [
            f"[{i}] {records[i]['kind']} evidence, {records[i]['path']} lines "
            f"{records[i]['start_line']}-{records[i]['end_line']}:\n{records[i]['quote'][:2000]}" for i in ids])
        questions = {f"p{n}": {"type": "noul", "instructions":
                               f"Do evidence {a} and {b} contradict each other about task {task}?"}
                     for n, (a, b) in enumerate(pairs)}
        asks.append((state, questions))
        keys.append((task, pairs))
    candidates = []
    for (task, pairs), answers in zip(keys, client.decide_many(asks)):
        for n, (a, b) in enumerate(pairs):
            p = answers[f"p{n}"]["noul"]
            if p >= MIN_PROBABILITY:
                candidates.append({
                    "task": task, "evidence": [a, b], "probability": round(p, 4),
                    "spans": [f"{records[x]['path']}:{records[x]['start_line']}-{records[x]['end_line']}"
                              for x in (a, b)],
                    "draft_conflict": {"id": f"conflict-{a}-{b}"[:80], "task": task,
                                       "statement": f"TODO: state how {a} and {b} disagree, or discard.",
                                       "evidence": [a, b]}})
    candidates.sort(key=lambda c: (-c["probability"], c["task"], c["evidence"]))
    return {"schema_version": 1, "kind": "CONFLICT_CANDIDATES", "model": client.label,
            "candidates": candidates, "pairs_asked": sum(len(p) for _, p in keys),
            "pairs_not_asked": truncated, "contract_changed": False,
            "semantics": "MODEL_CANDIDATES_THE_AUTHOR_READS_BOTH_SPANS_AND_DECIDES"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="authoring workspace or compiled skill")
    args = parser.parse_args()
    try:
        print(json.dumps(scan(args.target, decide.DecisionClient()), indent=2))
        return 0
    except (DomainError, decide.DecisionError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
