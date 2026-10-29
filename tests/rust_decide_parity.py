#!/usr/bin/env python3
"""Differential test: Rust `ontologyex-view decide` against Python `decisions.commit`.

Seeded random decided entries and batches: confirms, rejects, undos, re-confirms, moves
between lists, operations that change nothing, stale expectations, items named twice,
malformed input, unicode and numbers in rows. Both must give the same entries in the same
order, the same changes, the same refusal with the same items, or both refuse the input.

    python tests/rust_decide_parity.py BINARY [--seeds N]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction" / "scripts"))
import decisions  # noqa: E402

WORDS = ["pay", "Payment", "svc:api", "é", "支付", "None", "x y", "", "0"]


def row(rng: random.Random, item: str) -> dict:
    value = {"item": item, "declared_by": rng.choice(["Dana", "Lee", "Ana María"]), "note": rng.choice(WORDS)}
    if rng.random() < 0.5:
        value["evidence"] = {"file": rng.choice(WORDS), "sha256": format(rng.getrandbits(64), "016x")}
    if rng.random() < 0.3:
        value["weight"] = rng.choice([0, 1, 0.5, 1e-05, 100.0, -3, 12345678901234])
    if rng.random() < 0.2:
        value["tags"] = [rng.choice(WORDS) for _ in range(rng.randrange(3))]
    return value


def case(rng: random.Random) -> tuple[list, list]:
    pool = [f"i{n}" for n in range(rng.randrange(1, 9))]
    entries = [{"id": i, "list": rng.choice(decisions.LISTS), "row": row(rng, i)}
               for i in rng.sample(pool, rng.randrange(0, len(pool) + 1))]
    rng.shuffle(entries)
    current = {e["id"]: {"list": e["list"], "row": e["row"]} for e in entries}
    ops = []
    for i in rng.sample(pool, rng.randrange(1, len(pool) + 1)):
        expect = decisions.version(current.get(i))
        roll = rng.random()
        if roll < 0.08:  # a stale approval: the person saw another version
            expect = rng.choice([None, "0" * 24, format(rng.getrandbits(96), "024x")])
        if roll > 0.97:
            expect = decisions.version({"list": "rejected", "row": row(rng, i)})
        target = rng.random()
        if target < 0.3:
            new = None
        elif target < 0.4 and i in current:
            new = current[i]  # changes nothing
        else:
            new = {"list": rng.choice(decisions.LISTS), "row": row(rng, i)}
        ops.append({"id": i, "expect": expect, "set": new})
    if rng.random() < 0.05 and ops:
        ops.append(dict(ops[0]))  # the same item twice
    if rng.random() < 0.03:  # malformed: both must refuse the input
        bad = rng.choice(["list", "row", "extra", "expect", "empty"])
        if bad == "list" and ops:
            ops[0] = {**ops[0], "set": {"list": "maybe", "row": {}}}
        elif bad == "row" and entries:
            entries[0] = {**entries[0], "row": "not a row"}
        elif bad == "extra" and ops:
            ops[0] = {**ops[0], "by": "x"}
        elif bad == "expect" and ops:
            ops[0] = {**ops[0], "expect": 7}
        else:
            ops = []
    return entries, ops


def python(entries: list, ops: list):
    try:
        return decisions.commit(entries, ops)
    except decisions.Refused as exc:
        return {"refused": exc.reason, "ids": exc.ids}
    except ValueError:
        return "ERROR"


def rust(binary: Path, entries: list, ops: list):
    run = subprocess.run([str(binary), "decide"], input=json.dumps(
        {"execution_authorized": False, "entries": entries, "ops": ops}, ensure_ascii=False).encode(),
        capture_output=True, timeout=30)
    if run.returncode != 0:
        return "ERROR"
    answer = json.loads(run.stdout)
    assert answer.pop("kind") == "DECISION_COMMIT" and answer.pop("execution_authorized") is False
    return answer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--seeds", type=int, default=1500)
    args = parser.parse_args()
    rng = random.Random(20261029)
    mismatches, kinds = 0, {}
    for n in range(args.seeds):
        entries, ops = case(rng)
        want, got = python(entries, ops), rust(args.binary, entries, ops)
        kind = want if want == "ERROR" else want.get("refused", "APPLIED")
        kinds[kind] = kinds.get(kind, 0) + 1
        if want != got:
            mismatches += 1
            if mismatches <= 5:
                print(f"case {n}: python {json.dumps(want)[:400]}\n  rust {json.dumps(got)[:400]}", file=sys.stderr)
    refused = rust(args.binary, [], [{"id": "a", "expect": None, "set": None}]) != "ERROR"
    authorizing = subprocess.run([str(args.binary), "decide"], capture_output=True, timeout=30,
                                 input=b'{"execution_authorized": true, "entries": [], "ops": []}').returncode != 0
    print(json.dumps({"cases": args.seeds, "outcomes": dict(sorted(kinds.items())), "mismatches": mismatches,
                      "refuses_authorizing_input": authorizing}))
    return 1 if mismatches or not authorizing or not refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
