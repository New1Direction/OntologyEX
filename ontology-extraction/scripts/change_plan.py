#!/usr/bin/env python3
"""Read-only, evidence-aware structural change planning for compiled OntologyEX skills.

This is DECLARED-REFERENCE reachability, not RDFS/OWL entailment, runtime
blast-radius measurement, human approval, or permission to apply changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

OBJECTS = (
    ("10-upper.yaml", "anchors", "upper_anchor"),
    ("20-domain.yaml", "classes", "class"),
    ("20-domain.yaml", "interfaces", "interface"),
    ("20-domain.yaml", "relations", "relation"),
    ("30-task.yaml", "tasks", "task"),
    ("40-application.yaml", "concepts", "application_binding"),
)


def objects(model: dict, layer: str, collection: str) -> dict:
    """Compiled skill models have already passed the four-layer validator."""
    return {item["id"]: item for item in model["layers"].get(layer, {}).get(collection, [])}


def declared_plan(before: dict, after: dict, conservative_reruns: list[str]) -> dict:
    """Explain structurally possible effects of changes across L0-L3.

    It deliberately traverses *both* models, so removals do not hide consumers.
    Evidence references are IDs from the already-verified source contracts.
    """
    changed = []
    by_kind = {}
    for layer, collection, kind in OBJECTS:
        old, new = objects(before, layer, collection), objects(after, layer, collection)
        by_kind[kind] = set()
        for key in sorted(old.keys() | new.keys()):
            if old.get(key) == new.get(key):
                continue
            by_kind[kind].add(key)
            previous, proposed = old.get(key), new.get(key)
            change = "added" if previous is None else "removed" if proposed is None else "modified"
            changed.append({
                "layer": layer,
                "kind": kind,
                "id": key,
                "change": change,
                "evidence_before": previous.get("source") if previous else None,
                "evidence_after": proposed.get("source") if proposed else None,
            })

    classes = set(by_kind["class"])
    relationships = set(by_kind["relation"])
    tasks = set(by_kind["task"])
    bindings = set(by_kind["application_binding"])

    for model in (before, after):
        domain = model["layers"].get("20-domain.yaml", {})
        for item in domain.get("classes", []):
            if (item.get("upper") in by_kind["upper_anchor"] or
                    by_kind["interface"].intersection(item.get("implements", []))):
                classes.add(item["id"])
        # Only explicitly changed relations seed class impact. Avoid accidental
        # transitive closure through unrelated neighbors and list-order effects.
        for relation in domain.get("relations", []):
            if relation["id"] in by_kind["relation"]:
                classes.update([relation["domain"], relation["range"]])

    for model in (before, after):
        for relation in model["layers"].get("20-domain.yaml", {}).get("relations", []):
            if relation.get("domain") in classes or relation.get("range") in classes:
                relationships.add(relation["id"])

    for model in (before, after):
        for task in model["layers"].get("30-task.yaml", {}).get("tasks", []):
            referenced = set(task.get("inputs", [])) | set(task.get("outputs", [])) | set(task.get("actor_roles", []))
            if referenced & classes:
                tasks.add(task["id"])
        for binding in model["layers"].get("40-application.yaml", {}).get("concepts", []):
            if binding["id"] in by_kind["application_binding"]:
                tasks.update(binding.get("used_by_tasks", []))
            if binding.get("binds") in classes:
                bindings.add(binding["id"])
                tasks.update(binding.get("used_by_tasks", []))

    for model in (before, after):
        for binding in model["layers"].get("40-application.yaml", {}).get("concepts", []):
            if set(binding.get("used_by_tasks", [])) & tasks:
                bindings.add(binding["id"])

    return {
        "status": "STRUCTURAL_REVIEW_ONLY",
        "change_items": changed,
        "possible_impacted_classes": sorted(classes),
        "possible_impacted_relations": sorted(relationships),
        "possible_impacted_tasks": sorted(tasks),
        "possible_impacted_bindings": sorted(bindings),
        "conservative_rerun_tasks": sorted(set(conservative_reruns)),
        "reachability": "DECLARED_L0_L3_REFERENCES_NOT_SEMANTIC_CLOSURE",
        "semantic_conservativity": "NOT_EVALUATED",
        "runtime_impact": "UNKNOWN",
        "approval": "NOT_GRANTED",
        "execution_authorized": False,
    }


def plan(old: Path, new: Path) -> dict:
    """Verify both complete bundles through OntologyEX before reading models."""
    import domain_skill as ds
    from policy_check import MAX_TOTAL, parse_json, safe_file

    comparison = ds.compare(old, new)
    before = parse_json(safe_file(old, "model.json", MAX_TOTAL))
    after = parse_json(safe_file(new, "model.json", MAX_TOTAL))
    # compare verifies integrity, root goal identity, and rejects unrelated skills.
    return {**comparison, "declared_impact": declared_plan(before, after, comparison["rerun_tasks"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path, help="older compiled skill")
    parser.add_argument("new", type=Path, help="proposed compiled skill")
    args = parser.parse_args()
    try:
        result = plan(args.old, args.new)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
