#!/usr/bin/env python3
"""Suggest dependencies between components from calls seen in traces; a person confirms each.

The feeds (`feeds.py` or `ontologyex-feeds`) pair client and server spans into calls
between services and keep the counts in a calls file beside the observations. A call
from A to B that the repository does not already declare is suggested as "A depends
on B", with how often and when it was seen. Only an explicit confirm decision adds it
to the dependencies file; the control room then draws it as a CALLS edge and carries
possible impact along it. A reject keeps it from being suggested again.

Traces are source-reported: a call seen is not proof that A needs B, and a
confirmed dependency is a person's decision, recorded with who made it.

    python dependency_suggest.py suggest --repo REPO --calls OBS.calls.json --dependencies deps.json
    python dependency_suggest.py confirm --repo REPO --calls OBS.calls.json --dependencies deps.json \\
        --id ID --by NAME [--note TEXT]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import decisions
import feeds
from policy_check import DomainError, MAX_TOTAL, canonical, digest, parse_json, require, safe_file

DEPENDENCY_KIND = "ONTOLOGYEX_DEPENDENCIES"
MAX_DEPENDENCIES = 1024
MAX_SHOWN = 100
# Ordinal, by how often the call was seen; not a probability.
STRENGTH = (("STRONG", 20), ("MODERATE", 5), ("WEAK", 1))
EVIDENCE_KEYS = {"caller_services", "calls", "errors", "first_seen", "last_seen"}
DECLARED = ("DEPENDS_ON", "CALLS")


def _rows(rows: object, seen: set) -> list[dict]:
    require(type(rows) is list and len(rows) <= MAX_DEPENDENCIES, "dependencies: too many rows")
    for row in rows:
        require(type(row) is dict and set(row) == {"source", "target", "declared_by", "note", "evidence"},
                "dependencies: unexpected row fields")
        pair = (row["source"], row["target"])
        require(all(type(x) is str and feeds.IDENT.fullmatch(x) for x in pair) and pair[0] != pair[1]
                and pair not in seen, "dependencies: invalid or repeated pair")
        seen.add(pair)
        require(type(row["declared_by"]) is str and 0 < len(row["declared_by"]) <= 120
                and type(row["note"]) is str and len(row["note"]) <= 500, "dependencies: invalid decider or note")
        evidence = row["evidence"]
        require(type(evidence) is dict and set(evidence) == EVIDENCE_KEYS
                and type(evidence["caller_services"]) is list and len(evidence["caller_services"]) <= 64
                and all(type(x) is str and len(x) <= 404 for x in evidence["caller_services"])
                and type(evidence["calls"]) is int and type(evidence["errors"]) is int
                and 0 <= evidence["errors"] <= evidence["calls"]
                and all(type(evidence[k]) is str and feeds.STAMP.fullmatch(evidence[k])
                        for k in ("first_seen", "last_seen")), "dependencies: invalid evidence")
    return rows


def read_dependency_document(path: Path) -> tuple[list[dict], list[dict]]:
    """Confirmed dependencies and rejected suggestions. A pair is never both."""
    require(path.is_file() and not path.is_symlink(), "dependencies path must be a regular file")
    document = parse_json(safe_file(path.parent, path.name, MAX_TOTAL))
    require(type(document) is dict and set(document) in (
        {"schema_version", "kind", "confirmed"}, {"schema_version", "kind", "confirmed", "rejected"}),
        "dependencies document: unexpected fields")
    require(document["schema_version"] == 1 and document["kind"] == DEPENDENCY_KIND,
            "unsupported dependencies document")
    seen: set = set()
    confirmed = _rows(document["confirmed"], seen)
    return confirmed, _rows(document.get("rejected", []), seen)


def read_calls(path: Path | None) -> list[dict] | None:
    """The calls file's rows, or None when there is none yet."""
    return feeds.read_calls(path) if path is not None and path.is_file() else None


def strength(calls: int) -> str:
    return next(name for name, least in STRENGTH if calls >= least)


def suggestion_id(source: str, target: str) -> str:
    return decisions.decision_id("depends_on", source, target)


