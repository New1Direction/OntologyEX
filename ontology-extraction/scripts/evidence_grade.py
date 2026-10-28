#!/usr/bin/env python3
"""Ordinal evidence grades (G0-G4) for the rules of a verified domain skill.

A grade says how far a rule's evidence links were checked, from recorded events.
It is never authored into the model, never a number, and never a permission.
Review and behavior events are recorded assertions, not authenticated approvals.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

from policy_check import (
    DomainError, MAX_ITEMS, MAX_TOTAL, canonical, digest, identifier, keys,
    parse_json, require, safe_file, verify_bundle,
)

# grade -> (label, what has to be true). The ladder is cumulative: a rule holds
# a grade only when it also meets every lower one. Unknown input stays at G0.
SCALE = (
    ("G0", "Cited", "The compiler verified the cited bytes and file digests."),
    ("G1", "Reviewed", "A recorded review says the cited spans support the current claim."),
    ("G2", "Multi-kind", "The rule is observed and cites two or more evidence kinds."),
    ("G3", "Exercised", "A recorded behavior check passed for the current claim; none failed."),
    ("G4", "Observed in use", "A fresh agent used a handoff and its work passed independent checks."),
)
EVENTS = ("REVIEW_SUPPORTS", "REVIEW_REJECTS", "BEHAVIOR_CHECK_PASSED", "BEHAVIOR_CHECK_FAILED")
EVENT_KIND = "ONTOLOGYEX_EVIDENCE_EVENTS"
# No tool in this repository observes fresh-agent use yet, so nothing can confer G4.
G4_PRODUCER = "NONE_IN_THIS_REPOSITORY"


def load_skill(skill: Path) -> tuple[dict, dict]:
    manifest = verify_bundle(skill)
    model = parse_json(safe_file(skill, "model.json", MAX_TOTAL))
    require(type(model) is dict and type(model.get("rules")) is list and
            type(model.get("evidence")) is list, "model has no rules or evidence")
    return manifest, model


def claims(model: dict) -> dict[str, dict]:
    """Bind each rule to a digest of its statement, check, and exact cited evidence.

    Any change to the rule or to a cited span changes the digest, so earlier
    events no longer apply to it.
    """
    evidence = {e["id"]: e for e in model["evidence"] if type(e) is dict and type(e.get("id")) is str}
    result = {}
    for rule in model["rules"]:
        require(type(rule) is dict and type(rule.get("evidence")) is list, "invalid rule")
        identifier(rule.get("id"), "rule.id")
        cited = []
        for ref in rule["evidence"]:
            require(ref in evidence, f"rule {rule['id']} cites missing evidence {ref}")
            e = evidence[ref]
            cited.append({"id": ref, "path": e["path"], "sha256": e["sha256"],
                          "start_line": e["start_line"], "end_line": e["end_line"],
                          "kind": e["kind"], "quote_sha256": digest(e["quote"].encode("utf-8"))})
        body = {"root_goal_id": model["root_goal_id"], "id": rule["id"], "task": rule["task"],
                "statement": rule["statement"], "status": rule["status"], "check": rule["check"],
                "evidence": cited}
        result[rule["id"]] = {"rule": rule, "claim_sha256": digest(canonical(body)), "cited": cited}
    return result


def check_event(event: object) -> None:
    keys(event, {"rule", "claim_sha256", "event", "by", "ref"}, "event")
    identifier(event["rule"], "event.rule")
    require(type(event["claim_sha256"]) is str and len(event["claim_sha256"]) == 64,
            "invalid event claim digest")
    require(event["event"] in EVENTS, "unsupported event type")
    require(type(event["by"]) is str and 0 < len(event["by"]) <= 120, "event needs a recorder name")
    require(type(event["ref"]) is str and len(event["ref"]) <= 500, "invalid event reference")
    require(not event["event"].startswith("BEHAVIOR") or event["ref"],
            "behavior events must name the check that ran")


def load_events(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    require(path.is_file() and not path.is_symlink(), "events path must be a regular file")
    document = parse_json(safe_file(path.parent, path.name, MAX_TOTAL))
    keys(document, {"schema_version", "kind", "events"}, "events document")
    require(document["schema_version"] == 1 and document["kind"] == EVENT_KIND,
            "unsupported events document")
    events = document["events"]
    require(type(events) is list and len(events) <= MAX_ITEMS * 16, "invalid events list")
    for event in events:
        check_event(event)
    return events


HELD_BY = {
    "REJECTED": "A recorded review says the cited spans do not support the claim.",
    "NOT_REVIEWED": "No recorded review for the current claim.",
    "INFERRED": "The rule is inferred, so its grade stops at G1.",
    "ONE_KIND": "The rule cites only one evidence kind.",
    "CHECK_FAILED": "A recorded behavior check failed for the current claim.",
    "NOT_EXERCISED": "No recorded behavior check for the current claim.",
    "NO_G4_PRODUCER": "G4 needs an observed fresh-agent result; no tool here records one.",
}


def ladder(rejected: bool, supported: bool, observed: bool, multi_kind: bool,
           failed: bool, passed: bool) -> tuple[str, str]:
    """The grade and why it stops there. Mirrored in Rust, Lean, and Bend."""
    if rejected:
        return "G0", "REJECTED"
    if not supported:
        return "G0", "NOT_REVIEWED"
    if not observed:
        return "G1", "INFERRED"
    if not multi_kind:
        return "G1", "ONE_KIND"
    if failed:
        return "G2", "CHECK_FAILED"
    if not passed:
        return "G2", "NOT_EXERCISED"
    return "G3", "NO_G4_PRODUCER"


def grade_rule(entry: dict, events: list[dict]) -> dict:
    rule = entry["rule"]
    kinds = sorted({c["kind"] for c in entry["cited"]})
    by = {name: [e for e in events if e["event"] == name] for name in EVENTS}
    grade, reason = ladder(bool(by["REVIEW_REJECTS"]), bool(by["REVIEW_SUPPORTS"]),
                           rule["status"] == "observed", len(kinds) >= 2,
                           bool(by["BEHAVIOR_CHECK_FAILED"]), bool(by["BEHAVIOR_CHECK_PASSED"]))
    holds = [HELD_BY[reason]]
    index = [g for g, _, _ in SCALE].index(grade)
    return {
        "id": rule["id"], "task": rule["task"], "status": rule["status"],
        "claim_sha256": entry["claim_sha256"], "grade": grade, "label": SCALE[index][1],
        "evidence_kinds": kinds, "evidence_files": len({c["path"] for c in entry["cited"]}),
        "reviews": {"supports": sorted({e["by"] for e in by["REVIEW_SUPPORTS"]}),
                    "rejects": sorted({e["by"] for e in by["REVIEW_REJECTS"]})},
        "behavior_checks": {"passed": sorted({e["ref"] for e in by["BEHAVIOR_CHECK_PASSED"]}),
                            "failed": sorted({e["ref"] for e in by["BEHAVIOR_CHECK_FAILED"]})},
        "held_by": holds,
        "next": SCALE[index + 1][2] if index + 1 < len(SCALE) else None,
    }


def grade(skill: Path, events_path: Path | None = None) -> dict:
    require(events_path is None or events_path.exists(), "events file not found")
    manifest, model = load_skill(skill)
    current = claims(model)
    events = load_events(events_path)
    applied = {rid: [] for rid in current}
    stale, unmatched = [], []
    for event in events:
        if event["rule"] not in current:
            unmatched.append(event)
        elif event["claim_sha256"] != current[event["rule"]]["claim_sha256"]:
            stale.append(event)
        else:
            applied[event["rule"]].append(event)
    rules = [grade_rule(current[rid], applied[rid]) for rid in sorted(current)]
    distribution = {g: 0 for g, _, _ in SCALE}
    for rule in rules:
        distribution[rule["grade"]] += 1
    return {
        "schema_version": 1, "kind": "EVIDENCE_GRADES",
        "skill": model["name"], "root_goal_id": model["root_goal_id"],
        "bundle_id": manifest["bundle_id"],
        "scale": [{"grade": g, "label": label, "requires": req} for g, label, req in SCALE],
        "rules": rules, "distribution": distribution,
        "not_graded": {"unknowns": len(model.get("unknowns", [])),
                       "conflicts": len(model.get("conflicts", []))},
        "stale_events": stale, "unmatched_events": unmatched,
        "g4_producer": G4_PRODUCER,
        "events_authenticated": False,
        "numeric_weights": "NONE_ORDINAL_ONLY",
        "execution_authorized": False,
    }


def record(skill: Path, events_path: Path, rule_id: str, event: str, by: str, ref: str) -> dict:
    """Append one event bound to the rule's current claim digest. Never rewrites history."""
    skill_root = skill.resolve()
    target = events_path.absolute()
    require(target.resolve() != skill_root and skill_root not in target.resolve().parents,
            "keep the events file outside the skill bundle; the bundle is hash-verified")
    _, model = load_skill(skill)
    current = claims(model)
    require(rule_id in current, "unknown rule ID")
    item = {"rule": rule_id, "claim_sha256": current[rule_id]["claim_sha256"],
            "event": event, "by": by, "ref": ref}
    check_event(item)
    before = safe_file(target.parent, target.name, MAX_TOTAL) if target.exists() else None
    existing = load_events(target)
    if item in existing:
        return {"status": "UNCHANGED", "event": item}
    data = canonical({"schema_version": 1, "kind": EVENT_KIND, "events": [*existing, item]})
    require(len(data) <= MAX_TOTAL, "events byte budget exceeded")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".events-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        now = safe_file(target.parent, target.name, MAX_TOTAL) if target.exists() else None
        require(now == before, "events file changed during write")
        temporary.replace(target)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"status": "RECORDED", "event": item, "authenticated": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("grade", help="grade every rule of a verified skill")
    p.add_argument("skill", type=Path)
    p.add_argument("--events", type=Path, help="events file kept outside the bundle")
    p = commands.add_parser("record", help="append one review or behavior event")
    p.add_argument("skill", type=Path)
    p.add_argument("--events", type=Path, required=True)
    p.add_argument("--rule", required=True)
    p.add_argument("--event", choices=EVENTS, required=True)
    p.add_argument("--by", required=True, help="who reviewed or ran the check (not verified)")
    p.add_argument("--ref", default="", help="check name, log path, or review note")
    args = parser.parse_args()
    try:
        if args.command == "grade":
            result = grade(args.skill, args.events)
        else:
            result = record(args.skill, args.events, args.rule, args.event, args.by, args.ref)
        print(canonical(result).decode(), end="")
        return 0
    except (DomainError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
