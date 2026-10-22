#!/usr/bin/env python3
"""Deterministic path/origin stress fixtures. No model or semantic detector is run."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import domain_skill as ds
import provenance
from policy_check import DomainError, canonical, digest, evaluate, require


def fixture(number: int, copies: int, *, distinct: bool = False, conflict: bool = True) -> tuple[dict, dict]:
    """Authored ground truth; identical wording is held fixed across the origin control."""
    cap = 1000 + number * 37
    rule_text = f"A request for fixture {number} must not exceed {cap} units."
    opposing_text = f"A request for fixture {number} must not exceed {cap // 2} units."
    sources, evidence, attrs = {}, [], []
    for index in range(copies + int(conflict)):
        opposed = index == copies
        path = "policy/opposing.md" if opposed else f"policy/copy-{index}.md"
        quote = opposing_text if opposed else rule_text
        data = (quote + "\n").encode()
        eid = "E-opposing" if opposed else f"E-support-{index}"
        item = {"id": eid, "path": path, "sha256": digest(data), "start_line": 1,
                "end_line": 1, "quote": quote, "kind": "requirement"}
        sources[path] = {"sha256": digest(data), "bytes": len(data)}
        evidence.append(item)
        origin = "origin-opposing" if opposed else f"origin-support-{index if distinct else 0}"
        attrs.append({"evidence_id": eid, "evidence_sha256": digest(canonical(item)),
                      "origin_id": origin, "independence_group": "group-" + origin,
                      "basis": "Authored synthetic fixture lineage, not an inferred real-world relationship."})
    support = [e["id"] for e in evidence if e["id"] != "E-opposing"]
    model = {"source_files": sources, "evidence": evidence, "inputs": {"Submit": {"units": "integer"}},
             "rules": [{"id": "cap", "task": "Submit", "statement": rule_text, "status": "observed",
                        "evidence": support, "check": {"left": "units", "op": "lte", "right": {"value": cap}}}],
             "unknowns": [], "conflicts": ([{"id": "inconsistent-cap", "task": "Submit",
                 "statement": "Selected requirements disagree about the permitted cap; do not resolve by counting copies.",
                 "evidence": [*support, "E-opposing"]}] if conflict else [])}
    return model, {"schema_version": 1, "attributions": attrs}


def run(out: Path) -> dict:
    results = []
    example = None
    independent_controls, legitimate_controls, unknown_controls = 0, 0, 0
    for number in range(20):
        expected_projection = None
        for copies in (1, 3, 5):
            model, attrs = fixture(number, copies)
            report = provenance.analyze(model, attrs)
            cap = 1000 + number * 37
            baseline = [evaluate(model, "Submit", {"units": units}) for units in (0, cap, cap + 1)]
            candidate = [evaluate({**model, "provenance": report}, "Submit", {"units": units}) for units in (0, cap, cap + 1)]
            require(candidate == baseline, "provenance changed a modeled diagnostic")
            conflict = next(item for item in report["items"] if item["kind"] == "conflicts")
            require(conflict["status"] == "CONFLICT_RECORDED" and "E-opposing" in conflict["evidence_ids"],
                    "recorded counterevidence was dropped")
            require({r["evidence_id"] for r in report["records"]} == {e["id"] for e in model["evidence"]},
                    "an original representation was lost")
            rule = next(item for item in report["items"] if item["kind"] == "rules")
            projection = (rule["declared_origin_ids"], rule["declared_independence_groups"], rule["atom_ids"],
                          [r["status"] for r in candidate], conflict["declared_origin_ids"])
            require(expected_projection is None or projection == expected_projection, "duplicate-count invariance failed")
            expected_projection = projection
            unknown = provenance.analyze(model)
            require(unknown["summary"]["independent_support_count"] is None and
                    len(unknown["summary"]["unattributed_evidence_ids"]) == copies + 1,
                    "missing lineage became independent support")
            unknown_controls += 1
            results.append({"fixture": number, "supporting_copies": copies,
                            "raw_evidence_records": len(model["evidence"]),
                            "supporting_declared_origins": len(rule["declared_origin_ids"]),
                            "supporting_declared_groups": len(rule["declared_independence_groups"]),
                            "supporting_atoms": rule["evidence_atoms"], "diagnostics": [r["status"] for r in candidate],
                            "recorded_conflict_retained": True, "all_evidence_retained": True, "baseline_parity": True})
            if number == 0 and copies == 5:
                example = report
        model, attrs = fixture(number, 5, distinct=True)
        distinct = provenance.analyze(model, attrs)
        rule = next(item for item in distinct["items"] if item["kind"] == "rules")
        require(len(rule["declared_origin_ids"]) == 5 and len(rule["declared_independence_groups"]) == 5 and
                rule["evidence_atoms"] == 5 and rule["content_groups"] == 1,
                "distinct declared origins were collapsed by equal wording")
        independent_controls += 1
        model, attrs = fixture(number, 5, conflict=False)
        result = evaluate({**model, "provenance": provenance.analyze(model, attrs)}, "Submit", {"units": 1})
        require(result == evaluate(model, "Submit", {"units": 1}) and result["status"] == "CHECKS_PASS" and
                result["execution_authorized"] is False, "legitimate outcome changed")
        legitimate_controls += 1
    summary = {"status": "STRESS_PASSED", "authored_fixtures": 20, "paired_variants": len(results),
               "duplicate_invariance_passed": len(results), "recorded_conflicts_retained": len(results),
               "diagnostic_comparisons_passed": 3 * len(results), "distinct_origin_controls": independent_controls,
               "legitimate_success_controls": legitimate_controls, "unknown_lineage_controls": unknown_controls,
               "model_calls": 0, "paid_model_spend_usd": 0, "fresh_agent_evaluation": "NOT_RUN",
               "semantic_conflict_detection": "NOT_TESTED", "independence_verified": False,
               "fixture_type": "AUTHORED_REGRESSION_NOT_HIDDEN_MODEL_EVALUATION"}
    ds.write_files(out, {"summary.json": canonical(summary), "variants.json": canonical(results),
                        "example-provenance.json": canonical(example)})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "build/provenance-stress")
    args = parser.parse_args()
    try:
        print(canonical(run(args.out)).decode(), end="")
        return 0
    except (DomainError, OSError, ValueError, TypeError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