def suggest(state: dict, calls: list[dict] | None, confirmed: list[dict], rejected: list[dict]) -> dict:
    """Deterministic proposals from observed calls; nothing here changes the graph."""
    if calls is None:
        return {"schema_version": 1, "kind": "DEPENDENCY_SUGGESTIONS", "status": "NO_OBSERVED_CALLS",
                "suggestions": [], "corroborated": [], "gaps": [], "rejected_not_shown": 0,
                "execution_authorized": False}
    labels = {n["id"]: n["label"] for n in state["nodes"]}
    declared = {(e["source"], e["target"]) for e in state["edges"] if e["relation"] in DECLARED}
    decided_yes = {(r["source"], r["target"]) for r in confirmed}
    decided_no = {(r["source"], r["target"]) for r in rejected}
    pairs: dict[tuple[str, str], dict] = {}
    gaps = []
    for row in calls:
        a, b = row["caller"], row["callee"]
        where = f"{row['caller_service']} -> {row['callee_service']}"
        if a is None or b is None:
            gaps.append({"source": where, "reason": "SERVICE_NOT_MAPPED_TO_A_COMPONENT"})
            continue
        if a not in labels or b not in labels:
            gaps.append({"source": where, "reason": "COMPONENT_NOT_IN_SYSTEM_MAP"})
            continue
        if a == b:
            continue  # two service names for one component: a call within it
        # Several service names can map to one component; their calls add up.
        pair = pairs.setdefault((a, b), {"caller_services": [], "calls": 0, "errors": 0,
                                         "first_seen": row["first_seen"], "last_seen": row["last_seen"]})
        pair["caller_services"].append(where)
        pair["calls"] += row["calls"]
        pair["errors"] += row["errors"]
        pair["first_seen"] = min(pair["first_seen"], row["first_seen"])
        pair["last_seen"] = max(pair["last_seen"], row["last_seen"])
    suggestions, corroborated, skipped = [], [], 0
    for (a, b), evidence in sorted(pairs.items()):
        evidence["caller_services"] = sorted(evidence["caller_services"])[:64]
        if (a, b) in decided_yes:
            continue
        if (a, b) in declared:
            corroborated.append({"source": a, "target": b, "source_label": labels[a], "target_label": labels[b],
                                 "calls": evidence["calls"], "last_seen": evidence["last_seen"]})
            continue
        if (a, b) in decided_no:
            skipped += 1
            continue
        level = strength(evidence["calls"])
        suggestions.append({
            "id": suggestion_id(a, b), "source": a, "target": b,
            "source_label": labels[a], "target_label": labels[b], "evidence": evidence,
            "strength": level, "status": "PROPOSED",
            "signals": [{"signal": "OBSERVED_CALLS", "strength": level,
                         "detail": f"{labels[a]} called {labels[b]} {evidence['calls']} times in traces "
                                   f"({evidence['errors']} failed), {evidence['first_seen']} to "
                                   f"{evidence['last_seen']}"}]})
    order = [name for name, _ in STRENGTH]
    suggestions.sort(key=lambda s: (order.index(s["strength"]), -s["evidence"]["calls"], s["source"], s["target"]))
    return {"schema_version": 1, "kind": "DEPENDENCY_SUGGESTIONS", "status": "OBSERVED_CALLS",
            "suggestions": suggestions[:MAX_SHOWN], "suggestions_omitted": max(0, len(suggestions) - MAX_SHOWN),
            "corroborated": corroborated[:MAX_SHOWN], "gaps": gaps[:MAX_SHOWN], "rejected_not_shown": skipped,
            "semantics": "OBSERVED_IN_TRACES_NOT_PROOF_CONFIRMATION_REQUIRED",
            "auto_applied": 0, "execution_authorized": False}


