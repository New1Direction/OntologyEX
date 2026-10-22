#!/usr/bin/env python3
"""Normalize bounded OTLP/JSON GenAI spans into OntologyEX's strict JSONL evidence input.

Only allowlisted non-content metadata is copied. No prompts, tool arguments/results,
token keys, messages, or arbitrary attributes leave this adapter. No model calls.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import trace_extract as tx

MAX_SOURCE = 2097152
MAX_SPANS = 512
KNOWN_ATTRIBUTES = {"gen_ai.operation.name", "gen_ai.agent.name", "gen_ai.tool.name",
                    "ontologyex.root_goal_id", "ontologyex.entity_type"}
OPERATIONS = {"invoke_agent", "create_agent", "execute_tool", "invoke_workflow",
              "plan", "generate_content", "chat", "text_completion"}


def attributes(raw: object) -> dict:
    tx.require(type(raw) is list and len(raw) <= 128, "invalid attributes")
    result = {}
    for item in raw:
        tx.require(type(item) is dict, "invalid attribute entry")
        key = item.get("key")
        if key not in KNOWN_ATTRIBUTES:
            continue  # never carry raw content or arbitrary span metadata
        tx.require(key not in result, "duplicate allowlisted attribute")
        value = item.get("value")
        tx.require(type(value) is dict and type(value.get("stringValue")) is str,
                   "invalid allowlisted string attribute")
        clean = value["stringValue"]
        tx.require(0 < len(clean) <= tx.MAX_VALUE and tx.SAFE.fullmatch(clean) is not None,
                   "unsafe allowlisted attribute")
        result[key] = clean
    return result


def normalize(source: bytes) -> bytes:
    tx.require(0 < len(source) <= MAX_SOURCE, "OTLP input exceeds 2 MiB")
    root = json.loads(source.decode("utf-8"), object_pairs_hook=tx.no_duplicates)
    tx.require(type(root) is dict and type(root.get("resourceSpans")) is list,
               "expected OTLP JSON resourceSpans")
    extracted = []
    for resource in root["resourceSpans"]:
        tx.require(type(resource) is dict and type(resource.get("scopeSpans")) is list,
                   "invalid resourceSpans record")
        for scope in resource["scopeSpans"]:
            tx.require(type(scope) is dict and type(scope.get("spans")) is list,
                       "invalid scopeSpans record")
            for span in scope["spans"]:
                tx.require(type(span) is dict, "invalid span")
                trace_id, span_id = span.get("traceId"), span.get("spanId")
                tx.require(type(trace_id) is str and tx.ID.fullmatch(trace_id) is not None,
                           "missing/unsafe traceId")
                tx.require(type(span_id) is str and tx.ID.fullmatch(span_id) is not None,
                           "missing/unsafe spanId")
                a = attributes(span.get("attributes", []))
                op = a.get("gen_ai.operation.name")
                kind = "tool" if op == "execute_tool" else "agent" if op in {"invoke_agent", "create_agent"} else "observation"
                status_data = span.get("status") or {}
                tx.require(type(status_data) is dict, "invalid status")
                code = status_data.get("code", "STATUS_CODE_UNSET")
                status = {"STATUS_CODE_OK": "ok", "STATUS_CODE_ERROR": "error",
                          "STATUS_CODE_UNSET": "unknown", 1: "ok", 2: "error", 0: "unknown"}.get(code, "unknown")
                row = {"schema_version": 1, "trace_id": trace_id, "span_id": span_id,
                       "kind": kind, "status": status}
                if a.get("gen_ai.agent.name"):
                    row["agent"] = a["gen_ai.agent.name"]
                if kind == "tool" and a.get("gen_ai.tool.name"):
                    row["tool"] = a["gen_ai.tool.name"]
                if op in OPERATIONS:
                    row["operation"] = op
                if a.get("ontologyex.root_goal_id"):
                    row["root_goal_id"] = a["ontologyex.root_goal_id"]
                if a.get("ontologyex.entity_type"):
                    row["entity_type"] = a["ontologyex.entity_type"]
                start = span.get("startTimeUnixNano")
                if start is not None:
                    tx.require(type(start) is str and start.isdecimal() and len(start) <= 20,
                               "invalid startTimeUnixNano")
                    try:
                        row["timestamp"] = datetime.fromtimestamp(
                            int(start) // 1000000000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    except (OverflowError, OSError, ValueError) as exc:
                        raise tx.TraceError("timestamp out of range") from exc
                parent = span.get("parentSpanId")
                if parent:
                    tx.require(type(parent) is str and tx.ID.fullmatch(parent) is not None,
                               "unsafe parentSpanId")
                    row["parent_span_id"] = parent
                extracted.append(row)
                tx.require(len(extracted) <= MAX_SPANS, "span count exceeded")
    available = {(s["trace_id"], s["span_id"]) for s in extracted}
    for row in extracted:
        if "parent_span_id" in row and (row["trace_id"], row["parent_span_id"]) not in available:
            # A partial OTLP export does not prove the missing span; never synthesize it.
            del row["parent_span_id"]
    output = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                     for row in extracted).encode("utf-8")
    tx.parse(output)  # enforce identical privacy/size/graph input contract
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="OTLP/JSON local export")
    parser.add_argument("--out", required=True, type=Path, help="new normalized JSONL file")
    args = parser.parse_args()
    try:
        tx.require(args.input.is_file() and not args.input.is_symlink(),
                   "input must be a regular file")
        out = args.out
        tx.require(not out.exists() and not out.is_symlink(), "output exists")
        tx.require(not any(p.is_symlink() for p in (out.parent, *out.parent.parents)),
                   "symlinked output parent")
        data = normalize(args.input.read_bytes())
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("xb") as handle:
            handle.write(data)
        print(tx.encoded({"status": "NORMALIZED_TRACE_ONLY", "out": str(out),
                          "bytes": len(data)}).decode(), end="")
        return 0
    except (tx.TraceError, ValueError, OSError, TypeError, UnicodeError, RecursionError) as exc:
        print(tx.encoded({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
