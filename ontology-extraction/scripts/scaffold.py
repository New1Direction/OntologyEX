#!/usr/bin/env python3
"""Scaffold and validate four-layer ontology workspaces.

Usage:
  python scaffold.py init --name acme [--out ./acme-ontology]
  python scaffold.py validate <dir>
  python scaffold.py mappings <dir>     # regenerate 50-mappings.yaml from layers

Requires: pyyaml  (pip install pyyaml)
"""
import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pyyaml required: pip install pyyaml")

TEMPLATES = {
    "00-scope.md": """# Scope — {name}

## Target
<!-- company/business (research mode) or project/codebase (retrofit mode) -->

## Consumer
<!-- what eats this ontology: knowledge graph | agent tools | db/api schema | RAG | docs -->

## Boundary
<!-- which slice of the business; what is explicitly OUT of scope -->

## Competency questions
### L1 (domain)
- [ ] 
### L2 (task)
- [ ] 
### L3 (application)
- [ ] 
""",
    "10-upper.yaml": """source_ontology: gist   # gist | bfo+cco | dolce | sumo | schema.org | custom-minimal
version: "pin-me"
anchors:
  - id: Agent
    iri: "https://w3id.org/semanticarts/ns/ontology/gist/Agent"
  - id: Event
    iri: "https://w3id.org/semanticarts/ns/ontology/gist/Event"
""",
    "20-domain.yaml": """classes:
  - id: ExampleClass
    definition: "One sentence a competitor's employee would agree with."
    upper: Agent
    synonyms: []
    source: "where this came from"
relations: []
#  - id: example_relation
#    definition: ""
#    domain: ExampleClass
#    range: ExampleClass
#    cardinality: many-to-one
#    source: ""
""",
    "30-task.yaml": """tasks: []
#  - id: ExampleTask
#    verb_phrase: "do the thing"
#    actor_roles: []
#    inputs: []
#    outputs: []
#    preconditions: []
#    effects: []
#    source: ""
""",
    "40-application.yaml": """system: "{name}"
concepts: []
#  - id: example_table
#    kind: db_table   # db_table | api_type | tool_schema | event | ui_object
#    binds: ExampleClass
#    used_by_tasks: []
#    source: ""
""",
    "README.md": """# {name} ontology

Four-layer ontology. Edit the YAML files, then:

    python scaffold.py validate .
    python scaffold.py mappings .

Layer rules: every L3 `binds` an L1 class; every L1 class has an `upper` anchor;
every L2 task's inputs/outputs are L1 classes.
""",
}


def load(d: Path, name: str):
    p = d / name
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text()) or {}


def init(args):
    out = Path(args.out or f"./{args.name}-ontology")
    out.mkdir(parents=True, exist_ok=True)
    for fname, body in TEMPLATES.items():
        p = out / fname
        if p.exists():
            print(f"skip (exists): {p}")
            continue
        p.write_text(body.format(name=args.name))
        print(f"wrote: {p}")
    print(f"\nScaffolded {out}. Fill 00-scope.md first; CQs define done.")


