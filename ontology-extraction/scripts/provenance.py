#!/usr/bin/env python3
"""Track evidence origins without voting, confidence scores, or semantic inference.

Optional 70-provenance.json attributions are declarations, not authenticated facts.
Exact content matches are not proof of common origin or statistical independence.
All original evidence records and recorded conflicts are retained.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

from policy_check import (DomainError, MAX_ITEMS, MAX_TOTAL, SHA, canonical, digest,
                          entries, identifier, keys, parse_json, relative, require,
                          safe_file, text)

SIDECAR = "70-provenance.json"
NOTICE = ("Paths and repeated quotations are not independent corroboration. Origins and "
          "independence groups are declared, not verified. Unknown lineage stays unknown. "
          "No confidence score, source truth, semantic contradiction detection, or execution authorization.")
ATTR_KEYS = {"evidence_id", "evidence_sha256", "origin_id", "independence_group", "basis"}
EVIDENCE_KEYS = {"id", "path", "sha256", "start_line", "end_line", "quote", "kind"}
KINDS = {"requirement", "implementation", "test", "documentation"}


def source_id(path: str, sha256: str) -> str:
    """Stable identity for a selected file version, NOT an independent origin."""
    relative(path)
    require(type(sha256) is str and SHA.fullmatch(sha256) is not None, "invalid source digest")
    return "source-" + digest(canonical({"path": path, "sha256": sha256}))


def evidence_index(model: dict) -> dict:
    require(type(model) is dict and type(model.get("source_files")) is dict, "source inventory required")
    require(0 < len(model["source_files"]) <= 64, "invalid source inventory budget")
    for path, record in model["source_files"].items():
        require(type(record) is dict, "invalid source inventory record")
        source_id(path, record.get("sha256"))
    result = {}
    for item in entries(model.get("evidence"), "evidence"):
        keys(item, EVIDENCE_KEYS, "evidence")
        identifier(item["id"], "evidence.id")
        require(item["id"] not in result, "duplicate evidence ID")
        relative(item["path"])
        require(item["path"] in model["source_files"] and
                item["sha256"] == model["source_files"][item["path"]]["sha256"],
                "evidence source identity mismatch")
        require(type(item["kind"]) is str and item["kind"] in KINDS, "invalid evidence kind")
        require(type(item["quote"]) is str and len(item["quote"].encode("utf-8")) <= MAX_TOTAL,
                "invalid evidence quote")
        require(type(item["start_line"]) is int and type(item["end_line"]) is int and
                1 <= item["start_line"] <= item["end_line"] and
                item["end_line"] - item["start_line"] < 80, "invalid evidence span")
        result[item["id"]] = item
    return result


def validate_attributions(document: dict | None, evidence: dict) -> dict:
    if document is None:
        return {}
    keys(document, {"schema_version", "attributions"}, SIDECAR)
    require(type(document["schema_version"]) is int and document["schema_version"] == 1,
            "unsupported provenance schema version")
    result, origin_groups = {}, {}
    for item in entries(document["attributions"], "attributions"):
        keys(item, ATTR_KEYS, "attribution")
        for field in ("evidence_id", "origin_id", "independence_group"):
            identifier(item[field], "attribution." + field)
        eid = item["evidence_id"]
        require(eid in evidence, f"attribution refers to unknown evidence: {eid}")
        require(eid not in result, f"duplicate attribution: {eid}")
        require(item["evidence_sha256"] == digest(canonical(evidence[eid])),
                f"stale evidence attribution: {eid}; review the new evidence before attributing it")
        text(item["basis"], "attribution.basis")
        origin, group = item["origin_id"], item["independence_group"]
        require(origin not in origin_groups or origin_groups[origin] == group,
                f"origin {origin} assigned to inconsistent independence groups")
        origin_groups[origin] = group
        result[eid] = dict(item)
    return result


def summarize(rows: list[dict]) -> dict:
    return {"evidence_records": len(rows),
            "selected_file_versions": len({r["source_id"] for r in rows}),
            "content_groups": len({r["content_id"] for r in rows}),
            "evidence_atoms": len({r["atom_id"] for r in rows}),
            "declared_origin_ids": sorted({r["origin_id"] for r in rows if r["origin_id"] is not None}),
            "declared_independence_groups": sorted({r["independence_group"] for r in rows if r["independence_group"] is not None}),
            "unattributed_evidence_ids": sorted(r["evidence_id"] for r in rows if r["origin_id"] is None),
            "independent_support_count": None,
            "independence_verified": False}


def analyze(model: dict, document: dict | None = None) -> dict:
    """Group representations, not beliefs. Keep independent-origin sensitivity.

    Declared atoms use (origin, kind, exact quote). Unattributed matches use
    (whole-file hash, kind, exact quote), explicitly labeled byte equivalence only.
    The same words from distinct declared origins remain distinct atoms. Different
    snippets from one origin remain distinct atoms but share one declared origin.
    """
    evidence = evidence_index(model)
    attrs = validate_attributions(document, evidence)
    rows, atom_groups, content_groups = [], {}, {}
    for eid, item in sorted(evidence.items()):
        attr = attrs.get(eid)
        content = {"kind": item["kind"], "quote_sha256": digest(item["quote"].encode("utf-8"))}
        content_id = "content-" + digest(canonical(content))
        origin = {"declared_origin": attr["origin_id"]} if attr else {"unattributed_file_sha256": item["sha256"]}
        atom_id = "atom-" + digest(canonical({**content, **origin}))
        row = {"evidence_id": eid, "source_id": source_id(item["path"], item["sha256"]),
               "path": item["path"], "source_sha256": item["sha256"], "kind": item["kind"],
               "start_line": item["start_line"], "end_line": item["end_line"],
               "content_id": content_id, "atom_id": atom_id,
               "origin_id": attr["origin_id"] if attr else None,
               "independence_group": attr["independence_group"] if attr else None,
               "attribution_status": "DECLARED_NOT_VERIFIED" if attr else "UNKNOWN",
               "basis": attr["basis"] if attr else None}
        rows.append(row)
        atom = atom_groups.setdefault(atom_id, {"atom_id": atom_id, "content_id": content_id,
            "kind": item["kind"], "origin_id": row["origin_id"],
            "independence_group": row["independence_group"],
            "grouping_basis": "DECLARED_ORIGIN_AND_EXACT_QUOTE" if attr else "IDENTICAL_FILE_BYTES_AND_QUOTE_ONLY",
            "evidence_ids": [], "source_ids": []})
        atom["evidence_ids"].append(eid)
        atom["source_ids"].append(row["source_id"])
        content_groups.setdefault(content_id, []).append(row)
    by_id = {r["evidence_id"]: r for r in rows}
    items = []
    for kind in ("rules", "unknowns", "conflicts"):
        for item in entries(model.get(kind), kind):
            require(type(item) is dict, f"{kind}: invalid item")
            identifier(item.get("id"), f"{kind}.id")
            identifier(item.get("task"), f"{kind}.task")
            refs = entries(item.get("evidence"), f"{kind}.evidence")
            require(all(type(r) is str and r in evidence for r in refs), "unknown item evidence reference")
            selected = [by_id[r] for r in sorted(set(refs))]
            counts = summarize(selected)
            state = ("CONFLICT_RECORDED" if kind == "conflicts" else "UNKNOWN_RECORDED" if kind == "unknowns" else
                     "LINEAGE_UNKNOWN" if counts["unattributed_evidence_ids"] or not selected else "DECLARED_LINEAGE_ONLY")
            items.append({"id": item["id"], "kind": kind, "task": item["task"], "status": state,
                          "evidence_ids": sorted(set(refs)), "repeated_reference_count": len(refs) - len(set(refs)),
                          "atom_ids": sorted({r["atom_id"] for r in selected}), **counts})
    warnings = []
    for content_id, members in sorted(content_groups.items()):
        if len(members) > 1:
            warnings.append({"code": "EXACT_WORDING_REPEATED_NOT_CORROBORATION", "content_id": content_id,
                             "evidence_ids": [r["evidence_id"] for r in members]})
        if len({r["origin_id"] for r in members if r["origin_id"] is not None}) > 1:
            warnings.append({"code": "SAME_WORDING_DISTINCT_DECLARED_ORIGINS_RETAINED", "content_id": content_id,
                             "evidence_ids": [r["evidence_id"] for r in members]})
    for atom in atom_groups.values():
        atom["source_ids"] = sorted(set(atom["source_ids"]))
    result = {"schema_version": 1, "notice": NOTICE, "summary": summarize(rows),
              "records": rows, "atoms": [atom_groups[k] for k in sorted(atom_groups)],
              "items": sorted(items, key=lambda item: (item["kind"], item["id"])),
              "warnings": warnings,
              "attributions": [attrs[k] for k in sorted(attrs)],
              "conflict_detection": "RECORDED_CONFLICTS_ONLY_NOT_SEMANTIC_DETECTION",
              "decision_policy": "NO_VOTING_NO_CONFIDENCE_NO_POLICY_CHANGE",
              "execution_authorized": False}
    require(len(canonical(result)) <= MAX_TOTAL, "provenance report exceeds byte budget")
    return result


def task_view(report: dict, task: str, evidence_ids: set[str]) -> dict:
    """Return only the task's provenance, keeping every relevant representation."""
    rows = [r for r in report["records"] if r["evidence_id"] in evidence_ids]
    atoms = []
    for atom in report["atoms"]:
        members = [r for r in rows if r["atom_id"] == atom["atom_id"]]
        if members:
            atoms.append({**atom, "evidence_ids": [r["evidence_id"] for r in members],
                          "source_ids": sorted({r["source_id"] for r in members})})
    return {"schema_version": 1, "notice": NOTICE, "summary": summarize(rows), "records": rows, "atoms": atoms,
            "items": [item for item in report["items"] if item["task"] == task],
            "execution_authorized": False}


