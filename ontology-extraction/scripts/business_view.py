#!/usr/bin/env python3
"""Business operating picture: verified domain skills joined to the control-room system map.

Read-only projection. A link from a code binding to a discovered component is an
operator declaration, not a discovered fact. Health is the source-reported status of
linked systems, rolled up per business action and object; it never proves that an
action works or fails, and no action is authorized or executed here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import evidence_grade
from policy_check import DomainError, ID, MAX_TOTAL, keys, parse_json, require, safe_file

MAX_SKILLS = 16
MAX_LINKS = 512
MAX_QUOTE = 600
LINK_KIND = "ONTOLOGYEX_SYSTEM_LINKS"
# Most urgent first. REPORTED_OK is a source report, not verified health.
HEALTH = ("AT_RISK", "POSSIBLY_IMPACTED", "DEGRADED", "STALE", "UNKNOWN", "UNLINKED", "REPORTED_OK")


def _link_rows(rows: object, seen: set) -> list[dict]:
    require(type(rows) is list and len(rows) <= MAX_LINKS, "invalid links list")
    for link in rows:
        keys(link, {"skill", "binding", "component", "declared_by", "note"}, "link")
        require(type(link["skill"]) is str and 0 < len(link["skill"]) <= 64, "invalid link skill")
        require(type(link["binding"]) is str and ID.fullmatch(link["binding"]) is not None,
                "invalid link binding")
        require(type(link["component"]) is str and 0 < len(link["component"]) <= 1024,
                "invalid link component")
        require(type(link["declared_by"]) is str and 0 < len(link["declared_by"]) <= 120,
                "links need a declaring person or team")
        require(type(link["note"]) is str and len(link["note"]) <= 500, "invalid link note")
        key = (link["skill"], link["binding"], link["component"])
        require(key not in seen, "duplicate link or a link that is also rejected")
        seen.add(key)
    return rows


def read_link_document(path: Path) -> tuple[list[dict], list[dict]]:
    """Confirmed links and rejected suggestions. A pair is never both."""
    require(path.is_file() and not path.is_symlink(), "links path must be a regular file")
    document = parse_json(safe_file(path.parent, path.name, MAX_TOTAL))
    require(type(document) is dict and set(document) in (
        {"schema_version", "kind", "links"}, {"schema_version", "kind", "links", "rejected"}),
        "links document: unexpected fields")
    require(document["schema_version"] == 1 and document["kind"] == LINK_KIND,
            "unsupported links document")
    seen: set = set()
    links = _link_rows(document["links"], seen)
    return links, _link_rows(document.get("rejected", []), seen)


def read_links(path: Path | None) -> list[dict]:
    """Operator-declared binding -> component links. Unknown targets become gaps, not edges."""
    return [] if path is None else read_link_document(path)[0]


def rollup(systems: list[dict]) -> tuple[str, str]:
    """Most urgent source-reported condition among linked systems, with its reason."""
    if not systems:
        return "UNLINKED", "No system is linked to this item, so its health is unknown."
    for system in systems:
        if system["status"] == "error":
            return "AT_RISK", f"Linked system {system['label']} reports error."
    for system in systems:
        if system["potentially_impacted_by"]:
            sources = ", ".join(system["potentially_impacted_by"])
            return ("POSSIBLY_IMPACTED",
                    f"{system['label']} declares a dependency on failing {sources}.")
    for system in systems:
        if system["status"] == "warning":
            return "DEGRADED", f"Linked system {system['label']} reports a warning."
    for system in systems:
        if system["status"] == "stale":
            return "STALE", f"The last observation of {system['label']} has expired."
    for system in systems:
        if system["status"] != "ok":
            return "UNKNOWN", f"No current observation for {system['label']}."
    return "REPORTED_OK", "Every linked system reports ok. Reports are not independently attested."


def _evidence(records: dict, refs: list) -> list[dict]:
    out = []
    for ref in refs:
        record = records.get(ref)
        if record is None:
            continue
        quote = record.get("quote", "")
        out.append({"id": ref, "path": record["path"], "start_line": record["start_line"],
                    "end_line": record["end_line"], "kind": record["kind"],
                    "quote": quote[:MAX_QUOTE], "quote_truncated": len(quote) > MAX_QUOTE})
    return out


def _system(node: dict, labels: dict) -> dict:
    return {"id": node["id"], "label": node["label"], "kind": node["kind"],
            "status": node["status"], "observed_at": node.get("observed_at"),
            "observation_source": node.get("observation_source"),
            "observation_sources": node.get("observation_sources", []),
            "metrics": node.get("metrics", {}),
            "potentially_impacted_by": [labels.get(i, i) for i in node.get("potentially_impacted_by", [])]}


def project(skills: list[Path], state: dict, links: list[dict] | None = None,
            events: Path | None = None) -> dict:
    """Join verified skills to one control-room state evaluation. Deterministic and read-only."""
    require(type(skills) is list and len(skills) <= MAX_SKILLS, "expected at most 16 skills")
    require(type(state) is dict and state.get("kind") == "LOCAL_CONTROL_ROOM_STATE",
            "expected a control-room state")
    links = links or []
    nodes = {n["id"]: n for n in state["nodes"]}
    labels = {n["id"]: n["label"] for n in state["nodes"]}
    loaded, objects, actions, graph_edges, gaps = [], [], [], [], []
    linked_systems: dict[str, dict] = {}
    serves: dict[str, set] = {}
    names = set()
    bindings_by_skill: dict[str, set] = {}

    models = []
    for path in skills:
        manifest, model = evidence_grade.load_skill(path)
        name = model["name"]
        require(name not in names, f"two loaded skills share the name {name}")
        names.add(name)
        grades = evidence_grade.grade(path, events)
        models.append((path, manifest, model, grades))
        bindings_by_skill[name] = {c["id"] for c in model["layers"]["40-application.yaml"].get("concepts", [])}

    link_map: dict[tuple[str, str], list[str]] = {}
    for link in links:
        if link["skill"] not in bindings_by_skill:
            gaps.append({"link": link, "reason": "LINK_SKILL_NOT_LOADED"})
        elif link["binding"] not in bindings_by_skill[link["skill"]]:
            gaps.append({"link": link, "reason": "LINK_BINDING_NOT_IN_SKILL"})
        elif link["component"] not in nodes:
            gaps.append({"link": link, "reason": "LINK_COMPONENT_NOT_DISCOVERED"})
        else:
            link_map.setdefault((link["skill"], link["binding"]), []).append(link["component"])

    for path, manifest, model, grades in models:
        name = model["name"]
        layers = model["layers"]
        records = {e["id"]: e for e in model["evidence"]}
        rule_grades = {r["id"]: r for r in grades["rules"]}
        loaded.append({"name": name, "root_goal_id": model["root_goal_id"],
                       "bundle_id": manifest["bundle_id"], "goal": model.get("goal"),
                       "review_status": model.get("review_status", "UNREVIEWED_CANDIDATE"),
                       "grade_distribution": grades["distribution"],
                       "stale_events": len(grades["stale_events"])})

        def key(kind: str, local: str) -> str:
            return f"{name}:{kind}:{local}"

        bindings = []
        for concept in layers["40-application.yaml"].get("concepts", []):
            components = sorted(set(link_map.get((name, concept["id"]), [])))
            for component in components:
                linked_systems[component] = nodes[component]
            bindings.append({"id": concept["id"], "kind": concept.get("kind"),
                             "binds": concept.get("binds"),
                             "used_by_tasks": concept.get("used_by_tasks", []),
                             "components": components,
                             "evidence": _evidence(records, [concept.get("source")])})

        relations = layers["20-domain.yaml"].get("relations", [])
        tasks = layers["30-task.yaml"].get("tasks", [])
        for cls in layers["20-domain.yaml"].get("classes", []):
            cid = cls["id"]
            uses = []
            for task in tasks:
                for role, field in (("consumes", "inputs"), ("produces", "outputs"),
                                    ("actor", "actor_roles")):
                    if cid in task.get(field, []):
                        uses.append({"action": key("action", task["id"]), "label": task["id"], "role": role})
            mine = [b for b in bindings if b["binds"] == cid]
            systems = sorted({c for b in mine for c in b["components"]})
            health, reason = rollup([_system(nodes[c], labels) for c in systems])
            for c in systems:
                serves.setdefault(c, set()).add(key("object", cid))
                graph_edges.append({"source": key("object", cid), "target": c,
                                    "relation": "IMPLEMENTED_BY"})
            objects.append({
                "key": key("object", cid), "id": cid, "skill": name,
                "definition": cls.get("definition", ""), "anchor": cls.get("upper"),
                "relations": [{"id": r["id"], "direction": "out" if r["domain"] == cid else "in",
                               "other": r["range"] if r["domain"] == cid else r["domain"],
                               "cardinality": r.get("cardinality"),
                               "definition": r.get("definition", "")}
                              for r in relations if cid in (r["domain"], r["range"])],
                "actions": uses, "bindings": [b["id"] for b in mine], "systems": systems,
                "health": health, "health_reason": reason,
                "evidence": _evidence(records, [cls.get("source")]),
            })
        for r in relations:
            graph_edges.append({"source": key("object", r["domain"]),
                                "target": key("object", r["range"]),
                                "relation": "RELATION", "label": r["id"]})

        for task in tasks:
            tid = task["id"]
            rules = [r for r in model["rules"] if r["task"] == tid]
            unknowns = [u for u in model["unknowns"] if u["task"] == tid]
            conflicts = [c for c in model["conflicts"] if c["task"] == tid]
            blockers = ([{"type": "unknown", "id": u["id"], "statement": u["statement"]} for u in unknowns] +
                        [{"type": "conflict", "id": c["id"], "statement": c["statement"]} for c in conflicts] +
                        [{"type": "inferred rule", "id": r["id"], "statement": r["statement"]}
                         for r in rules if r["status"] == "inferred"] +
                        [{"type": "no executable check", "id": r["id"], "statement": r["statement"]}
                         for r in rules if r["status"] == "observed" and r["check"] is None])
            mine = [b for b in bindings if tid in b["used_by_tasks"]]
            systems = sorted({c for b in mine for c in b["components"]})
            health, reason = rollup([_system(nodes[c], labels) for c in systems])
            for c in systems:
                serves.setdefault(c, set()).add(key("action", tid))
                graph_edges.append({"source": key("action", tid), "target": c, "relation": "RUNS_ON"})
            for role, field in (("CONSUMES", "inputs"), ("PRODUCES", "outputs"),
                                ("ACTOR", "actor_roles")):
                for cid in task.get(field, []):
                    graph_edges.append({"source": key("action", tid), "target": key("object", cid),
                                        "relation": role})
            distribution = {g: 0 for g, _, _ in evidence_grade.SCALE}
            for r in rules:
                distribution[rule_grades[r["id"]]["grade"]] += 1
            actions.append({
                "key": key("action", tid), "id": tid, "skill": name,
                "verb_phrase": task.get("verb_phrase", ""), "actor_roles": task.get("actor_roles", []),
                "inputs": task.get("inputs", []), "outputs": task.get("outputs", []),
                "effects": task.get("effects", []),
                "readiness": "REVIEW_BLOCKED" if blockers else "MODELED_NOT_APPROVED",
                "blockers": blockers,
                "rules": [{"id": r["id"], "statement": r["statement"], "status": r["status"],
                           "executable": r["check"] is not None,
                           "grade": rule_grades[r["id"]]["grade"], "grade_label": rule_grades[r["id"]]["label"],
                           "held_by": rule_grades[r["id"]]["held_by"],
                           "evidence": _evidence(records, r["evidence"])} for r in rules],
                "unknowns": [{"id": u["id"], "statement": u["statement"],
                              "evidence": _evidence(records, u["evidence"])} for u in unknowns],
                "conflicts": [{"id": c["id"], "statement": c["statement"],
                               "evidence": _evidence(records, c["evidence"])} for c in conflicts],
                "grade_distribution": distribution,
                "bindings": [{k: b[k] for k in ("id", "kind", "components", "evidence")} for b in mine],
                "systems": systems, "health": health, "health_reason": reason,
                "evidence": _evidence(records, [task.get("source")]),
            })

    systems = [{**_system(node, labels), "serves": sorted(serves.get(cid, set()))}
               for cid, node in sorted(linked_systems.items())]
    rank = {h: i for i, h in enumerate(HEALTH)}
    attention = []
    for item, kind in [(a, "action") for a in actions] + [(o, "object") for o in objects]:
        if item["health"] in ("AT_RISK", "POSSIBLY_IMPACTED", "DEGRADED", "STALE"):
            attention.append({"key": item["key"], "kind": kind, "label": item["id"], "skill": item["skill"],
                              "severity": item["health"], "reason": item["health_reason"]})
    for action in actions:
        if action["blockers"]:
            attention.append({"key": action["key"], "kind": "action", "label": action["id"],
                              "skill": action["skill"], "severity": "REVIEW_BLOCKED",
                              "reason": f"{len(action['blockers'])} open review blocker(s): "
                                        + action["blockers"][0]["statement"]})
    for action in actions:
        if action["health"] == "UNLINKED":
            attention.append({"key": action["key"], "kind": "action", "label": action["id"],
                              "skill": action["skill"], "severity": "UNLINKED",
                              "reason": action["health_reason"]})
    order = {s: i for i, s in enumerate(("AT_RISK", "POSSIBLY_IMPACTED", "DEGRADED", "STALE",
                                         "REVIEW_BLOCKED", "UNLINKED"))}
    attention.sort(key=lambda a: (order[a["severity"]], a["kind"] != "action", a["label"], a["skill"]))
    rules_total = sum(len(a["rules"]) for a in actions)
    reviewed = sum(1 for a in actions for r in a["rules"] if r["grade"] != "G0")
    health_counts = {h: sum(a["health"] == h for a in actions) for h in HEALTH}
    graph_nodes = ([{"key": o["key"], "type": "object", "label": o["id"], "health": o["health"]} for o in objects] +
                   [{"key": a["key"], "type": "action", "label": a["id"], "health": a["health"],
                     "readiness": a["readiness"]} for a in actions] +
                   [{"key": s["id"], "type": "system", "label": s["label"], "status": s["status"]} for s in systems])
    edges = sorted({json.dumps(e, sort_keys=True) for e in graph_edges})
    return {
        "schema_version": 1, "kind": "BUSINESS_OPERATING_PICTURE",
        "status": "READY" if loaded else "NO_DOMAIN_SKILLS",
        "generated_at": state["generated_at"],
        "skills": loaded,
        "summary": {
            "objects": len(objects), "actions": len(actions), "rules": rules_total,
            "rules_reviewed_or_better": reviewed,
            "review_blocked_actions": sum(a["readiness"] == "REVIEW_BLOCKED" for a in actions),
            "action_health": health_counts,
            "linked_systems": len(systems),
            "system_status": {s: sum(n["status"] == s for n in state["nodes"])
                              for s in ("ok", "warning", "error", "stale", "unknown")},
            "discovered_components": len(state["nodes"]),
        },
        "attention": attention,
        "objects": sorted(objects, key=lambda o: (rank[o["health"]], o["skill"], o["id"])),
        "actions": sorted(actions, key=lambda a: (rank[a["health"]], a["skill"], a["id"])),
        "systems": systems,
        "graph": {"nodes": graph_nodes, "edges": [json.loads(e) for e in edges]},
        "link_gaps": gaps,
        "health_semantics": "LINKED_SYSTEM_STATUS_SOURCE_REPORTED_NOT_ACTION_OUTCOME",
        "link_semantics": "OPERATOR_DECLARED_BINDING_TO_COMPONENT_NOT_DISCOVERED",
        "grade_semantics": "ORDINAL_FROM_RECORDED_EVENTS_NOT_CONFIDENCE",
        "execution_authorized": False,
    }


def main() -> int:
    import control_room
    import discover_repo

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--skill", type=Path, action="append", default=[])
    parser.add_argument("--links", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        state = control_room.build_state(discover_repo.discover(args.repo),
                                         control_room.read_observations(args.observations), {})
        result = project(args.skill, state, read_links(args.links), args.events)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DomainError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
