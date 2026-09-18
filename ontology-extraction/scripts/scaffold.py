#!/usr/bin/env python3
"""Scaffold and validate four-layer ontology workspaces.

Usage:
  python3 scaffold.py init --name acme [--out ./acme-ontology]
  python3 scaffold.py validate <dir>
  python3 scaffold.py mappings <dir>     # regenerate 50-mappings.yaml from layers

Requires: pyyaml  (pip install pyyaml)
"""
import argparse
import re
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
    "20-domain.yaml": """interfaces: []
#  - id: Addressable
#    definition: "Can be associated with a postal or physical address."
#    properties: [address]
#    source: "where this trait came from"
classes:
  - id: ExampleClass
    definition: "One sentence a competitor's employee would agree with."
    upper: Agent
    implements: []
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

    python3 scaffold.py validate .
    python3 scaffold.py mappings .

Layer rules: every L3 `binds` an L1 class; every L1 class has an `upper` anchor;
every L2 task's inputs/outputs are L1 classes.
""",
}

VAGUE_CLASS_IDS = {"Asset", "Entity", "Object", "Record", "Item", "Thing", "Data", "Resource"}
DEPARTMENT_PREFIXES = {
    "sales", "support", "billing", "finance", "marketing", "ops", "operations",
    "service", "success", "crm", "erp", "warehouse", "system", "vendor", "internal",
}
TECHNICAL_FIELD_PATTERNS = (
    r"^_", r"^etl_", r"^elt_", r"^dw_", r"^dwh_", r"^tmp_", r"^debug_",
    r"^batch_", r"^extract_", r"^ingest_", r"^cdc_", r"^load_", r"^row_",
    r"^record_", r"^source_system", r"^sync_", r"^job_", r"^run_id$",
)
PROPERTY_UPDATE_WORDS = {
    "phone", "email", "address", "name", "status", "note", "tag", "field", "property",
    "attribute", "metadata", "flag", "description", "owner",
}


