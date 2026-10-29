#!/usr/bin/env python3
"""Everything known about one entity, across the business and system layers, with provenance.

    python entity_context.py --bundle BUNDLE.json --id ENTITY_ID
    python entity_context.py --bundle BUNDLE.json --search QUERY

A bundle is what the control room already holds: its state (discovered components,
declared edges, observations), its business picture (domain-skill objects and actions,
confirmed links, open suggestions, decided rows) and the decision history files. From
it this module answers, for one entity: what it is, what it relates to in both
directions, why each relationship is believed, what depends on it, what is proposed,
and what changed recently.

Every relationship carries a standing:

- SOURCE: a file states it (a compose dependency, a code declaration, a domain-skill
  item with its citation). It says what the file declares, not what runs.
- CONFIRMED: a person decided it (who, when, and their note come from the decision
  record, and which agent proposed it when one did). Only decisions produce this standing.
- INFERRED: proposed and not decided (a ranked link suggestion, a dependency seen in
  traces, an agent proposal) or reached through other relationships (impact).
- UNKNOWN: a gap the sources leave open (an action linked to no system, a link whose
  component is no longer discovered, an open question in a skill).

Read-only and deterministic: the same bundle gives the same answer. Nothing here
decides, links, or authorizes anything. The Rust engine computes the same context
(`ontologyex-view context`); tests/rust_context_parity.py checks they agree.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

STANDINGS = ("CONFIRMED", "SOURCE", "INFERRED", "UNKNOWN")
MAX_RELATIONSHIPS = 200
MAX_IMPACT = 100
MAX_IMPACT_DEPTH = 4
MAX_CHANGES = 25
MAX_SEARCH = 25
# Relations along which a change to the target can reach the source (source relies on target).
RELIES_ON = {"DEPENDS_ON", "CALLS", "RUNS_ON", "IMPLEMENTED_BY", "CONSUMES"}


WEAKNESS = {"CONFIRMED": 0, "SOURCE": 1, "INFERRED": 2, "UNKNOWN": 3}


def path_standing(standings) -> str:
    """A chain of relationships is only as strong as its weakest link (the first weakest on ties).

    The empty chain is CONFIRMED: an entity stands for itself. Lean proves the laws of this
    rule (proofs/lean, OntologyEX.Context) and tests/conformance.py checks Python, Rust and
    Lean compute the same table.
    """
    result = "CONFIRMED"
    for standing in standings:
        if WEAKNESS[standing] > WEAKNESS[result]:
            result = standing
    return result


class ContextError(ValueError):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ContextError(message)


def _entities(bundle: dict) -> dict[str, dict]:
    """Every entity both layers name, keyed by its ID."""
    state, picture = bundle["state"], bundle.get("business") or {}
    found: dict[str, dict] = {}
    for node in state["nodes"]:
        found[node["id"]] = {"id": node["id"], "layer": "system", "kind": node["kind"], "label": node["label"],
                             "status": node.get("status", "unknown"), "observed_at": node.get("observed_at"),
                             "evidence_ids": [node["evidence_id"]] if node.get("evidence_id") else []}
    for kind, rows in (("business_object", picture.get("objects", [])), ("business_action", picture.get("actions", []))):
        for row in rows:
            found[row["key"]] = {"id": row["key"], "layer": "business", "kind": kind, "label": row["id"],
                                 "skill": row["skill"], "status": row.get("health", "UNKNOWN"),
                                 "summary": row.get("definition") or row.get("verb_phrase") or "",
                                 "quotes": [{"path": e.get("path"), "start": e.get("start"), "end": e.get("end"),
                                             "quote": e.get("quote")} for e in row.get("evidence", [])][:4],
                                 "rules": [{k: r.get(k) for k in ("id", "statement", "status", "grade", "grade_label")}
                                           for r in row.get("rules", [])][:20],
                                 "open_questions": [{"type": b.get("type"), "id": b.get("id"),
                                                     "statement": b.get("statement")}
                                                    for b in row.get("blockers", [])][:20]}
    return found


def entity_ids(bundle: dict) -> set[str]:
    """The IDs `context` answers for."""
    return set(_entities(bundle))


def _history(bundle: dict) -> list[dict]:
    """Decision events, oldest first, each with the endpoints it touches."""
    events = []
    for name, rows in (("links", bundle.get("link_history", [])), ("dependencies", bundle.get("dependency_history", []))):
        for event in rows:
            row = event.get("row") or {}
            if name == "links":
                skill, binding, component = row.get("skill"), row.get("binding"), row.get("component")
                touches = [component] + [f"{skill}:binding:{binding}"]
                summary = f"{event.get('action')} link {binding} -> {component}"
            else:
                touches = [row.get("source"), row.get("target")]
                summary = f"{event.get('action')} dependency {row.get('source')} -> {row.get('target')}"
            events.append({"at": event.get("at"), "by": event.get("by"), "action": event.get("action"),
                           "note": event.get("note", ""), "file": name, "summary": summary,
                           "touches": [t for t in touches if t], "row": row})
    events.sort(key=lambda e: (e["at"] or "", e["file"], e["summary"]))
    return events


def _decision_for(history: list[dict], file: str, match: dict) -> dict | None:
    """The newest confirm event for a decided row: who confirmed it, when, and why."""
    for event in reversed(history):
        if event["file"] == file and event["action"] == "confirm" and all(
                event["row"].get(k) == v for k, v in match.items()):
            return {"by": event["by"], "at": event["at"], "note": event["note"]}
    return None


def _relationships(bundle: dict, entities: dict, history: list[dict]) -> list[dict]:
    """Every relationship both layers know, as one list of directed, provenance-carrying edges."""
    state, picture = bundle["state"], bundle.get("business") or {}
    out = []
    for edge in state["edges"]:
        confirmed = edge.get("state") == "OBSERVED_IN_TRACES_CONFIRMED_BY_PERSON"
        out.append({"source": edge["source"], "target": edge["target"], "relation": edge["relation"],
                    "standing": "CONFIRMED" if confirmed else "SOURCE",
                    "basis": edge.get("state") or "DECLARED_IN_SOURCE",
                    "evidence_ids": [edge["evidence_id"]] if edge.get("evidence_id") else [],
                    "decision": _decision_for(history, "dependencies", {"source": edge["source"],
                                                                         "target": edge["target"]})
                    if confirmed else None})
    # Business edges: links a person confirmed, and relations the skills declare.
    decided = {(r["skill"], r["binding"], r["component"]): r for r in picture.get("link_decided", [])
               if r.get("decision") in ("confirm", "confirmed")}
    proposers: dict[tuple, str] = {}  # who proposed a link, when an agent did: provenance kept after confirmation
    for proposal in bundle.get("agent_proposals", []):
        proposers.setdefault((proposal.get("skill"), proposal.get("binding"), proposal.get("component")),
                             proposal.get("proposed_by"))
    for kind, rows in (("action", picture.get("actions", [])), ("object", picture.get("objects", []))):
        for row in rows:
            for binding in row.get("bindings", []):
                binding_id = binding["id"] if isinstance(binding, dict) else binding
                components = binding.get("components", []) if isinstance(binding, dict) else row.get("systems", [])
                for component in components:
                    link = decided.get((row["skill"], binding_id, component))
                    decision = (_decision_for(history, "links", {"skill": row["skill"], "binding": binding_id,
                                                                 "component": component})
                                or ({"by": link.get("declared_by"), "at": None, "note": link.get("note", "")}
                                    if link else None))
                    if decision and (row["skill"], binding_id, component) in proposers:
                        decision = {**decision, "proposed_by": proposers[(row["skill"], binding_id, component)]}
                    out.append({"source": row["key"], "target": component,
                                "relation": "RUNS_ON" if kind == "action" else "IMPLEMENTED_BY",
                                "standing": "CONFIRMED", "basis": "PERSON_CONFIRMED_LINK",
                                "via": f"{row['skill']}:binding:{binding_id}", "evidence_ids": [],
                                "decision": decision})
    for edge in (picture.get("graph") or {}).get("edges", []):
        if edge["relation"] in ("RUNS_ON", "IMPLEMENTED_BY"):
            continue  # already above, with their decisions
        out.append({"source": edge["source"], "target": edge["target"], "relation": edge["relation"],
                    "standing": "SOURCE", "basis": "DECLARED_IN_DOMAIN_SKILL", "label": edge.get("label"),
                    "evidence_ids": [], "decision": None})
    # Proposals nobody has decided yet.
    for row in (picture.get("link_suggestions") or {}).get("suggestions", []):
        key = f"{row['skill']}:binding:{row['binding']}"
        actions = [a["key"] for a in picture.get("actions", []) if a["skill"] == row["skill"]
                   and any((b["id"] if isinstance(b, dict) else b) == row["binding"] for b in a.get("bindings", []))]
        for source in actions or [key]:
            out.append({"source": source, "target": row["component"], "relation": "RUNS_ON",
                        "standing": "INFERRED", "basis": "LINK_SUGGESTION", "via": key,
                        "suggestion": {"id": row["id"], "strength": row.get("strength"),
                                       "signals": [s.get("detail") or s.get("signal") for s in row.get("signals", [])][:6],
                                       "proposed_by": row.get("proposed_by", "ranked suggestion")},
                        "evidence_ids": [], "decision": None})
    for row in (picture.get("dependency_suggestions") or {}).get("suggestions", []):
        out.append({"source": row["source"], "target": row["target"], "relation": "CALLS",
                    "standing": "INFERRED", "basis": "SEEN_IN_TRACES_NOT_CONFIRMED",
                    "suggestion": {"id": row["id"], "strength": row.get("strength"),
                                   "signals": [f"{row.get('calls', 0)} calls, last seen {row.get('last_seen')}"],
                                   "proposed_by": "trace observation"},
                    "evidence_ids": [], "decision": None})
    # Gaps the sources leave open.
    for gap in picture.get("link_gaps", []):
        link = gap.get("link") or {}
        out.append({"source": f"{link.get('skill')}:binding:{link.get('binding')}", "target": link.get("component"),
                    "relation": "RUNS_ON", "standing": "UNKNOWN", "basis": gap.get("reason"),
                    "evidence_ids": [], "decision": None})
    return out


def _evidence(bundle: dict, ids: set[str]) -> list[dict]:
    records = {e["id"]: e for e in bundle["state"].get("source_evidence", [])}
    return [{k: records[i].get(k) for k in ("id", "path", "sha256", "source_kind", "authenticity")}
            for i in sorted(ids) if i in records]


def _side(entities: dict, entity_id: str) -> dict:
    other = entities.get(entity_id)
    if other is None:
        return {"id": entity_id, "layer": "unknown", "kind": "unknown", "label": entity_id or ""}
    return {k: other[k] for k in ("id", "layer", "kind", "label")}


def _impact(entity_id: str, relationships: list[dict], entities: dict) -> list[dict]:
    """What relies on this entity, transitively: the things a change to it could reach."""
    relying: dict[str, list[dict]] = {}
    for rel in relationships:
        if rel["relation"] in RELIES_ON and rel["standing"] != "UNKNOWN":
            relying.setdefault(rel["target"], []).append(rel)
    found, frontier, seen = [], [(entity_id, 0, "CONFIRMED", [])], {entity_id}
    while frontier and len(found) < MAX_IMPACT:
        current, depth, standing, path = frontier.pop(0)
        if depth >= MAX_IMPACT_DEPTH:
            continue
        for rel in sorted(relying.get(current, []), key=lambda r: (r["source"] or "", r["relation"], r["standing"])):
            if rel["source"] in seen:
                continue
            seen.add(rel["source"])
            step = path_standing([standing, rel["standing"]])
            hop = path + [f"{rel['source']} {rel['relation']} {current}"]
            found.append({**_side(entities, rel["source"]), "depth": depth + 1, "standing": step, "path": hop})
            frontier.append((rel["source"], depth + 1, step, hop))
    return found[:MAX_IMPACT]


def context(bundle: dict, entity_id: str) -> dict:
    """Everything known about one entity. Raises ContextError for an unknown ID."""
    _check(isinstance(bundle, dict) and isinstance(bundle.get("state"), dict), "expected a context bundle")
    _check(bundle["state"].get("execution_authorized") is False, "the state must not authorize execution")
    _check(type(entity_id) is str and 0 < len(entity_id) <= 1024, "invalid entity ID")
    entities = _entities(bundle)
    history = _history(bundle)
    relationships = _relationships(bundle, entities, history)
    # A binding is not an entity of its own; its links name the action through "via".
    _check(entity_id in entities, "entity not found")
    entity = entities[entity_id]
    mine = [r for r in relationships if entity_id in (r["source"], r["target"])]
    mine.sort(key=lambda r: (STANDINGS.index(r["standing"]), r["relation"], r["source"] or "", r["target"] or ""))
    rows, evidence_ids = [], set(entity.get("evidence_ids", []))
    for rel in mine[:MAX_RELATIONSHIPS]:
        outgoing = rel["source"] == entity_id
        evidence_ids.update(rel["evidence_ids"])
        rows.append({"direction": "out" if outgoing else "in", "relation": rel["relation"],
                     "other": _side(entities, rel["target"] if outgoing else rel["source"]),
                     **{k: rel[k] for k in ("standing", "basis", "evidence_ids", "decision")},
                     **{k: rel[k] for k in ("via", "label", "suggestion") if rel.get(k) is not None}})
    vias = {r.get("via") for r in mine if r.get("via")} | {entity_id}
    changes = [{k: e[k] for k in ("at", "by", "action", "note", "file", "summary")}
               for e in reversed(history) if vias & set(e["touches"])][:MAX_CHANGES]
    why = ([f"{entity['label']}: {entity['summary']} (declared in the {entity['skill']} skill)"]
           if entity["layer"] == "business" and entity.get("summary") else [])
    why += [f"{r['other']['label']} {r['relation'].lower().replace('_', ' ')} this ({r['standing'].lower()}"
           + (f", confirmed by {r['decision']['by']}" + (f" on {r['decision']['at'][:10]}" if r['decision'].get('at') else "")
              if r.get("decision") else "") + ")"
           for r in rows if r["direction"] == "in" and r["standing"] in ("CONFIRMED", "SOURCE")][:12]
    unknowns = [r for r in rows if r["standing"] == "UNKNOWN"]
    for question in entity.get("open_questions", []):
        if question["type"] in ("unknown", "conflict"):
            unknowns.append({"direction": "out", "relation": "OPEN_QUESTION",
                             "other": {"id": question["id"], "layer": "business", "kind": question["type"],
                                       "label": question["statement"]}, "standing": "UNKNOWN",
                             "basis": f"SKILL_{question['type'].upper()}", "evidence_ids": [], "decision": None})
    if entity["layer"] == "business" and not any(r["relation"] in ("RUNS_ON", "IMPLEMENTED_BY") and r["standing"] == "CONFIRMED"
                                                  for r in rows):
        unknowns.append({"direction": "out", "relation": "RUNS_ON", "other": {"id": "", "layer": "unknown",
                         "kind": "unknown", "label": "no confirmed system"}, "standing": "UNKNOWN",
                         "basis": "NO_CONFIRMED_LINK", "evidence_ids": [], "decision": None})
    counts = {s: sum(r["standing"] == s for r in rows) for s in STANDINGS}
    return {"schema_version": 1, "kind": "ENTITY_CONTEXT", "entity": {k: v for k, v in entity.items() if k != "evidence_ids"},
            "relationships": rows, "relationships_total": len(mine),
            "relationships_omitted": max(0, len(mine) - MAX_RELATIONSHIPS),
            "standing_counts": counts, "why": why, "relied_on_by": _impact(entity_id, relationships, entities),
            "suggestions": [r for r in rows if r["standing"] == "INFERRED" and r.get("suggestion")],
            "unknowns": unknowns, "recent_changes": changes,
            "source_evidence": _evidence(bundle, evidence_ids),
            "standing_semantics": "CONFIRMED_BY_PERSON|SOURCE_DECLARED|INFERRED_NOT_DECIDED|UNKNOWN_GAP",
            "impact_semantics": "DECLARED_REACHABILITY_NOT_MEASURED_BLAST_RADIUS",
            "execution_authorized": False}


def explain(bundle: dict, source: str, target: str) -> dict:
    """Why two entities are related: every path between them (up to three hops), each with
    the weakest standing along it and the decisions and evidence each hop rests on."""
    _check(isinstance(bundle, dict) and isinstance(bundle.get("state"), dict), "expected a context bundle")
    entities = _entities(bundle)
    _check(source in entities and target in entities, "entity not found")
    history = _history(bundle)
    adjacency: dict[str, list[tuple[str, dict]]] = {}
    for rel in _relationships(bundle, entities, history):
        adjacency.setdefault(rel["source"], []).append((rel["target"], rel))
        adjacency.setdefault(rel["target"], []).append((rel["source"], rel))
    paths, frontier = [], [(source, [], {source})]
    for _ in range(3):
        nxt = []
        for node, hops, seen in frontier:
            for other, rel in sorted(adjacency.get(node, []), key=lambda x: (x[0] or "", x[1]["relation"], x[1]["standing"])):
                if other in seen:
                    continue
                hop = {"from": _side(entities, node), "to": _side(entities, other), "relation": rel["relation"],
                       "direction": "out" if rel["source"] == node else "in",
                       **{k: rel[k] for k in ("standing", "basis", "evidence_ids", "decision")},
                       **{k: rel[k] for k in ("via", "suggestion") if rel.get(k) is not None}}
                if other == target:
                    paths.append(hops + [hop])
                else:
                    nxt.append((other, hops + [hop], seen | {other}))
        frontier = nxt[:2000]
        if len(paths) >= 10:
            break
    paths.sort(key=lambda p: (len(p), WEAKNESS[path_standing(h["standing"] for h in p)]))
    out = [{"standing": path_standing(h["standing"] for h in p), "hops": p} for p in paths[:10]]
    ids = {i for p in out for h in p["hops"] for i in h["evidence_ids"]}
    return {"schema_version": 1, "kind": "RELATIONSHIP_EXPLANATION", "source": _side(entities, source),
            "target": _side(entities, target), "paths": out,
            "verdict": out[0]["standing"] if out else "NO_PATH_WITHIN_THREE_HOPS",
            "source_evidence": _evidence(bundle, ids),
            "standing_semantics": "A_PATH_IS_AS_STRONG_AS_ITS_WEAKEST_HOP", "execution_authorized": False}


def search(bundle: dict, query: str) -> dict:
    """Entities whose label, ID or summary contains every word of the query."""
    _check(type(query) is str and 0 < len(query) <= 200 and all(" " <= ch != "\x7f" for ch in query),
           "invalid query")  # no control characters; the Rust engine applies the same rule
    words = query.lower().split()
    hits = []
    for entity in _entities(bundle).values():
        text = f"{entity['label']} {entity['id']} {entity.get('summary', '')}".lower()
        if all(w in text for w in words):
            hits.append({k: entity[k] for k in ("id", "layer", "kind", "label")} | {"status": entity.get("status")})
    hits.sort(key=lambda e: (e["layer"] != "business", len(e["label"]), e["id"]))
    return {"schema_version": 1, "kind": "ENTITY_SEARCH", "query": query, "results": hits[:MAX_SEARCH],
            "total": len(hits), "execution_authorized": False}


def open_suggestions(bundle: dict, limit: int = MAX_RELATIONSHIPS) -> dict:
    """Every proposal nobody has decided yet (ranked links, trace-seen calls, agent proposals)."""
    _check(isinstance(bundle, dict) and isinstance(bundle.get("state"), dict), "expected a context bundle")
    _check(bundle["state"].get("execution_authorized") is False, "context never reads an authorizing state")
    entities = _entities(bundle)
    rows, seen = [], set()
    for rel in _relationships(bundle, entities, _history(bundle)):
        suggestion = rel.get("suggestion")
        if rel["standing"] != "INFERRED" or not suggestion or (suggestion["id"], rel["source"]) in seen:
            continue
        seen.add((suggestion["id"], rel["source"]))
        rows.append({"id": suggestion["id"], "kind": "link" if rel["basis"] == "LINK_SUGGESTION" else "dependency",
                     "source": _side(entities, rel["source"]), "relation": rel["relation"],
                     "target": _side(entities, rel["target"]), **{k: suggestion[k] for k in
                                                                    ("strength", "signals", "proposed_by")},
                     **({"via": rel["via"]} if rel.get("via") else {})})
    rows.sort(key=lambda r: ({"STRONG": 0, "MODERATE": 1, "WEAK": 2}.get(r["strength"], 3), r["id"], r["source"]["id"]))
    return {"schema_version": 1, "kind": "OPEN_SUGGESTIONS", "suggestions": rows[:limit], "total": len(rows),
            "semantics": "PROPOSED_NOT_DECIDED_A_PERSON_CONFIRMS_IN_THE_CONTROL_ROOM", "execution_authorized": False}


def recent(bundle: dict, limit: int = MAX_CHANGES) -> dict:
    """The newest recorded decisions across links and dependencies."""
    _check(isinstance(bundle, dict) and isinstance(bundle.get("state"), dict), "expected a context bundle")
    _check(bundle["state"].get("execution_authorized") is False, "context never reads an authorizing state")
    events = [{k: e[k] for k in ("at", "by", "action", "note", "file", "summary", "touches")}
              for e in reversed(_history(bundle))]
    return {"schema_version": 1, "kind": "RECENT_DECISIONS", "changes": events[:limit], "total": len(events),
            "execution_authorized": False}


def read_history(path: Path | None, limit: int = 5000) -> list[dict]:
    """The decision events a history file holds (newest kept when it is long); missing file is empty."""
    if path is None or not path.is_file():
        return []
    rows = []
    for line in path.read_text("utf-8", "replace").splitlines()[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn last line from a concurrent append
        if isinstance(row, dict) and isinstance(row.get("row"), dict):
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="a control-room context bundle (JSON)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--id")
    group.add_argument("--search")
    args = parser.parse_args()
    try:
        bundle = json.loads(args.bundle.read_text("utf-8"))
        result = context(bundle, args.id) if args.id else search(bundle, args.search)
    except (ContextError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)[:300]}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
