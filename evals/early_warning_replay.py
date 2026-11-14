#!/usr/bin/env python3
"""Compare two threshold rules on a deliberately constructed, synthetic timeline.

This is a wiring/measurement demonstration, not evidence of predictive accuracy.
The separate outcome labels are supplied by the fixture, not discovered by the
tool. Only samples available by each decision time are passed to build_state.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import control_room as room

DATASET = ROOT / "evals/datasets/synthetic-early-warning.json"
COMPONENT = "discovery:service:compose.yml:payments"
TOPOLOGY = {"kind": "UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW", "project": "synthetic-shop",
            "nodes": [{"id": COMPONENT, "kind": "declared_service", "label": "payments"}],
            "edges": [], "source_evidence": [], "unresolved": [], "gaps": [], "coverage": {}}
POLICIES = ("single_sample", "two_samples")


def stamp(t):
    return t.isoformat().replace("+00:00", "Z")


def covered(rows, metric, at, required):
    """Whether one source has the required latest, distinct, unambiguous fresh samples."""
    sources = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        instant = room.timestamp(row["observed_at"])
        if instant <= at:
            sources[row["source"]][instant][json.dumps(row, sort_keys=True)] = row
    for samples in sources.values():
        history = sorted(samples.items())
        if len(history) < required:
            continue
        chosen = history[-required:]
        if all(len(variants) == 1 and metric in next(iter(variants.values()))["metrics"]
               and (at - instant).total_seconds() <= next(iter(variants.values()))["expires_after_seconds"]
               for instant, variants in chosen):
            return True
    return False


def replay_case(dataset, case):
    start = room.timestamp(dataset["start"])
    metric, threshold = dataset["metric"], dataset["threshold"]
    failure_at = (start + timedelta(minutes=case["failure_minute"])
                  if case["failure_minute"] is not None else None)
    follow_up_end = start + timedelta(minutes=dataset["follow_up_end_minute"])
    rows = []
    for minute, value in case["readings"]:
        at = start + timedelta(minutes=minute)
        rows.append({"schema_version": 1, "component_id": COMPONENT, "observed_at": stamp(at),
                     "status": "error" if failure_at is not None and at >= failure_at else "ok",
                     "source": "synthetic", "expires_after_seconds": dataset["sample_ttl_seconds"],
                     "metrics": {metric: value}})
    decisions = []
    for minute in sorted(case.get("decision_minutes", dataset["decision_minutes"])):
        at = start + timedelta(minutes=minute)
        # No decision at or after the incident can be a pre-incident warning.
        if at > follow_up_end or (failure_at is not None and at >= failure_at):
            continue
        visible = [row for row in rows if room.timestamp(row["observed_at"]) <= at]
        single = room.build_state(TOPOLOGY, visible, {}, at)["nodes"][0]
        two = room.build_state(TOPOLOGY, visible, {metric: threshold}, at)["nodes"][0]
        usable = single["status"] not in ("unknown", "stale")
        one_coverage = usable and covered(visible, metric, at, 1)
        two_coverage = usable and covered(visible, metric, at, 2)
        decisions.append({"at": stamp(at), "visible_observations": [r["observed_at"] for r in visible],
                          "single_sample": {"covered": one_coverage,
                                            "warning": one_coverage and single["metrics"].get(metric, -1) > threshold},
                          "two_samples": {"covered": two_coverage,
                                          "warning": any(i["metric"] == metric for i in two["leading_indicators"])}})
    policies = {}
    for policy in POLICIES:
        warned = next((d for d in decisions if d[policy]["warning"]), None)
        # Allow one initial decision for a two-sample rule to warm up, then
        # require fresh telemetry at every remaining decision. A silent gap
        # must not be scored as a clean no-alarm or a missed prediction.
        warmup = 0 if policy == "single_sample" else 1
        coverage_checks = decisions[warmup:]
        has_coverage = bool(coverage_checks) and all(d[policy]["covered"] for d in coverage_checks)
        outcome = ("insufficient_data" if not warned and not has_coverage else
                   "hit" if warned and failure_at else "miss" if failure_at else
                   "false_alarm" if warned else "no_alarm")
        policies[policy] = {"outcome": outcome,
                            "coverage": "complete_after_warmup" if has_coverage else "partial_or_missing",
                            "first_warning_at": warned["at"] if warned else None,
                            "lead_seconds": int((failure_at - room.timestamp(warned["at"])).total_seconds())
                            if warned and failure_at else None}
    return {"id": case["id"], "failure_at": stamp(failure_at) if failure_at else None,
            "follow_up_end": stamp(follow_up_end), "policies": policies, "decisions": decisions}


def run(dataset):
    if dataset.get("schema_version") != 1 or dataset.get("data") != "SYNTHETIC_NOT_PRODUCTION":
        raise ValueError("this demonstration accepts its synthetic fixture only")
    cases = [replay_case(dataset, case) for case in dataset["cases"]]
    return {"schema_version": 1, "kind": "SYNTHETIC_EARLY_WARNING_REPLAY",
            "data": dataset["data"], "dataset": dataset["name"],
            "metric": dataset["metric"], "threshold": dataset["threshold"],
            "method": "Evaluate only pre-incident decision times using observations available at that time. "
                      "Outcomes and the absence of failure through follow-up are fixture labels. "
                      "Unwarned cases with gaps after the initial rule warm-up are insufficient data.",
            "limitations": "Constructed examples, not historical client incidents or predictive performance. "
                           "The dependency graph and user response are not evaluated here.",
            "summary": {policy: dict(Counter(c["policies"][policy]["outcome"] for c in cases))
                        for policy in POLICIES}, "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the full JSON report here; otherwise print it")
    args = parser.parse_args()
    report = json.dumps(run(json.loads(DATASET.read_text(encoding="utf-8"))), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"Synthetic replay written to {args.out}")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