def validate(args):
    d = Path(args.dir)
    errors, warnings = [], []

    upper = load(d, "10-upper.yaml") or {}
    domain = load(d, "20-domain.yaml") or {}
    task = load(d, "30-task.yaml") or {}
    app = load(d, "40-application.yaml") or {}

    anchors = {a["id"] for a in (upper.get("anchors") or [])}
    classes = domain.get("classes") or []
    class_ids = [c["id"] for c in classes]
    class_set = set(class_ids)
    relations = domain.get("relations") or []
    tasks = task.get("tasks") or []
    task_ids = [t["id"] for t in tasks]
    task_set = set(task_ids)
    concepts = app.get("concepts") or []

    # duplicate ids per layer
    for label, ids in (("upper anchor", [a["id"] for a in (upper.get("anchors") or [])]),
                       ("domain class", class_ids),
                       ("relation", [r["id"] for r in relations]),
                       ("task", task_ids),
                       ("app concept", [c["id"] for c in concepts])):
        seen = set()
        for i in ids:
            if i in seen:
                errors.append(f"duplicate {label} id: {i}")
            seen.add(i)

    # synonym collisions with class ids
    for c in classes:
        for s in c.get("synonyms") or []:
            if s in class_set:
                warnings.append(f"synonym '{s}' on {c['id']} collides with a class id — merge them")

    # L1 -> L0 anchoring
    for c in classes:
        if not c.get("upper"):
            errors.append(f"domain class {c['id']} has no 'upper' anchor")
        elif c["upper"] not in anchors:
            errors.append(f"domain class {c['id']} anchors to unknown upper '{c['upper']}'")
        if not c.get("definition"):
            warnings.append(f"domain class {c['id']} has no definition")
        if not c.get("source"):
            warnings.append(f"domain class {c['id']} has no source evidence")

    # relations resolve
    for r in relations:
        for end in ("domain", "range"):
            if r.get(end) not in class_set:
                errors.append(f"relation {r['id']}: {end} '{r.get(end)}' not a domain class")

    # L2 references L1
    for t in tasks:
        for field in ("inputs", "outputs"):
            for ref in t.get(field) or []:
                if ref not in class_set:
                    errors.append(f"task {t['id']}: {field} ref '{ref}' not a domain class")
        for role in t.get("actor_roles") or []:
            if role not in class_set:
                warnings.append(f"task {t['id']}: actor_role '{role}' not defined as a domain class")
        for sub in t.get("decomposes_to") or []:
            if sub not in task_set:
                errors.append(f"task {t['id']}: decomposes_to '{sub}' not a known task")
        if not t.get("source"):
            warnings.append(f"task {t['id']} has no source evidence")

    # L3 -> L1 and L3 -> L2
    for c in concepts:
        if not c.get("binds"):
            errors.append(f"app concept {c['id']} binds nothing — dead schema or missing L1 class?")
        elif c["binds"] not in class_set:
            errors.append(f"app concept {c['id']} binds unknown domain class '{c['binds']}'")
        for t in c.get("used_by_tasks") or []:
            if t not in task_set:
                errors.append(f"app concept {c['id']}: used_by_tasks '{t}' not a known task")

    # orphans: L1 classes nothing touches
    touched = set()
    for r in relations:
        touched.update([r.get("domain"), r.get("range")])
    for t in tasks:
        touched.update(t.get("inputs") or [])
        touched.update(t.get("outputs") or [])
        touched.update(t.get("actor_roles") or [])
    for c in concepts:
        touched.add(c.get("binds"))
    for cid in class_ids:
        if cid not in touched:
            warnings.append(f"orphan domain class: {cid} (no relation, task, or app concept touches it)")

    for e in errors:
        print(f"ERROR   {e}")
    for w in warnings:
        print(f"warning {w}")
    print(f"\n{len(errors)} errors, {len(warnings)} warnings")
    sys.exit(1 if errors else 0)


def mappings(args):
    d = Path(args.dir)
    domain = load(d, "20-domain.yaml") or {}
    app = load(d, "40-application.yaml") or {}
    by_id = {c["id"]: c for c in (domain.get("classes") or [])}
    out = []
    for c in app.get("concepts") or []:
        dc = by_id.get(c.get("binds"), {})
        out.append({
            "app": c["id"],
            "domain": c.get("binds"),
            "upper": dc.get("upper"),
            "evidence": c.get("source"),
        })
    (d / "50-mappings.yaml").write_text(yaml.safe_dump({"mappings": out}, sort_keys=False))
    print(f"wrote {d / '50-mappings.yaml'} ({len(out)} mappings)")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init")
    pi.add_argument("--name", required=True)
    pi.add_argument("--out")
    pi.set_defaults(fn=init)
    pv = sub.add_parser("validate")
    pv.add_argument("dir")
    pv.set_defaults(fn=validate)
    pm = sub.add_parser("mappings")
    pm.add_argument("dir")
    pm.set_defaults(fn=mappings)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
