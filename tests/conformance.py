#!/usr/bin/env python3
"""Cross-language conformance for the business rollup and the evidence grade ladder.

`expected` prints the Python reference table. `check FILE` compares a table produced
by Rust (`ontologyex-view conformance`) or Lean (`lake exe conformance`) with it.
The table covers every system list of length 0..3 over the ten (status, impacted)
values and all 64 ladder inputs. This is exhaustive testing over a bounded domain,
not a refinement proof.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import business_view
import control_room
import entity_context
import evidence_grade

STATUSES = ("ok", "warning", "error", "stale", "unknown")
VALUES = [(status, impacted) for status in STATUSES for impacted in (False, True)]
# Source combining: one newest sample per source, (reported status, fresh).
SAMPLES = [(status, fresh) for status in ("ok", "warning", "error") for fresh in (False, True)]
SOURCES = ("otel", "health_probe", "prometheus")
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
COMPONENT = "discovery:service:compose.yml:db"


def fused(seq) -> str:
    """The status build_state shows for one component reported by len(seq) sources."""
    rows = []
    for source, i in zip(SOURCES, seq):
        status, fresh = SAMPLES[i]
        when = NOW if fresh else NOW - timedelta(hours=1)
        rows.append({"schema_version": 1, "component_id": COMPONENT, "status": status,
                     "source": source, "observed_at": when.isoformat().replace("+00:00", "Z"),
                     "expires_after_seconds": 120, "metrics": {}})
    topology = {"kind": "UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW", "project": {}, "coverage": {},
                "source_evidence": [], "unresolved": [], "gaps": [], "edges": [],
                "nodes": [{"id": COMPONENT, "kind": "declared_service", "label": "db"}]}
    return control_room.build_state(topology, rows, {}, NOW)["nodes"][0]["status"]


def impact_rows() -> list:
    """Impact standing (entity_context._impact) on 300 generated graphs: the generator of
    proofs/lean/Conformance.lean, so the rows line up with what Lean's proven model answers."""
    rows = []
    for i in range(300):
        state = [i + 1]

        def draw(m: int) -> int:
            state[0] = (state[0] * 1103515245 + 12345) % 2147483648
            return state[0] // 65536 % m

        n = 2 + draw(5)
        edges = []
        for _ in range(draw(9)):
            s, d, w = draw(n), draw(n), draw(3)
            edges.append([s, d, w])
        rels = [{"source": f"n{s}", "target": f"n{d}", "relation": "DEPENDS_ON",
                 "standing": entity_context.STANDINGS[w]} for s, d, w in edges]
        found = {r["id"]: r for r in entity_context._impact("n0", rels, {})}
        rows.append([edges, [[v, entity_context.WEAKNESS[found[f"n{v}"]["standing"]], found[f"n{v}"]["depth"]]
                             if f"n{v}" in found else [v] for v in range(1, n)]])
    return rows


def decision_rows() -> list:
    """decisions.commit on 300 generated batches: the generator of proofs/lean/Conformance.lean.
    Entry n stands for a decided row {"v": n} (confirmed when even, rejected when odd)."""
    import decisions

    def entry(v):
        return None if v is None else {"list": decisions.LISTS[v % 2], "row": {"v": v}}

    rows = []
    for i in range(300):
        state = [i + 1]

        def draw(m: int) -> int:
            state[0] = (state[0] * 1103515245 + 12345) % 2147483648
            return state[0] // 65536 % m

        st = [None if r == 4 else r for r in (draw(5) for _ in range(5))]
        ops = []
        for _ in range(1 + draw(4)):
            k, e, w = draw(5), draw(20), draw(5)
            ops.append([k, st[k] if e >= 5 else None if e == 4 else e, None if w == 4 else w])
        entries = [{"id": f"k{k}", **entry(v)} for k, v in enumerate(st) if v is not None]
        try:
            after = decisions.commit(entries, [{"id": f"k{k}", "expect": decisions.version(entry(e)), "set": entry(w)}
                                               for k, e, w in ops])
            now = {e["id"]: e["row"]["v"] for e in after["entries"]}
            result = [now.get(f"k{k}") for k in range(5)]
        except decisions.Refused:
            result = "refused"
        rows.append([st, ops, result])
    return rows


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
    fusion = [[list(seq), fused(seq)] for length in range(4)
              for seq in itertools.product(range(len(SAMPLES)), repeat=length)]
    # Path standing (entity_context.path_standing): every chain of 0..4 standings.
    path = [[list(seq), entity_context.path_standing(entity_context.STANDINGS[i] for i in seq)]
            for length in range(5) for seq in itertools.product(range(4), repeat=length)]
    # Decision watermarks (entity_context.watermark_standing): recorded none/0/1 against now 0/1.
    marks = [[[r, n], entity_context.watermark_standing(None if r == 0 else {"f": str(r - 1)}, {"f": str(n)})]
             for r in range(3) for n in range(2)]
    return {"kind": "BUSINESS_CONFORMANCE_TABLE", "rollup": rollups, "grade": grades,
            "fusion": fusion, "path_standing": path, "watermark": marks, "impact": impact_rows(), "decisions": decision_rows(),
            "execution_authorized": False}


def check(path: Path) -> int:
    actual = json.loads(path.read_text(encoding="utf-8"))
    reference = expected()
    failures = 0
    # rollup and grade are required; fusion, path_standing and watermark are compared when present.
    for section in ("rollup", "grade", *[s for s in ("fusion", "path_standing", "watermark", "impact", "decisions") if s in actual]):
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
    fusion = f", {len(reference['fusion'])} fusion rows" if "fusion" in actual else ""
    path = f", {len(reference['path_standing'])} path rows" if "path_standing" in actual else ""
    path += f", {len(reference['watermark'])} watermark rows" if "watermark" in actual else ""
    path += f", {len(reference['impact'])} impact graphs" if "impact" in actual else ""
    path += f", {len(reference['decisions'])} decision batches" if "decisions" in actual else ""
    print(f"conformance: {len(reference['rollup'])} rollup rows, {len(reference['grade'])} grade rows"
          f"{fusion}{path}, {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["expected"]:
        print(json.dumps(expected()))
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        raise SystemExit(check(Path(sys.argv[2])))
    else:
        raise SystemExit("usage: conformance.py expected | check FILE")
