#!/usr/bin/env python3
"""Cross-language conformance for the feeds kernels: classification, retention, p95 rank.

`expected` prints the Python reference table. `check FILE` compares the `classify`,
`retain`, and `p95` sections of a table printed by Rust (`ontologyex-feeds
conformance`) or Lean (`lake exe conformance`) with it. Exhaustive over a bounded
domain: two metrics with values and thresholds 0..3 (or absent) and a down flag;
every key list up to length 5 over 0..3 with keep 2..4; every sample count 1..100000.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import feeds  # noqa: E402

NAMES = ("error_rate_pct", "p95_latency_ms")
COMPONENT = "discovery:service:compose.yml:db"


def assoc(i: int) -> dict:
    """Index 0..24: each of two metrics absent (0) or value 0..3 (1..4)."""
    return {NAMES[slot]: float(value - 1) for slot, value in ((0, i // 5), (1, i % 5)) if value}


def expected() -> dict:
    classify = [feeds.classify(assoc(m), assoc(w), assoc(e), down=d)
                for m in range(25) for w in range(25) for e in range(25) for d in (False, True)]
    retain = []
    for n in range(6):
        for keys in itertools.product(range(4), repeat=n):
            for keep in (2, 3, 4):
                rows = [{"component_id": COMPONENT, "observed_at": f"2026-10-28T08:00:0{v}Z", "source": "otel",
                         "status": "ok"} for v in keys]
                retain.append([int(r["observed_at"][18]) for r in feeds.retain(rows, keep)])
    p95 = [feeds.p95_index(n) for n in range(1, feeds.MAX_SPANS_PER_SERVICE + 1)]
    return {"kind": "FEEDS_CONFORMANCE_TABLE", "classify": classify, "retain": retain, "p95": p95,
            "execution_authorized": False}


def check(path: Path) -> int:
    actual = json.loads(path.read_text(encoding="utf-8"))
    reference = expected()
    failures = 0
    for section in ("classify", "retain", "p95"):
        want, got = reference[section], actual.get(section)
        if type(got) is not list or len(got) != len(want):
            print(f"{section}: expected {len(want)} rows, got {len(got) if type(got) is list else got!r}")
            failures += 1
            continue
        for i, (w, g) in enumerate(zip(want, got)):
            if w != g:
                failures += 1
                if failures <= 10:
                    print(f"{section}[{i}]: expected {w} got {g}")
    if actual.get("execution_authorized") is not False:
        print("table must not authorize execution")
        failures += 1
    print(f"feeds conformance: {len(reference['classify'])} classify, {len(reference['retain'])} retain, "
          f"{len(reference['p95'])} p95 rows, {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["expected"]:
        print(json.dumps(expected()))
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        raise SystemExit(check(Path(sys.argv[2])))
    else:
        raise SystemExit("usage: feeds_conformance.py expected | check FILE")