def read_attributions(workspace: Path) -> dict | None:
    path = workspace / SIDECAR
    # Do not treat a broken symlink or an unreadable present file as an absent sidecar.
    if not (path.exists() or path.is_symlink()):
        return None
    document = parse_json(safe_file(workspace, SIDECAR, MAX_TOTAL))
    require(type(document) is dict, f"{SIDECAR}: expected an object, not a null/array/scalar")
    return document


def workspace_evidence(workspace: Path) -> tuple[dict, dict, bytes]:
    # Local imports avoid a cycle: the compiler also consumes this module.
    import domain_skill as ds
    inventory, sources = ds.load_sources(workspace)
    before = safe_file(workspace, "60-contract.json", MAX_TOTAL)
    contract = parse_json(before)
    require(type(contract) is dict, "contract must be an object")
    index = evidence_index({**contract, "source_files": inventory})
    for item in index.values():
        lines = sources[item["path"]].decode("utf-8").splitlines()
        require(item["end_line"] <= len(lines) and
                item["quote"] == "\n".join(lines[item["start_line"] - 1:item["end_line"]]),
                "evidence no longer matches its source snapshot")
    return contract, index, before


def attribute(workspace: Path, evidence_id: str, origin_id: str, group: str, basis: str) -> dict:
    """Add a hash-bound declaration. Never infer lineage or replace an attribution.

    Serializes with evidence.py's cooperative lock. Not protection from malicious
    concurrent writers, an authentication boundary, or a semantic review.
    """
    identifier(evidence_id, "evidence_id")
    identifier(origin_id, "origin_id")
    identifier(group, "independence_group")
    text(basis, "basis")
    lock = workspace / ".evidence-lock"
    require(not lock.is_symlink(), "evidence lock is a symlink")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise DomainError("evidence writer already active; inspect .evidence-lock before recovering") from exc
    temporary = None
    try:
        _, index, contract_before = workspace_evidence(workspace)
        require(evidence_id in index, "unknown evidence ID; capture evidence first")
        prior = read_attributions(workspace)
        attrs = validate_attributions(prior, index)
        item = {"evidence_id": evidence_id, "evidence_sha256": digest(canonical(index[evidence_id])),
                "origin_id": origin_id, "independence_group": group, "basis": basis}
        if evidence_id in attrs:
            require(attrs[evidence_id] == item, "attribution already differs; explicitly review/edit the authoring sidecar or use a new workspace")
            return {"status": "UNCHANGED", "attribution": item, "independence_verified": False}
        document = {"schema_version": 1, "attributions": [*attrs.values(), item]}
        validate_attributions(document, index)
        document["attributions"].sort(key=lambda row: row["evidence_id"])
        data = canonical(document)
        require(len(data) <= MAX_TOTAL, "attribution byte budget exceeded")
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".provenance-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        require(safe_file(workspace, "60-contract.json", MAX_TOTAL) == contract_before, "contract changed during attribution")
        require(read_attributions(workspace) == prior, "attributions changed during write")
        if prior is None:
            os.link(temporary, workspace / SIDECAR)  # exclusive create
        else:
            temporary.replace(workspace / SIDECAR)
        return {"status": "ATTRIBUTION_RECORDED_NOT_VERIFIED", "attribution": item, "independence_verified": False}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("attribute", help="record an explicit origin/group declaration for existing evidence")
    p.add_argument("workspace", type=Path)
    p.add_argument("--evidence", required=True)
    p.add_argument("--origin", required=True)
    p.add_argument("--group", required=True)
    p.add_argument("--basis", required=True)
    p = sub.add_parser("inspect", help="validate and summarize a workspace without building or executing it")
    p.add_argument("workspace", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "attribute":
            result = attribute(args.workspace, args.evidence, args.origin, args.group, args.basis)
        else:
            import domain_skill as ds
            result = ds.load_model(args.workspace)[0]["provenance"]
        print(canonical(result).decode(), end="")
        return 0
    except (DomainError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc), "execution_authorized": False}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
