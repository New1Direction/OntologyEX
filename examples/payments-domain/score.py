#!/usr/bin/env python3
"""Score externally supplied predictions. Does not run an agent or verify its identity."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ontology-extraction/scripts"))
from policy_check import DomainError, canonical, entries, keys, parse_json, require, safe_file


def score(oracle, predictions):
    truth = {}
    for item in entries(oracle, "oracle"):
        require(type(item) is dict and type(item.get("id")) is str and item["id"] not in truth, "invalid/duplicate oracle case")
        require({"expected_status", "expected_refunded_minor_after"} <= set(item), "oracle missing outcome")
        truth[item["id"]] = item
    require(bool(truth), "empty oracle")
    answers = {}
    for item in entries(predictions, "predictions"):
        keys(item, {"id", "status", "refunded_minor_after"}, "prediction")
        require(type(item["id"]) is str and item["id"] not in answers, "invalid/duplicate prediction id")
        require(item["status"] in ("CHECKS_PASS", "CHECKS_FAIL", "NEEDS_REVIEW", "INVALID_INPUT"), "invalid status")
        require(item["refunded_minor_after"] is None or type(item["refunded_minor_after"]) is int, "final state must be an integer or null")
        answers[item["id"]] = item
    require(set(answers) == set(truth), "predictions must cover every case exactly once; no extra or missing cases")
    outcomes = []
    for cid, expected in truth.items():
        answer = answers[cid]
        outcomes.append({"id": cid, "passed": answer["status"] == expected["expected_status"] and
                         answer["refunded_minor_after"] == expected["expected_refunded_minor_after"]})
    return {"kind": "SUPPLIED_PREDICTIONS_NOT_VERIFIED_AGENT_RUN", "passed": sum(x["passed"] for x in outcomes),
            "total": len(outcomes), "outcomes": outcomes,
            "limitations": "Outcome proposals only. Supply execution traces and cost/model metadata separately; no identity or run isolation is authenticated."}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--oracle", type=Path, required=True)
    p.add_argument("--predictions", type=Path, required=True)
    args = p.parse_args()
    try:
        result = score(parse_json(safe_file(args.oracle.parent, args.oracle.name)),
                       parse_json(safe_file(args.predictions.parent, args.predictions.name)))
        print(canonical(result).decode(), end="")
        raise SystemExit(0 if result["passed"] == result["total"] else 2)
    except (DomainError, OSError, ValueError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc)}).decode(), end="")
        raise SystemExit(1)
