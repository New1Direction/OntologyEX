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
import evidence_grade
from policy_check import DomainError, MAX_TOTAL, canonical, digest, parse_json, require, safe_file

KINDS = ("declared_service", "source_module", "api_route", "declared_db_table")
STRENGTH = ("STRONG", "MODERATE", "WEAK")
MAX_PER_BINDING = 5
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
            "rejected": [list(p) for p in pairs(rejected)]}


def suggestion_id(skill: str, binding: str, component: str) -> str:
    return digest(canonical([skill, binding, component]))[:24]


def suggest_from(data: dict) -> dict:
    """Pure matcher over prepared input. Deterministic; no ties are broken by guessing."""
    require(data.get("kind") == "LINK_MATCH_INPUT", "expected link match input")
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
            "auto_applied": 0}


def suggest(skills: list[Path], state: dict, links: list[dict], rejected: list[dict]) -> dict:
    return suggest_from(prepare(skills, state, links, rejected))


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
        if name != "suggest":
            p.add_argument("--id", required=True, help="suggestion ID from `suggest`")
            p.add_argument("--by", required=True, help="who decided (not verified)")
            p.add_argument("--note", default="")
    args = parser.parse_args()
    try:
        state = control_room.build_state(discover_repo.discover(args.repo), [], {})
        links, rejected = (business_view.read_link_document(args.links)
                           if args.links.exists() else ([], []))
        result = suggest(args.skill, state, links, rejected)
        if args.command != "suggest":
            match = [s for s in result["suggestions"] if s["id"] == args.id]
            require(len(match) == 1, "no current suggestion has that ID; run suggest again")
            result = decide(args.links, match[0], args.command, args.by, args.note)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DomainError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
