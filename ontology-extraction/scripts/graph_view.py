#!/usr/bin/env python3
"""Read-only typed graph projection of verified OntologyEX domain-skill bundles.

This projects authored declarations, NOT runtime dependencies or entailed facts.
No approvals, authorizations, model calls, or network requests are performed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from policy_check import (
    DomainError, ID, MAX_TOTAL, parse_json, require, safe_file, verify_bundle,
)

SPECS = (
    ("10-upper.yaml", "anchors", "upper_anchor"),
    ("20-domain.yaml", "classes", "domain_class"),
    ("20-domain.yaml", "interfaces", "interface"),
    ("20-domain.yaml", "relations", "relation"),
    ("30-task.yaml", "tasks", "task"),
    ("40-application.yaml", "concepts", "application_binding"),
)
MAX_SCOPES = 16
MAX_NODES = 4096
MAX_EDGES = 16384


def stable_id(scope: str, kind: str, local_id: str) -> str:
    """Stable across versions of the same scoped root goal, not across goals."""
    require(all(type(x) is str and ID.fullmatch(x) for x in (scope, kind, local_id)),
            "invalid graph identity")
    return f"{scope}:{kind}:{local_id}"


def _entries(model: dict, layer: str, collection: str) -> list[dict]:
    layers = model.get("layers")
    require(type(layers) is dict, "model layers must be an object")
    document = layers.get(layer)
    require(type(document) is dict, f"missing layer: {layer}")
    items = document.get(collection, [])
    require(type(items) is list and len(items) <= 256, f"invalid collection: {layer}/{collection}")
    for item in items:
        require(type(item) is dict and type(item.get("id")) is str and ID.fullmatch(item["id"]),
                f"invalid node in {layer}/{collection}")
    return items


def from_model(model: dict, bundle_id: str) -> dict:
    """Project one already-verified bundle model into explicitly typed nodes/edges.

    Unknown references are returned, never silently fabricated into edges.
    This function is deterministic and does not modify its input.
    """
    require(type(model) is dict, "model must be an object")
    scope = model.get("root_goal_id")
    name = model.get("name")
    require(type(scope) is str and ID.fullmatch(scope), "invalid root goal id")
    require(type(name) is str and 0 < len(name) <= 64, "invalid skill name")
    require(type(bundle_id) is str and len(bundle_id) == 64 and
            all(c in "0123456789abcdef" for c in bundle_id), "invalid bundle ID")

    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    unresolved: dict[tuple[str, str, str], dict] = {}
    lookup: dict[tuple[str, str], str] = {}
    declarations: list[tuple[str, dict]] = []

    def node(kind: str, item: dict, extra: dict | None = None) -> str:
        local_id = item["id"]
        uid = stable_id(scope, kind, local_id)
        require(uid not in nodes, f"duplicate {kind} node: {local_id}")
        entry = {"id": uid, "kind": kind, "local_id": local_id}
        if extra:
            entry.update(extra)
        nodes[uid] = entry
        lookup[kind, local_id] = uid
        return uid

    evidence = model.get("evidence")
    require(type(evidence) is list and len(evidence) <= 256, "invalid evidence records")
    for item in evidence:
        require(type(item) is dict and type(item.get("id")) is str and ID.fullmatch(item["id"]),
                "invalid evidence ID")
        require(type(item.get("path")) is str and type(item.get("sha256")) is str and
                type(item.get("start_line")) is int and type(item.get("end_line")) is int,
                "invalid evidence location")
        node("evidence", item, {
            "path": item["path"], "sha256": item["sha256"],
            "start_line": item["start_line"], "end_line": item["end_line"],
            "evidence_kind": item.get("kind", "unknown"),
        })

    for layer, collection, kind in SPECS:
        for item in _entries(model, layer, collection):
            attributes = {}
            if kind == "upper_anchor":
                attributes["iri"] = item.get("iri", "")
            node(kind, item, attributes)
            declarations.append((kind, item))

    def link(from_id: str, relation: str, target_kind: str, target_id: str) -> None:
        require(type(target_id) is str and ID.fullmatch(target_id),
                f"invalid reference {relation}")
        target = lookup.get((target_kind, target_id))
        if target is None:
            key = (from_id, relation, stable_id(scope, target_kind, target_id))
            unresolved[key] = {
                "source": from_id, "relation": relation,
                "missing_target": key[2], "status": "UNRESOLVED_REFERENCE",
            }
            return
        edge_id = f"{from_id}|{relation}|{target}"
        edges[edge_id] = {
            "id": edge_id, "source": from_id, "relation": relation, "target": target,
        }

    def refs(from_id: str, relation: str, target_kind: str, items: object) -> None:
        require(type(items) is list and len(items) <= 256, "invalid reference list")
        for local in items:
            link(from_id, relation, target_kind, local)

    for kind, item in declarations:
        uid = lookup[kind, item["id"]]
        source = item.get("source")
        if source is not None:
            link(uid, "SUPPORTED_BY", "evidence", source)
        if kind == "domain_class":
            link(uid, "ANCHORED_TO", "upper_anchor", item["upper"])
            refs(uid, "IMPLEMENTS", "interface", item.get("implements", []))
        elif kind == "interface":
            refs(uid, "APPLIES_TO", "domain_class", item.get("applies_to", []))
        elif kind == "relation":
            link(uid, "DOMAIN", "domain_class", item["domain"])
            link(uid, "RANGE", "domain_class", item["range"])
        elif kind == "task":
            refs(uid, "CONSUMES", "domain_class", item.get("inputs", []))
            refs(uid, "PRODUCES", "domain_class", item.get("outputs", []))
            refs(uid, "ACTOR_ROLE", "domain_class", item.get("actor_roles", []))
            refs(uid, "DECOMPOSES_TO", "task", item.get("decomposes_to", []))
        elif kind == "application_binding":
            link(uid, "BINDS_TO", "domain_class", item["binds"])
            refs(uid, "USED_BY_TASK", "task", item.get("used_by_tasks", []))

    require(len(nodes) <= MAX_NODES and len(edges) <= MAX_EDGES, "graph budget exceeded")
    return {
        "schema_version": 1,
        "kind": "COMPILED_DECLARATION_PROJECTION",
        "root_goal_id": scope,
        "skill": name,
        "bundle_id": bundle_id,
        "review_status": "UNREVIEWED_CANDIDATE",
        "integrity": "BUNDLE_HASHES_VERIFIED_NOT_AUTHENTICATED",
        "nodes": [nodes[k] for k in sorted(nodes)],
        "edges": [edges[k] for k in sorted(edges)],
        "unresolved": [unresolved[k] for k in sorted(unresolved)],
        "runtime_observations": "NOT_INCLUDED",
        "semantic_entailment": "NOT_EVALUATED",
        "execution_authorized": False,
    }


def project(skill: Path) -> dict:
    manifest = verify_bundle(skill)
    model = parse_json(safe_file(skill, "model.json", MAX_TOTAL))
    return from_model(model, manifest["bundle_id"])


def compose(skills: list[Path]) -> dict:
    """Combine disjoint goal-scoped projections, never deduplicating by label."""
    require(1 <= len(skills) <= MAX_SCOPES, "expected 1 to 16 skills")
    graphs = [project(path) for path in skills]
    scopes = [graph["root_goal_id"] for graph in graphs]
    require(len(scopes) == len(set(scopes)),
            "multiple versions of one root goal: use diff; select one explicitly")
    nodes = [node for graph in graphs for node in graph["nodes"]]
    edges = [edge for graph in graphs for edge in graph["edges"]]
    require(len(nodes) <= MAX_NODES and len(edges) <= MAX_EDGES, "combined graph budget exceeded")
    return {
        "schema_version": 1,
        "kind": "CROSS_SCOPE_DECLARATION_VIEW",
        "versions": sorted([
            {"root_goal_id": graph["root_goal_id"], "skill": graph["skill"],
             "bundle_id": graph["bundle_id"], "review_status": graph["review_status"]}
            for graph in graphs
        ], key=lambda x: (x["root_goal_id"], x["bundle_id"])),
        "nodes": sorted(nodes, key=lambda x: x["id"]),
        "edges": sorted(edges, key=lambda x: x["id"]),
        "unresolved": sorted(
            [item for graph in graphs for item in graph["unresolved"]],
            key=lambda x: (x["source"], x["relation"], x["missing_target"])
        ),
        "cross_project_dependencies": "NOT_INFERRED",
        "execution_authorized": False,
    }


def diff(old: Path, new: Path) -> dict:
    before, after = project(old), project(new)
    require(before["root_goal_id"] == after["root_goal_id"] and
            before["skill"] == after["skill"], "unrelated graph scopes")

    def delta(collection: str) -> dict:
        a = {x["id"]: x for x in before[collection]}
        b = {x["id"]: x for x in after[collection]}
        return {
            "added": sorted(b.keys() - a.keys()),
            "removed": sorted(a.keys() - b.keys()),
            "modified": sorted(k for k in a.keys() & b.keys() if a[k] != b[k]),
        }

    return {
        "schema_version": 1, "kind": "DECLARATION_GRAPH_DIFF",
        "root_goal_id": before["root_goal_id"],
        "from_bundle": before["bundle_id"], "to_bundle": after["bundle_id"],
        "nodes": delta("nodes"), "edges": delta("edges"),
        "unresolved_before": before["unresolved"],
        "unresolved_after": after["unresolved"],
        "status": "UNREVIEWED_CHANGE",
        "impact_completeness": "NOT_ESTABLISHED",
        "runtime_impact": "UNKNOWN",
        "promotion": "NOT_PERFORMED",
        "execution_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="project a verified compiled skill").add_argument("skill", type=Path)
    p = commands.add_parser("compose", help="combine different root-goal scopes")
    p.add_argument("skills", type=Path, nargs="+")
    p = commands.add_parser("diff", help="compare two verified versions of one root goal")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "export":
            result = project(args.skill)
        elif args.command == "compose":
            result = compose(args.skills)
        else:
            result = diff(args.old, args.new)
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        return 0
    except (DomainError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