def with_confirmed(topology: dict, confirmed: list[dict], source_name: str) -> dict:
    """The topology plus a CALLS edge, with its own evidence record, per confirmed dependency."""
    ids = {n["id"] for n in topology["nodes"]}
    edges, evidence, gaps = list(topology["edges"]), list(topology["source_evidence"]), list(topology.get("gaps", []))
    for row in confirmed:
        if row["source"] not in ids or row["target"] not in ids:
            gaps.append({"source": f"{row['source']} -> {row['target']}",
                         "reason": "CONFIRMED_DEPENDENCY_COMPONENT_MISSING"})
            continue
        record = digest(canonical(row))
        edges.append({"source": row["source"], "target": row["target"], "relation": "CALLS",
                      "evidence_id": record, "state": "OBSERVED_IN_TRACES_CONFIRMED_BY_PERSON"})
        evidence.append({"id": record, "path": source_name, "sha256": record,
                         "source_kind": "PERSON_CONFIRMED_TRACE_OBSERVATION", "authenticity": "UNVERIFIED"})
    return {**topology, "edges": edges, "source_evidence": evidence, "gaps": gaps}


def dependency_key(row: dict) -> tuple[str, str, str]:
    return ("depends_on", row["source"], row["target"])


def apply_decisions(path: Path, by: str, note: str = "", confirm: list[dict] = (),
                    reject: list[dict] = (), undo: list[str] = ()) -> dict:
    """Confirm or reject proposed dependencies and undo decided ones, in one atomic write."""
    for suggestion in (*confirm, *reject):
        require(suggestion.get("status") == "PROPOSED", "only a proposed suggestion can be decided")
    row = lambda s: {"source": s["source"], "target": s["target"], "evidence": s["evidence"]}
    return decisions.apply(path, read=read_dependency_document, kind=DEPENDENCY_KIND, confirmed_field="confirmed",
                           key=dependency_key, name="dependencies", max_rows=MAX_DEPENDENCIES, by=by, note=note,
                           confirm=[row(s) for s in confirm], reject=[row(s) for s in reject], undo=list(undo))


def decide(path: Path, suggestion: dict, decision: str, by: str, note: str = "") -> dict:
    """Append one confirmed dependency or one rejection. Atomic and idempotent."""
    require(decision in ("confirm", "reject"), "decision must be confirm or reject")
    result = apply_decisions(path, by, note, **{decision: [suggestion]})
    if not result["applied"]:
        return {"status": "UNCHANGED", "decision": decision, "dependency": result["unchanged"][0]}
    return {"status": "CONFIRMED" if decision == "confirm" else "REJECTED",
            "decision": decision, "dependency": result["applied"][0]["row"], "authenticated": False}


def main() -> int:
    import control_room
    import discover_repo

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("suggest", "confirm", "reject", "undo"):
        p = commands.add_parser(name)
        p.add_argument("--repo", type=Path, required=True)
        p.add_argument("--calls", type=Path, required=True,
                       help="the calls file the feeds write beside the observations (OBS.calls.json)")
        p.add_argument("--dependencies", type=Path, required=True)
        if name != "suggest":
            p.add_argument("--id", action="append", default=[],
                           help="suggestion ID from `suggest` (or, for undo, a decided dependency's ID); repeatable")
            p.add_argument("--by", required=True, help="who decided (not verified)")
            p.add_argument("--note", default="")
        if name in ("confirm", "reject"):
            p.add_argument("--all-strong", action="store_true", help="decide every STRONG suggestion")
    args = parser.parse_args()
    try:
        confirmed, rejected = (read_dependency_document(args.dependencies)
                               if args.dependencies.exists() else ([], []))
        topology = with_confirmed(discover_repo.discover(args.repo), confirmed, args.dependencies.name)
        state = control_room.build_state(topology, [], {})
        if args.command == "undo":
            require(args.id, "pass --id for each decided dependency to undo (see `suggest`'s decided list)")
            result = apply_decisions(args.dependencies, args.by, args.note, undo=args.id)
        else:
            require(args.calls.is_file(), f"no calls file at {args.calls}; the feeds write it once traces arrive")
            result = suggest(state, read_calls(args.calls), confirmed, rejected)
            if args.command != "suggest":
                chosen = decisions.select(result["suggestions"], args.id, args.all_strong)
                result = apply_decisions(args.dependencies, args.by, args.note, **{args.command: chosen})
            else:
                result["decided"] = decisions.decided(confirmed, rejected, dependency_key)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DomainError, feeds.FeedError, control_room.RoomError, discover_repo.DiscoveryError,
            OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
