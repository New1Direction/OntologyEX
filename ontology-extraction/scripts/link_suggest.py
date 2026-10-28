#!/usr/bin/env python3
"""Suggest binding-to-component links from names and paths; a person confirms each one.

Suggestions are deterministic and explained by their signals. A suggestion is never a
link: only an explicit confirm decision appends it to the links file, and a reject
decision keeps it from being suggested again. Strength is ordinal, not a probability.
The Rust engine implements the same matcher (`ontologyex-view suggest-links`).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

import business_view
import decide as decision_model
import evidence_grade
from policy_check import DomainError, MAX_TOTAL, canonical, digest, parse_json, require, safe_file

KINDS = ("declared_service", "source_module", "api_route", "declared_db_table")
STRENGTH = ("STRONG", "MODERATE", "WEAK")
MAX_PER_BINDING = 5
MAX_ROUTE_OPTIONS = 254          # Jev allows 255 choices; one is "none"
MODEL_MIN_PROBABILITY = 0.5
MAX_CANDIDATES = 4096
STOPWORDS = frozenset({
    "the", "and", "for", "get", "set", "apply", "type", "class", "def", "func", "function",
    "impl", "src", "lib", "libs", "app", "apps", "main", "index", "init", "core", "common",
    "util", "utils", "module", "modules", "test", "tests", "spec", "handler", "handlers",
    "model", "models", "domain", "code", "py", "js", "ts", "tsx", "jsx", "mjs", "go", "rs",
    "java", "rb", "yml", "yaml", "json", "toml", "compose", "docker", "dockerfile", "package",
})
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(text: str) -> list[str]:
    """ASCII-only, camelCase-aware word set. Identical rules in Rust."""
    ascii_text = "".join(c if c.isascii() else " " for c in text)
    words = SPLIT.split(CAMEL.sub(" ", ascii_text).lower())
    return sorted({w for w in words if len(w) >= 2 and w not in STOPWORDS})


def segments(path: str) -> list[str]:
    """Directory names and the file stem of a relative path, normalized like labels."""
    parts = PurePosixPath(path).parts
    names = list(parts[:-1]) + ([PurePosixPath(parts[-1]).stem] if parts else [])
    return [" ".join(tokens(p)) for p in names if tokens(p)]


def same_file(a: str, b: str) -> bool:
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def prepare(skills: list[Path], state: dict, links: list[dict], rejected: list[dict]) -> dict:
    """Normalized matcher input shared by the Python and Rust implementations."""
    evidence_paths = {e["id"]: e.get("path", "") for e in state.get("source_evidence", [])}
    bindings = []
    for path in skills:
        _, model = evidence_grade.load_skill(path)
        records = {e["id"]: e for e in model["evidence"]}
        application = model["layers"]["40-application.yaml"]
        system = application.get("system", "")
        for concept in application.get("concepts", []):
            source = records.get(concept.get("source"))
            bindings.append({
                "skill": model["name"], "binding": concept["id"],
                "binds": concept.get("binds", ""), "system": system if type(system) is str else "",
                "path": source["path"] if source else "",
            })
    components = [{"id": n["id"], "label": n["label"], "kind": n["kind"],
                   "path": evidence_paths.get(n.get("evidence_id"), "")}
                  for n in state["nodes"] if n["kind"] in KINDS]
    require(len(components) <= MAX_CANDIDATES, "too many candidate components")
    pairs = lambda rows: sorted({(r["skill"], r["binding"], r["component"]) for r in rows})
    return {"schema_version": 1, "kind": "LINK_MATCH_INPUT",
            "bindings": sorted(bindings, key=lambda b: (b["skill"], b["binding"])),
            "components": sorted(components, key=lambda c: c["id"]),
            "linked": [list(p) for p in pairs(links)],
            "rejected": [list(p) for p in pairs(rejected)],
            "execution_authorized": False}


def suggestion_id(skill: str, binding: str, component: str) -> str:
    return digest(canonical([skill, binding, component]))[:24]


def suggest_from(data: dict) -> dict:
    """Pure matcher over prepared input. Deterministic; no ties are broken by guessing."""
    require(data.get("kind") == "LINK_MATCH_INPUT" and data.get("execution_authorized") is False,
            "expected link match input that authorizes nothing")
    require(all(c["kind"] in KINDS for c in data["components"]), "component kind cannot be linked")
    linked = {tuple(p) for p in data["linked"]}
    rejected = {tuple(p) for p in data["rejected"]}
    out, skipped = [], 0
    for b in data["bindings"]:
        b_tokens = set(tokens(b["binding"])) | set(tokens(b["binds"])) | set(tokens(b["system"]))
        b_tokens |= {t for seg in segments(b["path"]) for t in seg.split()}
        b_segments = set(segments(b["path"]))
        system_tokens = set(tokens(b["system"]))
        found = []
        for c in data["components"]:
            key = (b["skill"], b["binding"], c["id"])
            if key in linked:
                continue
            label = " ".join(tokens(c["label"]))
            label_tokens = set(label.split()) if label else set()
            signals = []
            if b["path"] and c["path"] and c["kind"] != "declared_service" and same_file(b["path"], c["path"]):
                signals.append({"signal": "SAME_SOURCE_FILE", "strength": "STRONG",
                                "detail": f"{b['path']} is {c['path']}"})
            if label and label in b_segments:
                signals.append({"signal": "PATH_NAMES_COMPONENT", "strength": "MODERATE",
                                "detail": f"{b['path']} has a part named {c['label']}"})
            if label_tokens and label_tokens <= system_tokens:
                signals.append({"signal": "SYSTEM_NAMES_COMPONENT", "strength": "MODERATE",
                                "detail": f"system {b['system']} names {c['label']}"})
            shared = sorted(label_tokens & b_tokens)
            if shared:
                signals.append({"signal": "SHARED_NAME", "strength": "WEAK",
                                "detail": "shared words: " + ", ".join(shared)})
            if not signals:
                continue
            if key in rejected:
                skipped += 1
                continue
            strength = min((s["strength"] for s in signals), key=STRENGTH.index)
            found.append({"id": suggestion_id(*key), "skill": b["skill"], "binding": b["binding"],
                          "component": c["id"], "component_label": c["label"],
                          "component_kind": c["kind"], "strength": strength,
                          "signals": signals, "status": "PROPOSED"})
        found.sort(key=lambda s: (STRENGTH.index(s["strength"]), -len(s["signals"]), s["component"]))
        out.extend(found[:MAX_PER_BINDING])
    return {"schema_version": 1, "kind": "LINK_SUGGESTIONS", "suggestions": out,
            "rejected_not_shown": skipped,
            "semantics": "NAME_AND_PATH_SIGNALS_NOT_RUNTIME_EVIDENCE_CONFIRMATION_REQUIRED",
            "auto_applied": 0, "execution_authorized": False}


def binding_context(skills: list[Path]) -> dict[tuple[str, str], str]:
    """What a decision model is told about each code binding: its role and its source."""
    context = {}
    for path in skills:
        _, model = evidence_grade.load_skill(path)
        records = {e["id"]: e for e in model["evidence"]}
        classes = {c["id"]: c for c in model["layers"]["20-domain.yaml"].get("classes", [])}
        application = model["layers"]["40-application.yaml"]
        for concept in application.get("concepts", []):
            source = records.get(concept.get("source"), {})
            cls = classes.get(concept.get("binds"), {})
            lines = [f"Code binding: {concept['id']} ({concept.get('kind', 'unknown kind')})",
                     f"Business concept: {concept.get('binds', '')}: {cls.get('definition', '')}",
                     f"Used by tasks: {', '.join(concept.get('used_by_tasks', []))}",
                     f"System: {application.get('system', '')}"]
            if source:
                lines += [f"Source {source['path']} lines {source['start_line']}-{source['end_line']}:",
                          source.get("quote", "")[:2000]]
            context[(model["name"], concept["id"])] = "\n".join(lines)
    return context


def model_routes(data: dict, context: dict, client) -> list[dict]:
    """One pick-one question per binding: which candidate component runs this code?"""
    linked = {tuple(x) for x in data["linked"]}
    rejected = {tuple(x) for x in data["rejected"]}
    asks, keys = [], []
    for b in data["bindings"]:
        candidates = [c for c in data["components"]
                      if (b["skill"], b["binding"], c["id"]) not in linked | rejected][:MAX_ROUTE_OPTIONS]
        if not candidates:
            continue
        criteria = {f"c{i}": f"{c['label']} ({c['kind']}, declared in {c['path'] or 'unknown file'})"
                    for i, c in enumerate(candidates)}
        criteria["none"] = "None of these components runs or serves this code."
        state = context.get((b["skill"], b["binding"]), f"Code binding: {b['binding']}")
        asks.append((state, {"route": {
            "type": "choice", "criteria": criteria,
            "instructions": "Which listed service, module, route, or table runs or serves this "
                            "code binding? Answer none if no option fits."}}))
        keys.append((b, candidates))
    rows = []
    for (b, candidates), answer in zip(keys, client.decide_many(asks)):
        route = answer["route"]
        if route["choice"] == "none":
            continue
        probability = route["probabilities"].get(route["choice"], route["confidence"])
        if probability < MODEL_MIN_PROBABILITY:
            continue
        c = candidates[int(route["choice"][1:])]
        rows.append({"skill": b["skill"], "binding": b["binding"], "component": c,
                     "signal": {"signal": "MODEL_ROUTED",
                                "strength": "MODERATE" if probability >= 0.8 else "WEAK",
                                "detail": f"{client.label} chose {c['label']} "
                                          f"(probability {probability:.2f})",
                                "probability": round(probability, 4)}})
    return rows


def merge_model_routes(result: dict, routes: list[dict]) -> dict:
    """Add model signals to matching suggestions, or new suggestions; still proposals only."""
    rows = {(r["skill"], r["binding"], r["component"]): r for r in result["suggestions"]}
    for route in routes:
        c = route["component"]
        key = (route["skill"], route["binding"], c["id"])
        row = rows.get(key)
        if row is None:
            row = rows[key] = {"id": suggestion_id(*key), "skill": key[0], "binding": key[1],
                               "component": c["id"], "component_label": c["label"],
                               "component_kind": c["kind"], "strength": "WEAK",
                               "signals": [], "status": "PROPOSED"}
        row["signals"] = [*row["signals"], route["signal"]]
        row["strength"] = min((s["strength"] for s in row["signals"]), key=STRENGTH.index)
    ordered = sorted(rows.values(), key=lambda r: (r["skill"], r["binding"], STRENGTH.index(r["strength"]),
                                                    -len(r["signals"]), r["component"]))
    out, per = [], {}
    for row in ordered:
        n = per.get((row["skill"], row["binding"]), 0)
        if n < MAX_PER_BINDING:
            out.append(row)
            per[(row["skill"], row["binding"])] = n + 1
    return {**result, "suggestions": out}


def suggest(skills: list[Path], state: dict, links: list[dict], rejected: list[dict],
            native=None, client=None) -> dict:
    """`native` runs the Rust matcher; `client` (decide.DecisionClient) adds model routes."""
    data = prepare(skills, state, links, rejected)
    result = native.execute(data, "suggest-links") if native is not None else suggest_from(data)
    if client is None:
        return result
    try:
        routes = model_routes(data, binding_context(skills), client)
    except decision_model.DecisionError as exc:
        return {**result, "model_assist": {"status": "ERROR", "detail": str(exc)[:300]}}
    return {**merge_model_routes(result, routes),
            "model_assist": {**decision_model.describe(client), "routes_proposed": len(routes)}}


def decide(links_path: Path, suggestion: dict, decision: str, by: str, note: str = "") -> dict:
    """Append one confirmed link or one rejection. Atomic, append-only, idempotent."""
    require(decision in ("confirm", "reject"), "decision must be confirm or reject")
    require(type(by) is str and 0 < len(by.strip()) <= 120, "a decision needs the deciding person")
    require(type(note) is str and len(note) <= 500, "invalid note")
    require(suggestion.get("status") == "PROPOSED", "only a proposed suggestion can be decided")
    target = links_path.absolute()
    if target.exists():
        before = safe_file(target.parent, target.name, MAX_TOTAL)
        links, rejected = business_view.read_link_document(target)
    else:
        before, links, rejected = None, [], []
    row = {"skill": suggestion["skill"], "binding": suggestion["binding"],
           "component": suggestion["component"], "declared_by": by.strip(), "note": note}
    key = (row["skill"], row["binding"], row["component"])
    existing = {(r["skill"], r["binding"], r["component"]) for r in links + rejected}
    if key in existing:
        return {"status": "UNCHANGED", "decision": decision, "link": row}
    if decision == "confirm":
        links = [*links, row]
    else:
        rejected = [*rejected, row]
    document = {"schema_version": 1, "kind": business_view.LINK_KIND, "links": links}
    if rejected:
        document["rejected"] = rejected
    data = canonical(document)
    require(len(data) <= MAX_TOTAL, "links byte budget exceeded")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".links-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        now = safe_file(target.parent, target.name, MAX_TOTAL) if target.exists() else None
        require(now == before, "links file changed during write")
        temporary.replace(target)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    business_view.read_link_document(target)  # the written file must load back
    return {"status": "CONFIRMED" if decision == "confirm" else "REJECTED",
            "decision": decision, "link": row, "authenticated": False}


def main() -> int:
    import control_room
    import discover_repo

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("suggest", "confirm", "reject"):
        p = commands.add_parser(name)
        p.add_argument("--repo", type=Path, required=True)
        p.add_argument("--skill", type=Path, action="append", required=True)
        p.add_argument("--links", type=Path, required=True)
        p.add_argument("--model-assist", action="store_true",
                       help="also ask the configured decision model (sends binding source to it)")
        if name != "suggest":
            p.add_argument("--id", required=True, help="suggestion ID from `suggest`")
            p.add_argument("--by", required=True, help="who decided (not verified)")
            p.add_argument("--note", default="")
    args = parser.parse_args()
    try:
        state = control_room.build_state(discover_repo.discover(args.repo), [], {})
        links, rejected = (business_view.read_link_document(args.links)
                           if args.links.exists() else ([], []))
        client = decision_model.DecisionClient() if args.model_assist else None
        result = suggest(args.skill, state, links, rejected, client=client)
        if args.command != "suggest":
            match = [s for s in result["suggestions"] if s["id"] == args.id]
            require(len(match) == 1, "no current suggestion has that ID; run suggest again")
            result = decide(args.links, match[0], args.command, args.by, args.note)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DomainError, decision_model.DecisionError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
