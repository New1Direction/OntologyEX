#!/usr/bin/env python3
"""Cross-language conformance for the business rollup and the evidence grade ladder.

`expected` prints the Python reference table. `check FILE` compares a table produced
by Rust (`ontologyex-view conformance`) or Lean (`lake exe conformance`) with it.
The table covers every system list of length 0..3 over the ten (status, impacted)
values and all 64 ladder inputs. This is exhaustive testing over a bounded domain,
not a refinement proof.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import business_view
import evidence_grade

STATUSES = ("ok", "warning", "error", "stale", "unknown")
VALUES = [(status, impacted) for status in STATUSES for impacted in (False, True)]


def expected() -> dict:
    rollups = []
    for length in range(4):
        for seq in itertools.product(range(len(VALUES)), repeat=length):
            systems = [{"label": f"s{i}", "status": VALUES[i][0],
                        "potentially_impacted_by": ["upstream"] if VALUES[i][1] else []}
                       for i in seq]
            rollups.append([list(seq), business_view.rollup(systems)[0]])
    grades = []
    for flags in itertools.product((0, 1), repeat=6):
        grade, reason = evidence_grade.ladder(*(bool(f) for f in flags))
        grades.append([list(flags), grade, reason])
    return {"kind": "BUSINESS_CONFORMANCE_TABLE", "rollup": rollups, "grade": grades,
            "execution_authorized": False}


def check(path: Path) -> int:
    actual = json.loads(path.read_text(encoding="utf-8"))
    reference = expected()
    failures = 0
    for section in ("rollup", "grade"):
        want, got = reference[section], actual.get(section)
        if type(got) is not list or len(got) != len(want):
            print(f"{section}: expected {len(want)} rows, got {len(got) if type(got) is list else got!r}")
            failures += 1
            continue
        for w, g in zip(want, got):
            if w != g:
                failures += 1
                if failures <= 10:
                    print(f"{section}: expected {w} got {g}")
    if actual.get("execution_authorized") is not False:
        print("table must not authorize execution")
        failures += 1
    print(f"conformance: {len(reference['rollup'])} rollup rows, {len(reference['grade'])} grade rows, "
          f"{failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["expected"]:
        print(json.dumps(expected()))
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        raise SystemExit(check(Path(sys.argv[2])))
    else:
        raise SystemExit("usage: conformance.py expected | check FILE")
