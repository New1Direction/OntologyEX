"""A bounded, source-backed first look at an unmodeled repository.

This projects the existing discovery graph; it does not infer business rules,
running services, customers, or revenue. Source inspection is limited to a
discovered node's recorded file and line, and never accepts a path from a caller.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

import rule_suggest
from policy_check import DomainError, safe_file

MAX_PER_GROUP = 12
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_LINE_CHARS = 500
GROUPS = (
    ("systems", "Apps and services", {"source_manifest", "declared_service"}, None),
    ("routes", "Routes and operations", {"api_route"}, "code"),
    ("data", "Data structures", {"declared_db_table"}, "code"),
    ("providers", "Outside providers", {"external_supplier"}, "suppliers"),
)
KINDS = frozenset(kind for _, _, kinds, _ in GROUPS for kind in kinds)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SENSITIVE_LINE = re.compile(
    r"secret|password|passwd|passphrase|credentials?|private[_-]?key|api[_-]?key|access[_-]?key|"
    r"token|\bkey\s*[:=]|authorization\s*[:=]|"
    r"(?:sk_(?:live|test|proj)_|gh[pousr]_|github_pat_|AKIA|ASIA)[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)


class FirstLookError(ValueError):
    pass


def project(state: dict, scans: dict[str, dict]) -> dict:
    """Summarize only discovered declarations and say which scans are incomplete."""
    evidence = {row["id"]: row for row in state.get("source_evidence", [])}
    groups = []
    for key, label, kinds, scan in GROUPS:
        rows = []
        for node in state["nodes"]:
            if node.get("kind") not in kinds:
                continue
            ev = evidence.get(node.get("evidence_id")) or {}
            rows.append({"id": node["id"], "kind": node["kind"], "label": node["label"],
                         "source": {"path": ev.get("path"), "line": node.get("source_line"),
                                    "sha256": ev.get("sha256")},
                         "status": node.get("status", "unknown")})
        rows.sort(key=lambda row: (row["label"].casefold(), row["kind"], row["id"]))
        scan_info = scans.get(scan, {}) if scan else {}
        groups.append({"id": key, "label": label, "scan_status": scan_info.get("status", "PENDING")
                       if scan else "READY", "scan_error": scan_info.get("error") if scan else None,
                       "total": len(rows), "items": rows[:MAX_PER_GROUP],
                       "omitted": max(0, len(rows) - MAX_PER_GROUP)})
    coverage = state.get("coverage", {})
    project_node = next((n for n in state["nodes"] if n.get("id") == state.get("project")), {})
    return {"schema_version": 1, "kind": "REPOSITORY_FIRST_LOOK", "applicable": True,
            "project": project_node.get("label", "Repository"), "groups": groups,
            "scans": scans, "files_seen": coverage.get("files_seen"),
            "manifests_read": coverage.get("files_with_evidence"),
            "observed_nodes": coverage.get("observed_nodes", 0),
            "gaps": state.get("gaps", [])[:12], "total_gaps": len(state.get("gaps", [])),
            "unresolved_count": len(state.get("unresolved", [])),
            "discovery_freshness": state.get("discovery_freshness", "AS_OF_LATEST_SUCCESSFUL_SCAN"),
            "semantics": "SOURCE_CANDIDATES_NOT_RUNTIME_OR_BUSINESS_FACTS", "execution_authorized": False}


def source(repo: Path | None, state: dict, node_id: str, expected_sha: str) -> dict:
    """Show one recorded line only if its file is still the same and not sensitive."""
    if type(node_id) is not str or not 0 < len(node_id) <= 1024:
        raise FirstLookError("invalid node ID")
    if type(expected_sha) is not str or SHA256.fullmatch(expected_sha) is None:
        raise FirstLookError("invalid source hash")
    node = next((n for n in state["nodes"] if n.get("id") == node_id and n.get("kind") in KINDS), None)
    if node is None:
        raise FirstLookError("unknown preview item")
    evidence = next((e for e in state.get("source_evidence", []) if e.get("id") == node.get("evidence_id")), None)
    if not evidence or type(evidence.get("path")) is not str:
        return {"status": "NO_SOURCE_LOCATION"}
    path, line = evidence["path"], node.get("source_line")
    result = {"path": path, "line": line, "sha256": evidence.get("sha256")}
    if expected_sha != evidence.get("sha256"):
        return {**result, "status": "SOURCE_CHANGED"}
    if any(rule_suggest.secret(part.lower()) for part in path.split("/")):
        return {**result, "status": "WITHHELD_SECRET_FILE"}
    if repo is None:
        return {**result, "status": "LOCAL_SOURCE_UNAVAILABLE"}
    if type(line) is not int or line < 1:
        return {**result, "status": "LINE_NOT_RECORDED"}
    try:
        content = safe_file(repo, path, MAX_SOURCE_BYTES)
    except (DomainError, OSError, ValueError):
        return {**result, "status": "SOURCE_UNAVAILABLE"}
    if hashlib.sha256(content).hexdigest() != evidence.get("sha256"):
        return {**result, "status": "SOURCE_CHANGED"}
    lines = content.decode("utf-8", "replace").splitlines()
    if line > len(lines):
        return {**result, "status": "LINE_UNAVAILABLE"}
    text = lines[line - 1]
    if SENSITIVE_LINE.search(text):
        return {**result, "status": "WITHHELD_SENSITIVE_LINE"}
    return {**result, "status": "SOURCE_LINE", "text": text[:MAX_LINE_CHARS],
            "truncated": len(text) > MAX_LINE_CHARS}
