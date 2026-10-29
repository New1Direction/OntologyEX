#!/usr/bin/env python3
"""Business rules agents propose, each citing the lines it rests on; a person decides.

    python rule_suggest.py list --rules RULES.json --proposals PROPOSALS.jsonl --repo REPO
    python rule_suggest.py confirm --rules RULES.json --proposals PROPOSALS.jsonl --repo REPO --id ID --by NAME

A domain skill's rules are compiled and integrity-checked; nothing here edits a skill.
An agent's rule (agent_tools.py propose_rule) names a business object or action, a
statement ("a refund never exceeds the captured amount"), the file and lines it rests
on, and a reason. The control room checks that the cited lines exist and shows them;
whether they support the statement is for the person reading them. A confirmed rule
is recorded in a rules file with who decided, and the hash of the cited lines: when
those lines change, the rule reads as needing re-confirmation, never as current. A
rejected rule is not proposed again.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

import decisions
import entity_context
from policy_check import DomainError, MAX_TOTAL, parse_json, require, safe_file
import watermark

RULE_KIND = "ONTOLOGYEX_RULE_DECISIONS"
PROPOSAL_KIND = "ONTOLOGYEX_AGENT_RULE_PROPOSAL"
MAX_RULES = 1024
MAX_PROPOSALS = 2000
MAX_PROPOSAL_BYTES = 4 * 1024 * 1024
MAX_CITED_LINES = 80
EVIDENCE_KEYS = {"proposed_by", "on_behalf_of", "reason", "proposed_at"}
SHA256 = re.compile(r"[0-9a-f]{64}")
# Files that commonly hold secrets: never quoted, whoever asks.
SECRET_FILES = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                "*credentials*", "*secret*", ".npmrc", ".pypirc", ".netrc", "*.kdbx")


def secret(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g) for g in SECRET_FILES)


def rule_id(target: str, statement: str) -> str:
    return decisions.decision_id("rule", target, " ".join(statement.split()).lower())


def rule_key(row: dict) -> tuple[str, str, str]:
    return ("rule", row["target"], " ".join(row["statement"].split()).lower())


def _text(value, n: int) -> bool:
    return type(value) is str and 0 < len(value) <= n and all(" " <= ch != "\x7f" for ch in value)


def valid_citation(citation) -> bool:
    return (type(citation) is dict and set(citation) == {"path", "start_line", "end_line"}
            and _text(citation["path"], 1024) and not citation["path"].startswith("/") and ".." not in citation["path"].split("/")
            and all(type(citation[k]) is int and citation[k] >= 1 for k in ("start_line", "end_line"))
            and citation["start_line"] <= citation["end_line"] <= citation["start_line"] + MAX_CITED_LINES)


def valid_evidence(evidence) -> bool:
    return (type(evidence) is dict and set(evidence) == EVIDENCE_KEYS and evidence["proposed_by"].startswith("agent:")
            and _text(evidence["proposed_by"], 128) and _text(evidence["on_behalf_of"], 128)
            and _text(evidence["reason"], 500) and _text(evidence["proposed_at"], 40))


def valid_proposal(row) -> bool:
    fields = {"kind", "id", "at", "proposed_by", "on_behalf_of", "target", "statement", "citation", "reason"}
    return (type(row) is dict and set(row) == fields and row["kind"] == PROPOSAL_KIND
            and _text(row["target"], 1024) and _text(row["statement"], 500) and len(row["statement"].strip()) >= 8
            and valid_citation(row["citation"]) and row["id"] == rule_id(row["target"], row["statement"])
            and valid_evidence({"proposed_by": row["proposed_by"], "on_behalf_of": row["on_behalf_of"],
                                "reason": row["reason"], "proposed_at": row["at"]}))


def read_proposals(path: Path | None) -> list[dict]:
    """Agent rule proposals, oldest first, from the proposals file links and dependencies share."""
    return [row for row in decisions.read_jsonl_tail(path, MAX_PROPOSAL_BYTES, MAX_PROPOSALS) if valid_proposal(row)]


def append_proposal(path: Path, row: dict) -> None:
    require(valid_proposal(row), "invalid agent rule proposal")
    decisions.append_jsonl(path, row)


def _rows(rows: object, seen: set) -> list[dict]:
    require(type(rows) is list and len(rows) <= MAX_RULES, "rules: too many rows")
    for row in rows:
        require(type(row) is dict and set(row) in (
            {"target", "statement", "citation", "evidence", "declared_by", "note"},
            {"target", "statement", "citation", "evidence", "declared_by", "note", "lines_sha256"}),
            "rules: unexpected row fields")
        require(_text(row["target"], 1024) and _text(row["statement"], 500) and valid_citation(row["citation"])
                and valid_evidence(row["evidence"]), "rules: invalid rule")
        require(row.get("lines_sha256") is None or (type(row["lines_sha256"]) is str
                                                    and SHA256.fullmatch(row["lines_sha256"])),
                "rules: invalid cited-lines hash")
        require(_text(row["declared_by"], 120) and type(row["note"]) is str and len(row["note"]) <= 500,
                "rules: invalid decider or note")
        require(rule_key(row) not in seen, "rules: a rule is decided twice")
        seen.add(rule_key(row))
    return rows


def read_rule_document(path: Path) -> tuple[list[dict], list[dict]]:
    """Confirmed rules and rejected ones. A rule is never both."""
    require(path.is_file() and not path.is_symlink(), "rules path must be a regular file")
    document = parse_json(safe_file(path.parent, path.name, MAX_TOTAL))
    require(type(document) is dict and set(document) in (
        {"schema_version", "kind", "confirmed"}, {"schema_version", "kind", "confirmed", "rejected"}),
        "rules document: unexpected fields")
    require(document["schema_version"] == 1 and document["kind"] == RULE_KIND, "unsupported rules document")
    seen: set = set()
    return _rows(document["confirmed"], seen), _rows(document.get("rejected", []), seen)


def check_citation(files: watermark.RepoFiles, citation: dict) -> dict:
    """Whether the cited lines exist in the repository now, their text, and their hash.

    LINES_FOUND says the lines are there, not that they support the rule: a person reads them.
    """
    if secret(citation["path"]):
        return {"status": "WITHHELD_SECRET_FILE"}
    rel = files.find(citation["path"])
    if rel is None:
        return {"status": "FILE_NOT_FOUND"}
    try:
        lines = safe_file(files.repo, rel, watermark.MAX_BYTES).decode("utf-8", "replace").splitlines()
    except (DomainError, OSError, ValueError):
        return {"status": "FILE_NOT_FOUND"}
    if citation["end_line"] > len(lines):
        return {"status": "LINES_OUT_OF_RANGE", "path": rel, "file_lines": len(lines)}
    text = "\n".join(lines[citation["start_line"] - 1:citation["end_line"]])
    return {"status": "LINES_FOUND", "path": rel, "lines_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "quote": text[:1200], "quote_truncated": len(text) > 1200}


def suggest(proposals: list[dict], confirmed: list[dict], rejected: list[dict], entity_ids: set[str],
            files: watermark.RepoFiles) -> dict:
    """Open rule proposals, merged by rule (several agents may propose one), with their citations checked."""
    decided = {rule_key(r) for r in (*confirmed, *rejected)}
    rows: dict[str, dict] = {}
    hidden = 0
    for proposal in proposals:
        key = rule_key(proposal)
        if key in decided or proposal["target"] not in entity_ids:
            hidden += 1
            continue
        row = rows.get(proposal["id"])
        if row is None:
            row = rows[proposal["id"]] = {
                "id": proposal["id"], "target": proposal["target"], "statement": proposal["statement"],
                "citation": proposal["citation"], "citation_check": check_citation(files, proposal["citation"]),
                "evidence": {"proposed_by": proposal["proposed_by"], "on_behalf_of": proposal["on_behalf_of"],
                             "reason": proposal["reason"], "proposed_at": proposal["at"]},
                "proposed_by": proposal["proposed_by"], "strength": "WEAK", "status": "PROPOSED", "signals": []}
        if len(row["signals"]) < 3:
            row["signals"].append({"signal": "AGENT_PROPOSAL", "strength": "WEAK",
                                   "detail": f"{proposal['proposed_by']} for {proposal['on_behalf_of']}: {proposal['reason']}"})
    return {"schema_version": 1, "kind": "RULE_SUGGESTIONS", "suggestions": list(rows.values())[:200],
            "not_shown": hidden, "semantics": "PROPOSED_BY_AGENTS_A_PERSON_READS_THE_CITED_LINES_AND_DECIDES",
            "auto_applied": 0, "execution_authorized": False}


def confirmed_view(confirmed: list[dict], files: watermark.RepoFiles) -> list[dict]:
    """Confirmed rules as the business picture shows them, each with its cited lines checked again."""
    out = []
    for row in confirmed:
        now = check_citation(files, row["citation"])
        recorded = {row["citation"]["path"]: row["lines_sha256"]} if row.get("lines_sha256") else None
        current = {row["citation"]["path"]: now["lines_sha256"]} if now.get("lines_sha256") else {}
        check = entity_context.evidence_check(recorded, current)
        out.append({"id": decisions.decision_id(*rule_key(row)), "target": row["target"], "statement": row["statement"],
                    "status": "CONFIRMED_BEFORE_EVIDENCE_CHANGED" if check["status"] == "CHANGED_SINCE_DECISION"
                    else "CONFIRMED_BY_PERSON",
                    "citation": row["citation"], "quote": now.get("quote"), "evidence_check": check["status"],
                    "declared_by": row["declared_by"], "note": row["note"],
                    "proposed_by": row["evidence"]["proposed_by"]})
    return out


def apply_decisions(path: Path, by: str, note: str = "", confirm: list[dict] = (), reject: list[dict] = (),
                    undo: list[str] = (), reconfirm: list[dict] = ()) -> dict:
    """Confirm or reject proposed rules, undo or re-confirm decided ones, in one atomic write."""
    for suggestion in (*confirm, *reject):
        require(suggestion.get("status") == "PROPOSED", "only a proposed rule can be decided")

    def row(s: dict, record: bool) -> dict:
        lines = (s.get("citation_check") or {}).get("lines_sha256")
        return {"target": s["target"], "statement": s["statement"], "citation": s["citation"],
                "evidence": s["evidence"], **({"lines_sha256": lines} if record and lines else {})}
    return decisions.apply(path, read=read_rule_document, kind=RULE_KIND, confirmed_field="confirmed",
                           key=rule_key, name="rules", max_rows=MAX_RULES, by=by, note=note,
                           confirm=[row(s, True) for s in confirm], reject=[row(s, False) for s in reject],
                           undo=list(undo), reconfirm=list(reconfirm))


def reconfirm_rows(confirmed: list[dict], ids: list[str], files: watermark.RepoFiles) -> list[dict]:
    """Confirmed rules to confirm again, with the hash of their cited lines as they are now."""
    by_id = {decisions.decision_id(*rule_key(r)): r for r in confirmed}
    require(ids and all(i in by_id for i in ids), "no confirmed rule has that ID")
    out = []
    for i in dict.fromkeys(ids):
        check = check_citation(files, by_id[i]["citation"])
        require(check["status"] == "LINES_FOUND", f"the cited lines of {i} cannot be read now: {check['status']}")
        out.append({k: v for k, v in by_id[i].items() if k not in ("declared_by", "note")}
                   | {"lines_sha256": check["lines_sha256"]})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "confirm", "reject", "undo", "reconfirm"):
        p = commands.add_parser(name)
        p.add_argument("--rules", type=Path, required=True)
        p.add_argument("--proposals", type=Path)
        p.add_argument("--repo", type=Path, required=True)
        if name != "list":
            p.add_argument("--id", action="append", default=[], required=True)
            p.add_argument("--by", required=True, help="who decided (not verified)")
            p.add_argument("--note", default="")
    args = parser.parse_args()
    try:
        files = watermark.RepoFiles(args.repo)
        confirmed, rejected = read_rule_document(args.rules) if args.rules.exists() else ([], [])
        proposals = read_proposals(args.proposals)
        open_rules = suggest(proposals, confirmed, rejected, {p["target"] for p in proposals}, files)
        if args.command == "list":
            result = {**open_rules, "confirmed": confirmed_view(confirmed, files)}
        elif args.command in ("undo", "reconfirm"):
            result = (apply_decisions(args.rules, args.by, args.note, undo=args.id) if args.command == "undo" else
                      apply_decisions(args.rules, args.by, args.note,
                                      reconfirm=reconfirm_rows(confirmed, args.id, files)))
        else:
            chosen = decisions.select(open_rules["suggestions"], args.id, False)
            result = apply_decisions(args.rules, args.by, args.note, **{args.command: chosen})
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DomainError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)[:400]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
