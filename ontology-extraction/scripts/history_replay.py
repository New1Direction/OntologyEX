#!/usr/bin/env python3
"""Replay a threshold signal against supplied incident labels and existing alerts.

    python history_replay.py --input history.json --format html --out report.html

Every sample carries its observation time and when it became available.
The latter is supplied by the dataset owner and is not independently verified.
No service is contacted, no alerts are sent, and input files are never changed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import html
import json
import math
from pathlib import Path
import re
import sys

import control_room as room

KIND = "ONTOLOGYEX_HISTORICAL_SIGNAL_DATASET"
MAX_CASES = 20
MAX_OBSERVATIONS = 300
MAX_DECISIONS = 301
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z")


class HistoryError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise HistoryError(message)


def shape(value, fields, where):
    require(type(value) is dict and set(value) == set(fields), f"{where}: unexpected or missing fields")


def instant(value, where):
    try:
        return room.timestamp(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HistoryError(f"{where}: invalid timestamp with timezone") from exc


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def reference(value, where):
    require(type(value) is str and REF.fullmatch(value) is not None,
            f"{where}: use a short reference containing letters, digits, '.', '_', ':', '/' or '-'")


def read_dataset(path: Path) -> dict:
    """Read a bounded JSON document, rejecting duplicate keys and non-finite numbers."""
    try:
        return room.parse_json(room.safe_read(path, room.MAX_OBSERVATION_BYTES))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise HistoryError(f"cannot read dataset: {exc}") from exc


def validate(dataset):
    shape(dataset, {"schema_version", "kind", "name", "metric", "threshold", "cases"}, "dataset")
    require(type(dataset["schema_version"]) is int and dataset["schema_version"] == 1 and
            dataset["kind"] == KIND, "unsupported dataset")
    reference(dataset["name"], "dataset name")
    metric = dataset["metric"]
    require(type(metric) is str and metric in room.METRICS, "unsupported metric")
    threshold = dataset["threshold"]
    shape(threshold, {"above", "selected_at", "training_data_available_through"}, "threshold")
    number(threshold["above"], metric, "threshold")
    selected_at = instant(threshold["selected_at"], "threshold.selected_at")
    training_cutoff = (instant(threshold["training_data_available_through"], "threshold.training_data_available_through")
                       if threshold["training_data_available_through"] is not None else None)
    require(training_cutoff is None or training_cutoff <= selected_at,
            "threshold: training cutoff cannot be after selection")
    cases = dataset["cases"]
    require(type(cases) is list and 0 < len(cases) <= MAX_CASES, "dataset needs 1..20 cases")
    ids = set()
    for case in cases:
        fields = {"id", "component", "window", "outcome", "baseline", "observations"}
        require(type(case) is dict and fields <= set(case) <= fields | {"capture_coverage"},
                "case: unexpected or missing fields")
        if "capture_coverage" in case:
            capture = case["capture_coverage"]
            shape(capture, {"state", "evidence_ref"}, "capture coverage")
            require(capture["state"] in ("unknown", "complete"), "invalid capture coverage")
            if capture["state"] == "complete":
                reference(capture["evidence_ref"], "capture coverage evidence")
            else:
                require(capture["evidence_ref"] is None, "unknown capture coverage must not claim evidence")
        reference(case["id"], "case id")
        require(case["id"] not in ids, "case ids must be unique")
        ids.add(case["id"])
        require(type(case["component"]) is str and room.IDENT_RE.fullmatch(case["component"]) is not None,
                f"{case['id']}: invalid component id")
        window = case["window"]
        shape(window, {"start", "end", "follow_up_end", "step_seconds"}, f"{case['id']} window")
        start = instant(window["start"], "window.start")
        end = instant(window["end"], "window.end")
        follow_up = instant(window["follow_up_end"], "window.follow_up_end")
        step = window["step_seconds"]
        require(type(step) is int and 1 <= step <= 86400, f"{case['id']}: invalid step_seconds")
        duration = (end - start).total_seconds()
        if training_cutoff is not None:
            require(training_cutoff < start, f"{case['id']}: training cutoff must precede the decision window")
        if selected_at > start:
            require(training_cutoff is not None,
                    f"{case['id']}: a retrospectively selected threshold requires a pre-window training cutoff")
        require(0 <= duration <= 86400 * 30 and duration.is_integer() and int(duration) % step == 0 and
                int(duration) // step + 1 <= MAX_DECISIONS and end < follow_up,
                f"{case['id']}: invalid decision window, cadence or follow-up")
        outcome = case["outcome"]
        shape(outcome, {"state", "incident_at", "evidence_ref"}, f"{case['id']} outcome")
        require(outcome["state"] in ("incident", "no_incident", "unreviewed"),
                f"{case['id']}: invalid outcome state")
        reference(outcome["evidence_ref"], f"{case['id']} outcome evidence")
        if outcome["state"] == "incident" or (outcome["state"] == "unreviewed" and outcome["incident_at"] is not None):
            incident_at = instant(outcome["incident_at"], "outcome.incident_at")
            require(end < incident_at <= follow_up, f"{case['id']}: incident must follow decisions within follow-up")
        else:
            require(outcome["incident_at"] is None, f"{case['id']}: a no-incident outcome cannot have incident_at")
        baseline = case["baseline"]
        shape(baseline, {"coverage", "alerts"}, f"{case['id']} baseline")
        require(baseline["coverage"] in ("complete", "unknown"), f"{case['id']}: invalid baseline coverage")
        require(type(baseline["alerts"]) is list and len(baseline["alerts"]) <= MAX_OBSERVATIONS,
                f"{case['id']}: too many baseline alerts")
        alert_refs = set()
        for alert in baseline["alerts"]:
            shape(alert, {"ref", "alerted_at", "available_at"}, "baseline alert")
            reference(alert["ref"], "baseline alert ref")
            require(alert["ref"] not in alert_refs, f"{case['id']}: duplicate baseline alert ref")
            alert_refs.add(alert["ref"])
            require(instant(alert["alerted_at"], "alerted_at") <= instant(alert["available_at"], "alert.available_at"),
                    f"{case['id']}: baseline alert cannot be available before it was emitted")
        observations = case["observations"]
        require(type(observations) is list and len(observations) <= MAX_OBSERVATIONS,
                f"{case['id']}: too many observations")
        refs = set()
        for row in observations:
            shape(row, {"ref", "observed_at", "available_at", "source", "status", "ttl_seconds", "value"},
                  "observation")
            reference(row["ref"], "observation ref")
            require(row["ref"] not in refs, f"{case['id']}: duplicate observation ref")
            refs.add(row["ref"])
            require(instant(row["observed_at"], "observed_at") <= instant(row["available_at"], "available_at"),
                    f"{case['id']}: observation cannot be available before it happened")
            require(type(row["source"]) is str and row["source"] in room.SOURCES and
                    type(row["status"]) is str and row["status"] in room.STATUS,
                    f"{case['id']}: invalid observation source or status")
            require(type(row["ttl_seconds"]) is int and 1 <= row["ttl_seconds"] <= room.MAX_AGE,
                    f"{case['id']}: invalid freshness lifetime")
            number(row["value"], metric, "observation value")
    return dataset


def number(value, metric, where):
    require(type(value) in (int, float) and 0 <= value <= 1e9 and math.isfinite(value),
            f"{where}: invalid number")
    require(metric not in room.PERCENT_METRICS or value <= 100, f"{where}: percentage exceeds 100")


def row_for_engine(case, row, metric):
    return {"schema_version": 1, "component_id": case["component"],
            "observed_at": iso(instant(row["observed_at"], "observed_at")),
            "source": row["source"], "status": row["status"],
            "expires_after_seconds": row["ttl_seconds"], "metrics": {metric: row["value"]}}


def histories(case, rows, metric):
    """Group equivalent deliveries into distinct sample instants, retaining references."""
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for row in rows:
        normalized = row_for_engine(case, row, metric)
        instant_ = instant(row["observed_at"], "observed_at")
        grouped[row["source"]][instant_][json.dumps(normalized, sort_keys=True)].append(row["ref"])
    return grouped


def covered(grouped, at):
    """Whether at least one source has two distinct, unambiguous fresh readings."""
    for instants in grouped.values():
        history = sorted(instants.items())
        if len(history) < 2:
            continue
        good = True
        for observed_at, variants in history[-2:]:
            if len(variants) != 1:
                good = False
                break
            row = json.loads(next(iter(variants)))
            if (at - observed_at).total_seconds() > row["expires_after_seconds"]:
                good = False
                break
        if good:
            return True
    return False


def conflict_rows(grouped):
    result = []
    for source, instants in sorted(grouped.items()):
        for observed_at, variants in sorted(instants.items()):
            if len(variants) > 1:
                result.append({"source": source, "observed_at": iso(observed_at),
                               "variants": [{"refs": sorted(refs),
                                             "status": json.loads(key)["status"],
                                             "value": next(iter(json.loads(key)["metrics"].values())),
                                             "ttl_seconds": json.loads(key)["expires_after_seconds"]}
                                            for key, refs in sorted(variants.items())]})
    return result


def assess(outcome, warning, coverage):
    if outcome["state"] == "unreviewed":
        return "needs_review"
    if warning is None and not coverage:
        return "insufficient_data"
    if outcome["state"] == "incident":
        return "hit" if warning is not None else "miss"
    return "false_alarm" if warning is not None else "no_alarm"


def replay_case(dataset, case):
    metric = dataset["metric"]
    start = instant(case["window"]["start"], "window.start")
    end = instant(case["window"]["end"], "window.end")
    step = timedelta(seconds=case["window"]["step_seconds"])
    topology = {"kind": "UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW", "project": "history-replay",
                "nodes": [{"id": case["component"], "kind": "declared_service", "label": case["component"]}],
                "edges": [], "source_evidence": [], "unresolved": [], "gaps": [], "coverage": {}}
    decisions, at = [], start
    while at <= end:
        visible = [r for r in case["observations"] if instant(r["observed_at"], "observed_at") <= at and
                   instant(r["available_at"], "available_at") <= at]
        grouped = histories(case, visible, metric)
        state = room.build_state(topology, [row_for_engine(case, r, metric) for r in visible],
                                 {metric: dataset["threshold"]["above"]}, at)["nodes"][0]
        coverage = state["status"] not in ("unknown", "stale") and covered(grouped, at)
        signals = state["leading_indicators"]
        evidence = []
        for signal in signals:
            refs = []
            for observed in (signal["previous_observed_at"], signal["current_observed_at"]):
                candidates = grouped[signal["source"]][instant(observed, "signal timestamp")]
                refs.append(sorted(ref for group in candidates.values() for ref in group))
            evidence.append({**signal, "previous_refs": refs[0], "current_refs": refs[1]})
        decisions.append({"at": iso(at), "visible_refs": sorted(r["ref"] for r in visible),
                          "status": state["status"], "covered": coverage, "signals": evidence})
        at += step
    signal_decision = next((d for d in decisions if d["signals"]), None)
    signal_at = instant(signal_decision["at"], "decision.at") if signal_decision else None
    # One initial step allows a two-sample rule to warm up. Subsequent gaps
    # make an unwarned case unscorable rather than a miss or a clean no-alarm.
    coverage_checks = decisions[1:]
    capture = case.get("capture_coverage")
    signal_coverage = (bool(coverage_checks) and all(d["covered"] for d in coverage_checks) and
                       (capture is None or capture["state"] == "complete"))
    eligible_alerts = [a for a in case["baseline"]["alerts"]
                       if start <= instant(a["available_at"], "alert.available_at") <= end]
    eligible_alerts.sort(key=lambda a: (instant(a["available_at"], "alert.available_at"), a["ref"]))
    baseline_alert = eligible_alerts[0] if eligible_alerts else None
    baseline_at = instant(baseline_alert["available_at"], "alert.available_at") if baseline_alert else None
    incident_at = (instant(case["outcome"]["incident_at"], "outcome.incident_at")
                   if case["outcome"]["state"] == "incident" else None)
    def policy(warning_at, coverage, ref, complete_label, capture_unknown=False):
        # Missing captured readings could change even an apparent warning, not
        # just an apparent absence of warning. Keep it visible but unscored.
        outcome = ("insufficient_data" if capture_unknown and case["outcome"]["state"] != "unreviewed"
                   else assess(case["outcome"], warning_at, coverage))
        return {"outcome": outcome,
                "coverage": complete_label if coverage else "partial_or_unknown",
                "first_warning_at": iso(warning_at) if warning_at else None, "evidence_ref": ref,
                "lead_seconds": int((incident_at - warning_at).total_seconds())
                if incident_at is not None and warning_at is not None else None}
    known_by_end = [r for r in case["observations"] if instant(r["observed_at"], "observed_at") <= end and
                    instant(r["available_at"], "available_at") <= end]
    delayed = [r for r in case["observations"] if instant(r["observed_at"], "observed_at") <= end and
               instant(r["available_at"], "available_at") > end]
    threshold_mode = ("selected_before_window" if instant(dataset["threshold"]["selected_at"], "selected_at") <= start
                      else "retrospective_holdout")
    return {"id": case["id"], "component": case["component"], "window": case["window"],
            "threshold_mode": threshold_mode,
            "outcome": case["outcome"], "baseline": case["baseline"], "capture_coverage": capture,
            "policies": {"two_samples": policy(signal_at, signal_coverage,
                                               signal_decision["signals"] if signal_decision else None,
                                               "fresh_at_sampled_decisions_after_warmup",
                                               capture_unknown=capture is not None and capture["state"] == "unknown"),
                         "existing_alerts": policy(baseline_at, case["baseline"]["coverage"] == "complete",
                                                   baseline_alert["ref"] if baseline_alert else None, "complete")},
            "outside_alert_refs": sorted(a["ref"] for a in case["baseline"]["alerts"] if a not in eligible_alerts),
            "late_refs": sorted(r["ref"] for r in delayed),
            "conflicts": conflict_rows(histories(case, known_by_end, metric)),
            "observations": sorted(case["observations"], key=lambda r: (instant(r["observed_at"], "observed_at"), r["ref"])),
            "decisions": decisions}


def run(dataset):
    validate(dataset)
    cases = [replay_case(dataset, case) for case in dataset["cases"]]
    overlaps = []
    for index, first in enumerate(cases):
        for second in cases[index + 1:]:
            if (first["component"] == second["component"] and
                    instant(first["window"]["start"], "window.start") <=
                    instant(second["window"]["follow_up_end"], "window.follow_up_end") and
                    instant(second["window"]["start"], "window.start") <=
                    instant(first["window"]["follow_up_end"], "window.follow_up_end")):
                overlaps.append([first["id"], second["id"]])
    return {"schema_version": 1, "kind": "ONTOLOGYEX_HISTORICAL_SIGNAL_REPORT", "dataset": dataset["name"],
            "metric": dataset["metric"], "threshold": dataset["threshold"],
            "semantics": "USER_SUPPLIED_TIMES_AND_LABELS_NOT_INDEPENDENTLY_VERIFIED",
            "limitations": ["This simulates a threshold rule on supplied cases; it does not establish that the rule ran at the time or predict future incidents.",
                            "Observation availability, incident labels, baseline coverage, threshold selection and any training cutoff are user assertions.",
                            "Freshness is checked at supplied decision points using the supplied TTL, not continuously. Capture coverage, when supplied, is a user assertion.",
                            "Only a single component is evaluated per case; topology and downstream effects are not evaluated.",
                            "Results count cases, not unique incidents; overlapping cases can count the same event more than once."],
            "overlapping_cases": overlaps,
            "summary": {policy: dict(Counter(c["policies"][policy]["outcome"] for c in cases))
                        for policy in ("two_samples", "existing_alerts")}, "cases": cases}


def render_html(report):
    esc = lambda value: html.escape(str(value), quote=True)
    summaries = ("hit", "miss", "false_alarm", "no_alarm", "insufficient_data", "needs_review")
    lines = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
             '<title>OntologyEX — Historical signal replay</title><style>',
             'body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#08121c;color:#e6f0f4}main{max-width:1080px;margin:auto;padding:32px 20px}h1{font-size:32px}h2{font-size:21px}h3{font-size:16px}.muted{color:#a8bdc9}.notice,.case{border:1px solid #2b4656;border-radius:12px;background:#0d1c29;padding:16px;margin:18px 0}.notice{border-color:#846b36}table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}th,td{padding:8px;text-align:left;vertical-align:top;border-bottom:1px solid #2b4656;overflow-wrap:anywhere}th{color:#b9ccd5}code{overflow-wrap:anywhere}details{margin:14px 0}summary{cursor:pointer;color:#77dce7}small{color:#a8bdc9}@media(max-width:700px){main{padding:20px 12px}table{font-size:12px}th,td{padding:6px 4px}}',
             '</style></head><body><main><p class="muted">ONTOLOGYEX · HISTORICAL REPLAY</p>',
             f'<h1>{esc(report["dataset"])}</h1>',
             f'<p>Metric: <strong>{esc(report["metric"])}</strong> · threshold: above {esc(report["threshold"]["above"])} · selected {esc(report["threshold"]["selected_at"])}'
             f'{(" · claimed training availability cutoff: " + esc(report["threshold"]["training_data_available_through"])) if report["threshold"]["training_data_available_through"] else ""}</p>',
             '<div class="notice">This simulates a rule on supplied history, not a prediction of future incidents or proof the rule ran then. '
             'Availability times, outcome labels, training cutoff and baseline coverage are supplied by the dataset owner and are not independently verified. '
             'An observation time is not necessarily the time the underlying event began. '
             'Unreviewed outcomes and missing data are kept separate from hits and misses. '
             'Results count cases rather than unique incidents.</div>',
             '<h2>Results</h2><table><thead><tr><th>Outcome</th><th>Replayed two-reading rule</th><th>Existing alerts</th></tr></thead><tbody>']
    for outcome in summaries:
        lines.append(f'<tr><td>{esc(outcome.replace("_", " "))}</td><td>{report["summary"]["two_samples"].get(outcome, 0)}</td>'
                     f'<td>{report["summary"]["existing_alerts"].get(outcome, 0)}</td></tr>')
    lines.append('</tbody></table>')
    if report["overlapping_cases"]:
        pairs = '; '.join(f'{a} / {b}' for a, b in report["overlapping_cases"])
        lines.append(f'<div class="notice">These cases overlap for the same component: {esc(pairs)}. '
                     'The same incident or alert may be counted more than once.</div>')
    for case in report["cases"]:
        lines.append(f'<section class="case"><h2>{esc(case["id"])}</h2>')
        lines.append(f'<p class="muted">Component {esc(case["component"])} · decisions {esc(case["window"]["start"])} to {esc(case["window"]["end"])} · '
                     f'follow-up through {esc(case["window"]["follow_up_end"])}</p>')
        if case["threshold_mode"] == "retrospective_holdout":
            lines.append('<p>Retrospective holdout: the threshold was selected after this window; '
                         'the supplied training availability cutoff predates it and has not been independently verified.</p>')
        if case["capture_coverage"] is not None:
            capture = case["capture_coverage"]
            lines.append(f'<p>Recording coverage: <strong>{esc(capture["state"])}</strong>'
                         f'{(" · reviewer evidence: <code>" + esc(capture["evidence_ref"]) + "</code>") if capture["evidence_ref"] else ""}. '
                         'Coverage assertions are not independently verified.</p>')
        time_label = " · candidate time " if case["outcome"]["state"] == "unreviewed" else " at "
        lines.append(f'<p>Supplied outcome: <strong>{esc(case["outcome"]["state"])}</strong>'
                     f'{(time_label + esc(case["outcome"]["incident_at"])) if case["outcome"]["incident_at"] else ""} '
                     f'· evidence: <code>{esc(case["outcome"]["evidence_ref"])}</code></p>')
        lines.append('<table><thead><tr><th>Rule</th><th>Result</th><th>First warning</th><th>Lead time</th><th>Coverage</th></tr></thead><tbody>')
        for key, name in (("two_samples", "Replayed two-reading rule"), ("existing_alerts", "Existing alerts")):
            p = case["policies"][key]
            lead = f'{p["lead_seconds"]} seconds' if p["lead_seconds"] is not None else "—"
            lines.append(f'<tr><td>{name}</td><td>{esc(p["outcome"].replace("_", " "))}</td>'
                         f'<td>{esc(p["first_warning_at"] or "—")}</td><td>{lead}</td><td>{esc(p["coverage"].replace("_", " "))}</td></tr>')
        lines.append('</tbody></table>')
        if case["late_refs"]:
            lines.append(f'<p>Arrived after the decision window and excluded: <code>{esc(", ".join(case["late_refs"]))}</code>.</p>')
        if case["outside_alert_refs"]:
            lines.append(f'<p>Existing alerts outside the decision window and excluded: '
                         f'<code>{esc(", ".join(case["outside_alert_refs"]))}</code>.</p>')
        if case["conflicts"]:
            lines.append('<h3>Conflicting readings · review needed</h3><ul>')
            for conflict in case["conflicts"]:
                variants = '; '.join(f'{", ".join(v["refs"])}: {v["value"]} ({v["status"]}, TTL {v["ttl_seconds"]}s)'
                                     for v in conflict["variants"])
                lines.append(f'<li>{esc(conflict["source"])} at {esc(conflict["observed_at"])}: {esc(variants)}</li>')
            lines.append('</ul><p class="muted">The replay does not choose between conflicting samples.</p>')
        lines.append('<details><summary>Review observation times and sources</summary><table><thead><tr><th>Reference</th><th>Observed</th><th>Available</th><th>Source / status</th><th>Value</th></tr></thead><tbody>')
        for row in case["observations"]:
            lines.append(f'<tr><td>{esc(row["ref"])}</td><td>{esc(row["observed_at"])}</td><td>{esc(row["available_at"])}</td>'
                         f'<td>{esc(row["source"])} / {esc(row["status"])}</td><td>{esc(row["value"])}</td></tr>')
        lines.append('</tbody></table></details>')
        lines.append('<details><summary>Review existing alert evidence</summary>')
        lines.append(f'<p>Coverage asserted by the dataset: {esc(case["baseline"]["coverage"])}.</p>')
        if case["baseline"]["alerts"]:
            lines.append('<table><thead><tr><th>Reference</th><th>Alerted</th><th>Available</th></tr></thead><tbody>')
            for alert in case["baseline"]["alerts"]:
                lines.append(f'<tr><td>{esc(alert["ref"])}</td><td>{esc(alert["alerted_at"])}</td>'
                             f'<td>{esc(alert["available_at"])}</td></tr>')
            lines.append('</tbody></table>')
        else:
            lines.append('<p>No existing alerts supplied.</p>')
        lines.append('</details>')
        lines.append('<details><summary>Review what was available at each decision</summary><table><thead><tr><th>Decision time</th><th>Visible references</th><th>State</th><th>Signal</th></tr></thead><tbody>')
        for d in case["decisions"]:
            signals = '; '.join(f'{s["previous"]} → {s["current"]} ({", ".join(s["previous_refs"])} → {", ".join(s["current_refs"])})' for s in d["signals"]) or "—"
            lines.append(f'<tr><td>{esc(d["at"])}</td><td>{esc(", ".join(d["visible_refs"]) or "—")}</td><td>{esc(d["status"])}</td><td>{esc(signals)}</td></tr>')
        lines.append('</tbody></table></details></section>')
    lines.append('<p class="muted">Only the named component and configured threshold are evaluated. Dependency paths and customer impact are not measured by this report.</p></main></body></html>')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="a local history dataset with outcome labels")
    parser.add_argument("--format", choices=("json", "html"), default="json")
    parser.add_argument("--out", type=Path, help="new output file; never overwrites")
    args = parser.parse_args(argv)
    try:
        report = run(read_dataset(args.input))
        content = render_html(report) if args.format == "html" else json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.out is None:
            print(content, end="")
        else:
            with args.out.open("x", encoding="utf-8") as out:
                out.write(content)
            print(f"Historical replay written to {args.out}")
        return 0
    except (HistoryError, room.RoomError, OSError, ValueError, TypeError) as exc:
        print(f"Historical replay refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
