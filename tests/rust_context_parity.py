#!/usr/bin/env python3
"""Rust `ontologyex-view context|explain|search` against the Python reference (entity_context.py).

Runs after `cargo build --bin ontologyex-view`. Two kinds of input: the bundle the
control room builds for the operating-picture demo (synthetic data, every entity and
a sample of entity pairs), and seeded random bundles with duplicate links, history
rows, gaps, unicode labels and missing fields. No network and no production data.
"""
from __future__ import annotations

import importlib.util
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import entity_context

BINARY = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "engine/target/debug/ontologyex-view"


def native(bundle: dict, *args: str):
    run = subprocess.run([str(BINARY), *args], input=json.dumps(bundle).encode(), capture_output=True, timeout=30)
    if run.returncode != 0:
        return {"refused": True}
    return json.loads(run.stdout)


def python(fn, *args):
    try:
        return fn(*args)
    except entity_context.ContextError:
        return {"refused": True}


def same(label: str, expected, actual) -> None:
    if expected != actual:
        for key in sorted(set(expected) | set(actual)) if isinstance(expected, dict) and isinstance(actual, dict) else []:
            if expected.get(key) != actual.get(key):
                raise AssertionError(f"{label}: {key} differs\n python {json.dumps(expected.get(key))[:600]}\n"
                                     f" rust   {json.dumps(actual.get(key))[:600]}")
        raise AssertionError(f"{label}: outputs differ")


