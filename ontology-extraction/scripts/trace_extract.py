#!/usr/bin/env python3
"""Convert normalized agent execution spans to an evidence-linked ontology draft.

Offline, deterministic, no model calls, no raw prompt/tool payload retention, no execution.
Trace observations are NOT business rules, permissions, or verified independent sources.
Input: one schema_version=1 JSON object per JSONL line (see references/trace-to-ontology.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_SOURCE_BYTES = 1048576
MAX_SPANS = 512
MAX_LINE_BYTES = 4096
MAX_VALUE = 96
FIELDS = {"schema_version", "trace_id", "span_id", "parent_span_id", "timestamp",
          "root_goal_id", "kind", "agent", "tool", "entity_type", "entity_id",
          "operation", "effect", "status"}
REQUIRED = {"schema_version", "trace_id", "span_id", "kind", "status"}
KINDS = {"agent", "tool", "action", "decision", "observation"}
STATUSES = {"ok", "error", "unknown"}
EFFECTS = {"created", "updated", "deleted", "read", "none", "unknown"}
# No whitespace, query strings, credentials, email addresses, or unconstrained free text.
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
TIMESTAMP = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z")


class TraceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TraceError(message)


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def no_duplicates(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def utc_timestamp(value: str, number: int) -> str:
    match = TIMESTAMP.fullmatch(value)
    require(match is not None, f"line {number}: invalid timestamp")
    seconds, fraction, offset = match.groups()
    require(offset != "-00:00", f"line {number}: timestamp requires a known UTC offset")
    try:
        utc = datetime.fromisoformat(seconds + offset.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise TraceError(f"line {number}: invalid timestamp") from exc
    # Keep up to nanosecond precision without datetime's microsecond truncation.
    fraction = (fraction or "").rstrip("0")
    return utc.replace(tzinfo=None).isoformat() + ("." + fraction if fraction else "") + "Z"


def parse(source: bytes) -> list[dict]:
    require(0 < len(source) <= MAX_SOURCE_BYTES, "trace input must be 1 byte–1 MiB")
    lines = source.splitlines()
    require(0 < len(lines) <= MAX_SPANS, "trace span budget exceeded")
    spans, seen = [], set()
    for number, raw in enumerate(lines, 1):
        require(0 < len(raw) <= MAX_LINE_BYTES, f"line {number}: invalid byte length")
        try:
            entry = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates,
                               parse_constant=lambda _: (_ for _ in ()).throw(TraceError("nonfinite number")))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise TraceError(f"line {number}: invalid JSON: {exc}") from exc
        require(type(entry) is dict, f"line {number}: expected object")
        require(REQUIRED <= entry.keys() and entry.keys() <= FIELDS,
                f"line {number}: missing/unknown fields (raw content is forbidden)")
        require(type(entry["schema_version"]) is int and entry["schema_version"] == 1,
                f"line {number}: unsupported schema")
        for field, value in entry.items():
            if field == "schema_version":
                continue
            require(type(value) is str and 0 < len(value) <= MAX_VALUE,
                    f"line {number}: invalid {field}")
            if field == "timestamp":
                entry[field] = utc_timestamp(value, number)
                continue
            require((ID if field in {"trace_id", "span_id", "parent_span_id", "root_goal_id"} else SAFE).fullmatch(value) is not None,
                    f"line {number}: unsafe identifier {field}")
        require(entry["kind"] in KINDS and entry["status"] in STATUSES,
                f"line {number}: invalid kind/status")
        if "effect" in entry:
            require(entry["effect"] in EFFECTS, f"line {number}: invalid effect")
        require("tool" not in entry or entry["kind"] in {"tool", "action", "observation"},
                f"line {number}: tool requires tool/action/observation span")
        key = (entry["trace_id"], entry["span_id"])
        require(key not in seen, f"line {number}: duplicate span")
        require(entry.get("parent_span_id") != entry["span_id"],
                f"line {number}: self-parent span")
        seen.add(key)
        spans.append(entry)
    for e in spans:
        if "parent_span_id" in e:
            require((e["trace_id"], e["parent_span_id"]) in seen,
                    "dangling parent span: " + e["span_id"])
    parents = {(e["trace_id"], e["span_id"]): (e["trace_id"], e["parent_span_id"])
               for e in spans if "parent_span_id" in e}
    completed = set()
    for key in parents:
        path = set()
        while key in parents and key not in completed:
            require(key not in path, "parent cycle: " + key[1])
            path.add(key)
            key = parents[key]
        completed.update(path)
    return spans


def project(source: bytes) -> dict:
    spans = parse(source)
    file_hash = sha(source)
    nodes, edges, evidence = {}, {}, {}
    goal_ids = sorted({e["root_goal_id"] for e in spans if "root_goal_id" in e})
    # Without the terminal Z, a whole second sorts before its fractional instants.
    timestamps = sorted({e["timestamp"] for e in spans if "timestamp" in e}, key=lambda t: t[:-1])
    objects, actions, observed_links = {}, {}, {}
    def node(kind: str, label: str) -> str:
        # Raw entity IDs are NEVER stored. All labels are allowlisted vocabulary.
        key = sha(encoded([kind, label]))[:24]
        nodes[key] = {"id": key, "kind": kind, "label": label}
        return key

    def edge(source_id: str, relation: str, destination_id: str, evidence_id: str) -> None:
        key = sha(encoded([source_id, relation, destination_id]))[:24]
        if key not in edges:
            edges[key] = {"id": key, "from": source_id, "relation": relation,
                          "to": destination_id, "evidence": []}
        edges[key]["evidence"].append(evidence_id)

    span_nodes = {}
    for number, e in enumerate(spans, 1):
        eid = "trace-" + sha(encoded([file_hash, number, e["trace_id"], e["span_id"]]))[:20]
        evidence[eid] = {"id": eid, "input_sha256": file_hash, "line": number,
                         "line_sha256": sha(source.splitlines()[number - 1]),
                         "claim_status": "OBSERVED_IN_TRACE_NOT_VERIFIED_POLICY"}
        span_key = node("span", sha(encoded([e["trace_id"], e["span_id"]]))[:24])
        span_nodes[(e["trace_id"], e["span_id"])] = span_key
        if "agent" in e:
            edge(span_key, "performed_by", node("agent", e["agent"]), eid)
        if "tool" in e:
            edge(span_key, "called", node("tool", e["tool"]), eid)
        if "entity_type" in e:
            object_type = e["entity_type"]
            edge(span_key, "observed_type", node("object_type", object_type), eid)
            objects.setdefault(object_type, []).append(eid)
            if "entity_id" in e:
                # Identity is scoped to the trace and irreversibly hashed before output.
                opaque_id = sha(encoded([e["trace_id"], object_type, e["entity_id"]]))[:24]
                edge(span_key, "referenced", node("opaque_instance", opaque_id), eid)
        if "operation" in e:
            edge(span_key, "attempted", node("operation", e["operation"]), eid)
            actions.setdefault(e["operation"], []).append(eid)
        edge(span_key, "resulted_in", node("outcome", e["status"]), eid)
        if e.get("effect") and e["effect"] != "none":
            edge(span_key, "observed_effect", node("effect", e["effect"]), eid)
        if "agent" in e and "tool" in e:
            observed_links.setdefault(("agent_calls_tool", e["agent"], e["tool"]), []).append(eid)
        if "tool" in e and "entity_type" in e:
            observed_links.setdefault(("tool_touches_type", e["tool"], e["entity_type"]), []).append(eid)
    for e in spans:
        if "parent_span_id" in e:
            child = span_nodes[(e["trace_id"], e["span_id"])]
            parent = span_nodes[(e["trace_id"], e["parent_span_id"])]
            eid = next(v["id"] for v in evidence.values()
                       if v["line"] == spans.index(e) + 1)
            edge(child, "child_of", parent, eid)
    return {
        "schema_version": 1, "status": "DRAFT_NOT_APPROVED",
        "source_sha256": file_hash, "span_count": len(spans),
        "root_goal_ids": goal_ids, "observed_timestamp_range":
            {"first": timestamps[0], "last": timestamps[-1]} if timestamps else None,
        "source_independence_verified": False, "execution_authorized": False,
        "ontology_draft": {
            "object_types": [{"id": k, "evidence": v, "review": "REQUIRED"} for k, v in sorted(objects.items())],
            "action_types": [{"id": k, "evidence": v, "review": "REQUIRED"} for k, v in sorted(actions.items())],
            "link_candidates": [{"relation": rel, "source": x, "target": y,
                                 "evidence": v, "review": "REQUIRED"}
                                for (rel, x, y), v in sorted(observed_links.items())],
            "rules": [], "permissions": [], "inferred_preconditions": []},
        "graph": {"nodes": sorted(nodes.values(), key=lambda n: n["id"]),
                  "edges": sorted(edges.values(), key=lambda n: n["id"])},
        "evidence": list(evidence.values()),
        "limitations": [
            "Only normalized allowlisted metadata is accepted; raw payloads are forbidden.",
            "Occurrences are observations, not independent confirmation or causal evidence.",
            "No production rules, approvals, permissions, or autonomous actions are inferred.",
            "Line numbers and digests identify input bytes but do not authenticate origin.",
        ],
    }


def write_new(out: Path, data: dict) -> None:
    require(not out.exists() and not out.is_symlink(), "output already exists")
    require(not any(p.is_symlink() for p in (out.parent, *out.parent.parents)),
            "symlinked output ancestor")
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".trace-draft-", dir=out.parent))
    try:
        (stage / "ontology-draft.json").write_bytes(encoded(data))
        stage.rename(out)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="normalized .jsonl file")
    parser.add_argument("--out", required=True, type=Path, help="new output folder")
    args = parser.parse_args()
    try:
        require(args.input.is_file() and not args.input.is_symlink(), "input must be a regular file")
        data = project(args.input.read_bytes())
        write_new(args.out, data)
        print(encoded({"status": data["status"], "out": str(args.out),
                       "spans": data["span_count"]}).decode(), end="")
        return 0
    except (TraceError, OSError, UnicodeError, TypeError, RecursionError) as exc:
        print(encoded({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