def split_identifier(identifier: str):
    """Split snake/kebab/camel identifiers into lowercase words for lint heuristics."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(identifier))
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+|\s+", spaced) if w]


def normalised_business_noun(class_id: str):
    words = split_identifier(class_id)
    if len(words) > 1 and words[0] in DEPARTMENT_PREFIXES:
        words = words[1:]
    while len(words) > 1 and words[-1] in {"row", "record", "table", "object", "entity", "resource"}:
        words = words[:-1]
    return "".join(words)


def field_name(field):
    if isinstance(field, dict):
        return str(field.get("name") or field.get("id") or "")
    return str(field or "")


def looks_technical_field(name: str):
    lowered = name.lower()
    return any(re.search(pattern, lowered) for pattern in TECHNICAL_FIELD_PATTERNS)


LAYER_COLLECTIONS = {
    "10-upper.yaml": ("anchors",),
    "20-domain.yaml": ("classes", "interfaces", "relations"),
    "30-task.yaml": ("tasks",),
    "40-application.yaml": ("concepts",),
}
REFERENCE_LISTS = {
    "interfaces": ("applies_to",),
    "classes": ("synonyms", "implements"),
    "tasks": ("inputs", "outputs", "actor_roles", "decomposes_to"),
    "concepts": ("used_by_tasks",),
}
REFERENCE_FIELDS = {
    "classes": ("upper",),
    "relations": ("domain", "range"),
    "concepts": ("binds",),
}


class WorkspaceError(ValueError):
    """An actionable input error, rather than an internal validator failure."""


def is_identifier(value):
    return isinstance(value, str) and bool(value.strip())


def check_shape(document, name):
    """Check types before the cross-layer validator indexes or hashes values.

    Extension fields are permitted. Missing definitions and sources remain lint
    warnings; this is structural validation, not a semantic completeness proof.
    """
    collections = LAYER_COLLECTIONS.get(name, ())
    for index, key in enumerate(collections):
        if key not in document:
            if index == 0:
                raise WorkspaceError(f"{name}: missing required '{key}' list")
            continue
        entries = document[key]
        if not isinstance(entries, list):
            raise WorkspaceError(f"{name}: '{key}' must be a list (use [] when empty)")
        if key in ("anchors", "classes") and not entries:
            raise WorkspaceError(f"{name}: '{key}' must contain at least one entry")
        for number, entry in enumerate(entries):
            location = f"{name}: {key}[{number}]"
            if not isinstance(entry, dict):
                raise WorkspaceError(f"{location} must be a mapping")
            if not is_identifier(entry.get("id")):
                raise WorkspaceError(f"{location}.id must be a non-empty string")
            for field in REFERENCE_FIELDS.get(key, ()):
                if field in entry and not is_identifier(entry[field]):
                    raise WorkspaceError(f"{location}.{field} must be a non-empty string")
            for field in REFERENCE_LISTS.get(key, ()):
                if field not in entry:
                    continue
                refs = entry[field]
                if not isinstance(refs, list) or not all(is_identifier(ref) for ref in refs):
                    raise WorkspaceError(f"{location}.{field} must be a list of non-empty strings")
            if key == "concepts" and "fields" in entry:
                fields = entry["fields"]
                if not isinstance(fields, list) or not all(isinstance(f, (str, dict)) for f in fields):
                    raise WorkspaceError(f"{location}.fields must be a list of names or mappings")


def load(d: Path, name: str):
    p = d / name
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(f"{name}: cannot read required file ({exc})") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f":{mark.line + 1}:{mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise WorkspaceError(f"{name}{location}: invalid YAML: {problem}") from exc
    if not isinstance(document, dict) or not document:
        raise WorkspaceError(f"{name}: expected a non-empty YAML mapping")
    check_shape(document, name)
    return document


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

    if not d.is_dir():
        raise WorkspaceError(f"{d}: workspace must be an existing directory")
    layers = {}
    for name in LAYER_COLLECTIONS:
        try:
            layers[name] = load(d, name)
        except WorkspaceError as exc:
            errors.append(str(exc))
    if errors:
        for error in errors:
            print(f"ERROR   {error}")
        print(f"\n{len(errors)} errors, 0 warnings (layer checks not run)")
        sys.exit(1)

    upper = layers["10-upper.yaml"]
    domain = layers["20-domain.yaml"]
    task = layers["30-task.yaml"]
    app = layers["40-application.yaml"]

    anchors = {a["id"] for a in (upper.get("anchors") or [])}
    interfaces = domain.get("interfaces") or []
    interface_ids = [i["id"] for i in interfaces]
    interface_set = set(interface_ids)
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
                       ("interface", interface_ids),
                       ("domain class", class_ids),
                       ("relation", [r["id"] for r in relations]),
                       ("task", task_ids),
                       ("app concept", [c["id"] for c in concepts])):
        seen = set()
        for i in ids:
            if i in seen:
                errors.append(f"duplicate {label} id: {i}")
            seen.add(i)

    # interfaces support composition over brittle deep hierarchies
    for i in interfaces:
        if not i.get("definition"):
            warnings.append(f"interface {i['id']} has no definition")
        if not i.get("source"):
            warnings.append(f"interface {i['id']} has no source evidence")
        for ref in i.get("applies_to") or []:
            if ref not in class_set:
                errors.append(f"interface {i['id']}: applies_to '{ref}' not a domain class")

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
        for iface in c.get("implements") or []:
            if iface not in interface_set:
                errors.append(f"domain class {c['id']}: implements unknown interface '{iface}'")
        if c["id"] in VAGUE_CLASS_IDS:
            warnings.append(
                f"domain class {c['id']} uses a vague name — confirm it is the real business term, "
                "otherwise prefer a precise semantic label or an interface"
            )

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

        fields = c.get("fields") or []
        if len(fields) > 30:
            warnings.append(
                f"app concept {c['id']} has {len(fields)} fields — inspect for Kitchen Sink/God Object modeling"
            )
        technical_fields = [field_name(f) for f in fields if looks_technical_field(field_name(f))]
        if len(technical_fields) >= 3:
            warnings.append(
                f"app concept {c['id']} maps {len(technical_fields)} technical-looking fields "
                f"({', '.join(technical_fields[:5])}) — keep ETL/source metadata in L3 unless domain-relevant"
            )

    # Foundry-style anti-pattern heuristics: warnings mean inspect, not necessarily fail.
    silo_groups = {}
    for cid in class_ids:
        core = normalised_business_noun(cid)
        if core and core != cid.lower():
            silo_groups.setdefault(core, []).append(cid)
    for core, ids in sorted(silo_groups.items()):
        if len(ids) >= 2:
            warnings.append(
                f"possible department/system silo for '{core}': {', '.join(ids)} — prefer one L1 class plus L3 bindings"
            )

    property_update_groups = {}
    for t in tasks:
        words = split_identifier(t["id"])
        if words and words[0] in {"update", "set", "change"} and any(w in PROPERTY_UPDATE_WORDS for w in words[1:]):
            key = (tuple(t.get("actor_roles") or []), tuple(t.get("inputs") or []), tuple(t.get("outputs") or []))
            property_update_groups.setdefault(key, []).append(t["id"])
    for ids in property_update_groups.values():
        if len(ids) >= 3:
            warnings.append(
                f"possible action sprawl: {', '.join(ids)} — consider one business-level update task with clear preconditions"
            )

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
    try:
        args.fn(args)
    except (WorkspaceError, OSError, UnicodeError) as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
