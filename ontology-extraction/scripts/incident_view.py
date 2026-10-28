"""Read-only evidence-first incident investigation.

A report never treats status samples as independently authenticated, and a
source-declared dependency never establishes an observed outage or root cause.
No network access, side effects, repair, or source code execution.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

MAX_EVENTS = 32
MAX_DEPENDENTS = 40
MAX_REVISIONS = 8
MAX_RELATED = 40


class IncidentError(ValueError):
    pass


def require(cond, message):
    if not cond:
        raise IncidentError(message)


def incident(full: dict, rows: list[dict], node_id: str) -> dict:
    require(type(node_id) is str and 0 < len(node_id) <= 1024 and
            not any(ord(c) < 32 for c in node_id),
            "invalid component identity")
    nodes = {n["id"]: n for n in full["nodes"]}
    require(node_id in nodes, "unknown component")
    current = nodes[node_id]
    time = datetime.fromisoformat(full["generated_at"].replace("Z", "+00:00"))
    require(time.tzinfo is not None, "invalid evaluated time")
    observations = []
    for o in rows:
        if o.get("component_id") != node_id:
            continue
        when = datetime.fromisoformat(o["observed_at"].replace("Z", "+00:00"))
        if when > time:
            continue
        age_seconds = max(0, (time - when).total_seconds())
        observations.append({
            "observed_at": o["observed_at"],
            "status": o["status"],
            "source": o["source"],
            "metrics": {k: o["metrics"][k] for k in sorted(o["metrics"])},
            "expires_after_seconds": o["expires_after_seconds"],
            "age_seconds": round(age_seconds, 3),
            "fresh_at_evaluation": age_seconds <= o["expires_after_seconds"],
            "authenticity": "UNATTESTED_LOCAL_INPUT",
        })
    observations.sort(key=lambda o: (
        o["observed_at"], o["source"], o["status"],
        tuple(o["metrics"].items())))
    selected = observations[-MAX_EVENTS:]
    exposure = sorted(
        (n for n in full["nodes"] if node_id in n.get("potentially_impacted_by", [])),
        key=lambda n: (n["status"] != "error", n["label"], n["id"]))
    # These are candidate exposures, not confirmed downstream failures.
    possible = [
        {"id": n["id"], "label": n["label"],
         "independently_reported_status": n["status"]}
        for n in exposure[:MAX_DEPENDENTS]
    ]
    direct = sorted((e for e in full["edges"]
                     if e["source"] == node_id or e["target"] == node_id),
                    key=lambda e:(e["relation"],e["source"],e["target"],
                                  e.get("source_line",0)))
    node_evidence_id = current.get("evidence_id")
    relevant_evidence = {node_evidence_id} | {e.get("evidence_id") for e in direct[:MAX_RELATED]}
    evidence = [e for e in full["source_evidence"] if e["id"] in relevant_evidence]
    history = full.get("snapshot_history", [])[:MAX_REVISIONS]
    return {
        "schema_version": 1,
        "kind": "UNATTESTED_INCIDENT_INVESTIGATION",
        "component_id": node_id,
        "component": {
            "label": current["label"], "kind": current["kind"],
            "reported_state": current["status"],
            "source_status": current.get("source_status"),
            "source_authenticity": current.get("source_authenticity", "UNKNOWN"),
            "latest_observed_at": current.get("observed_at"),
            "leading_indicators": current.get("leading_indicators", []),
            "ci_run": current.get("ci_run"),
        },
        "evaluated_at": full["generated_at"],
        "observation_count": len(observations),
        "observations_included": len(selected),
        "older_observations_omitted": max(0, len(observations) - len(selected)),
        "timeline": selected,
        "direct_relationships": direct[:MAX_RELATED],
        "direct_relationships_omitted": max(0, len(direct) - MAX_RELATED),
        "source_evidence": evidence,
        "possible_dependents": possible,
        "possible_dependents_omitted": max(0, len(exposure) - MAX_DEPENDENTS),
        "source_history": history,
        "source_history_semantics": "SOURCE_SCAN_VERSIONS_NOT_DEPLOYMENT_EVENTS",
        "impact_semantics": "DECLARED_DEPENDENCY_POTENTIAL_IMPACT_NOT_CAUSAL_PROOF",
        "observation_semantics": "SOURCE_REPORTED_NOT_AUTHENTICATED_OR_VERIFIED",
        "root_cause": "UNDETERMINED",
        "recommended_diagnosis": "VERIFY_TELEMETRY_IDENTITY_AND_CHECK_DEPLOYMENT_LOGS",
        "runtime_discovery": full["runtime_discovery"],
        "execution_authorized": False,
    }