def demo_bundle() -> dict:
    spec = importlib.util.spec_from_file_location("ontologyex_context_demo", ROOT / "examples/operating-picture/demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    with tempfile.TemporaryDirectory(prefix="ontologyex-context-parity-") as tmp:
        built = demo.build(Path(tmp) / "demo")
        history = Path(tmp) / "demo" / "links.history.jsonl"
        history.write_text(json.dumps({"at": "2026-10-28T09:00:00Z", "by": "Dana", "action": "confirm",
                                       "note": "handler", "row": {"skill": "payments-domain", "binding": "payments.Payment",
                                                                  "component": "discovery:service:compose.yml:api"}}) + "\n")
        run = subprocess.run([*built["command"], "--bundle-only"], capture_output=True, text=True, check=True, timeout=120)
        return json.loads(run.stdout)


WORDS = ["api", "db", "Payment", "Refund", "ledger", "é", "支付", "x", "skill", "binding", "None"]


def word(rng: random.Random) -> str:
    return rng.choice(WORDS) + str(rng.randrange(4))


def random_bundle(rng: random.Random) -> dict:
    nodes = [{"id": f"svc:{word(rng)}{i}", "kind": rng.choice(["declared_service", "api_route", "project"]),
              "label": word(rng), "status": rng.choice(["ok", "error", "unknown"]),
              **({"evidence_id": f"e{rng.randrange(3)}"} if rng.random() < 0.8 else {}),
              **({"observed_at": "2026-10-28T10:00:00Z"} if rng.random() < 0.3 else {})} for i in range(rng.randrange(1, 9))]
    ids = [n["id"] for n in nodes]
    edges = [{"source": rng.choice(ids), "target": rng.choice(ids),
              "relation": rng.choice(["DEPENDS_ON", "DECLARES", "CALLS"]),
              **({"evidence_id": f"e{rng.randrange(3)}"} if rng.random() < 0.7 else {}),
              **({"state": "OBSERVED_IN_TRACES_CONFIRMED_BY_PERSON"} if rng.random() < 0.2 else {})}
             for _ in range(rng.randrange(0, 12))]
    skill = rng.choice(["s", "pay"])
    actions, objects, decided, suggestions, gaps, graph = [], [], [], [], [], []
    for i in range(rng.randrange(0, 4)):
        bindings = [{"id": f"b{j}", "components": rng.sample(ids, rng.randrange(0, min(3, len(ids)) + 1))}
                    for j in range(rng.randrange(0, 3))]
        actions.append({"key": f"{skill}:action:A{i}", "id": f"A{i}", "skill": skill, "health": "UNLINKED",
                        "verb_phrase": rng.choice(["", "take a payment", "réembourser"]), "bindings": bindings,
                        "rules": [{"id": "r1", "statement": "amount > 0", "status": "observed", "grade": "G1"}],
                        "blockers": [{"type": rng.choice(["unknown", "conflict", "inferred rule"]), "id": "u1",
                                      "statement": "who approves?"}] if rng.random() < 0.5 else [],
                        "evidence": [{"path": "req.md", "quote": "pay"}]})
        for b in bindings:
            for c in b["components"]:
                if rng.random() < 0.7:
                    decided.append({"skill": skill, "binding": b["id"], "component": c, "decision": "confirm",
                                    "declared_by": rng.choice(["Ana", None]), "note": "n"})
    for i in range(rng.randrange(0, 3)):
        objects.append({"key": f"{skill}:object:O{i}", "id": f"O{i}", "skill": skill, "health": "UNKNOWN",
                        "definition": "a thing", "bindings": [f"b{rng.randrange(3)}"],
                        "systems": rng.sample(ids, rng.randrange(0, min(2, len(ids)) + 1))})
        if actions:
            graph.append({"source": rng.choice(actions)["key"], "target": f"{skill}:object:O{i}",
                          "relation": rng.choice(["CONSUMES", "PRODUCES", "RELATION"]),
                          **({"label": "has"} if rng.random() < 0.5 else {})})
    for i in range(rng.randrange(0, 4)):
        suggestions.append({"id": f"x{i}", "skill": skill, "binding": f"b{rng.randrange(3)}",
                            "component": rng.choice(ids), "strength": rng.choice(["STRONG", "WEAK"]),
                            "signals": [{"signal": "SHARED_NAME", **({"detail": "shared words"} if rng.random() < 0.5 else {})}],
                            **({"proposed_by": "agent:claude"} if rng.random() < 0.3 else {})})
    if rng.random() < 0.4:
        gaps.append({"link": {"skill": skill, "binding": "b9", "component": rng.choice(ids + ["gone"])},
                     "reason": "LINK_COMPONENT_NOT_DISCOVERED"})
    history = [{"at": f"2026-10-2{rng.randrange(10)}T0{rng.randrange(10)}:00:00Z", "by": rng.choice(["Ana", "Bo"]),
                "action": rng.choice(["confirm", "reject", "undo", "reconfirm"]), "note": rng.choice(["", "ok"]),
                "row": {"skill": skill, "binding": f"b{rng.randrange(3)}", "component": rng.choice(ids)}}
               for _ in range(rng.randrange(0, 6))]
    dep_history = [{"at": "2026-10-27T00:00:00Z", "by": "Ana", "action": "confirm", "note": "",
                    "row": {"source": e["source"], "target": e["target"]}} for e in edges if e.get("state")]
    deps = [{"id": f"d{i}", "source": rng.choice(ids), "target": rng.choice(ids), "strength": "STRONG",
             "calls": rng.randrange(1, 90), "last_seen": "2026-10-28T11:00:00Z"} for i in range(rng.randrange(0, 3))]
    shas = ["a" * 64, "b" * 64]
    def evidence():
        return rng.choice([None, {}, {"compose.yml": rng.choice(shas)},
                           {"compose.yml": rng.choice(shas), "src/é.py": rng.choice(shas)}])
    watermarks = [{"id": f"d{i}", "skill": r["skill"], "binding": r["binding"], "component": r["component"],
                   "recorded": evidence(), "now": evidence() or {}} for i, r in enumerate(decided)
                  if rng.random() < 0.8]
    return {"state": {"execution_authorized": False, "nodes": nodes, "edges": edges,
                      "source_evidence": [{"id": f"e{i}", "path": f"f{i}.yml", "sha256": "ab", "source_kind": "MANIFEST",
                                           "authenticity": "UNVERIFIED"} for i in range(3)]},
            "business": {"actions": actions, "objects": objects, "graph": {"edges": graph}, "link_decided": decided,
                         "link_suggestions": {"suggestions": suggestions}, "link_gaps": gaps,
                         "link_watermarks": watermarks,
                         "dependency_suggestions": {"suggestions": deps}},
            "link_history": history, "dependency_history": dep_history,
            "agent_proposals": [{"skill": skill, "binding": f"b{rng.randrange(3)}", "component": rng.choice(ids),
                                 "proposed_by": rng.choice(["agent:claude", "agent:ci"])}
                                for _ in range(rng.randrange(0, 4))]}


def check(bundle: dict, label: str, rng: random.Random, pairs: int) -> int:
    ids = sorted(entity_context._entities(bundle))
    cases = 0
    for entity in ids + ["missing:entity"]:
        same(f"{label} context {entity}", python(entity_context.context, bundle, entity), native(bundle, "context", entity))
        cases += 1
    for _ in range(pairs if len(ids) > 1 else 0):
        a, b = rng.sample(ids, 2)
        same(f"{label} explain {a} {b}", python(entity_context.explain, bundle, a, b), native(bundle, "explain", a, b))
        cases += 1
    for query in ("api", "PAY", "é 0", "refund ledger", "bad\x01query", "None"):
        same(f"{label} search {query!r}", python(entity_context.search, bundle, query), native(bundle, "search", query))
        cases += 1
    return cases


def run() -> None:
    rng = random.Random(20261029)
    cases = check(demo_bundle(), "demo", rng, 25)
    for n in range(120):
        cases += check(random_bundle(rng), f"random#{n}", rng, 4)
    authorizing = random_bundle(rng)
    authorizing["state"]["execution_authorized"] = True
    assert native(authorizing, "context", authorizing["state"]["nodes"][0]["id"]) == {"refused": True}
    print(json.dumps({"cases": cases, "mismatches": 0}))


if __name__ == "__main__":
    run()
