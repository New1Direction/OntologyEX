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

import business_view
import decisions
import decide as decision_model
import evidence_grade
from policy_check import DomainError, require

KINDS = ("declared_service", "source_module", "api_route", "declared_db_table")
STRENGTH = ("STRONG", "MODERATE", "WEAK")
MAX_PER_BINDING = 5
MAX_ROUTE_OPTIONS = 254          # Jev allows 255 choices; one is "none"
MODEL_MIN_PROBABILITY = 0.5
MAX_CANDIDATES = 4096
PROPOSAL_KIND = "ONTOLOGYEX_AGENT_LINK_PROPOSAL"
MAX_PROPOSALS = 2000
MAX_PROPOSAL_BYTES = 4 * 1024 * 1024
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
    return decisions.decision_id(skill, binding, component)


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


def valid_proposal(row) -> bool:
    """One line of an agent proposals file: who proposed which link, for whom, and why."""
    fields = {"kind": 40, "id": 64, "at": 40, "proposed_by": 128, "on_behalf_of": 128,
              "skill": 200, "binding": 200, "component": 1024, "reason": 500}
    return (type(row) is dict and set(row) == set(fields) and row["kind"] == PROPOSAL_KIND
            and all(type(row[k]) is str and 0 < len(row[k]) <= n for k, n in fields.items())
            and row["proposed_by"].startswith("agent:")
            and row["id"] == suggestion_id(row["skill"], row["binding"], row["component"]))


def read_proposals(path: Path | None) -> list[dict]:
    """Agent link proposals, oldest first. Lines that are torn or malformed are skipped, never trusted."""
    if path is None or not path.is_file() or path.is_symlink():
        return []
    with path.open("rb") as handle:  # a long file is read from its newest end, never refused whole
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - MAX_PROPOSAL_BYTES))
        text = handle.read(MAX_PROPOSAL_BYTES).decode("utf-8", "replace")
    lines = text.splitlines()[1 if size > MAX_PROPOSAL_BYTES else 0:]
    rows = []
    for line in lines[-MAX_PROPOSALS:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if valid_proposal(row):
            rows.append(row)
    return rows


def append_proposal(path: Path, row: dict) -> None:
    """Append one proposal line. Append-only: nothing here rewrites or removes a line."""
    require(valid_proposal(row), "invalid agent proposal")
    require(not path.is_symlink(), "agent proposals path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def merge_proposals(result: dict, data: dict, proposals: list[dict]) -> dict:
    """Show agent proposals beside the ranked suggestions, as suggestions a person decides.

    A proposal adds a WEAK signal with the agent's stated reason; it never raises a
    suggestion's strength and never links anything. A proposal for a binding or
    component that does not exist, a link already confirmed, or one a person rejected
    is not shown: rejections stay sticky whoever proposes again.
    """
    bindings = {(b["skill"], b["binding"]) for b in data["bindings"]}
    components = {c["id"]: c for c in data["components"]}
    linked = {tuple(x) for x in data["linked"]}
    rejected = {tuple(x) for x in data["rejected"]}
    rows = {(r["skill"], r["binding"], r["component"]): r for r in result["suggestions"]}
    added, shown, hidden, counted = [], 0, 0, {}
    for proposal in proposals:
        key = (proposal["skill"], proposal["binding"], proposal["component"])
        if key[:2] not in bindings or key[2] not in components or key in linked or key in rejected:
            hidden += 1
            continue
        counted[key] = counted.get(key, 0) + 1
        if counted[key] > 3:
            continue  # the same link proposed again and again adds nothing a person needs to read
        row = rows.get(key)
        if row is None:
            c = components[key[2]]
            row = rows[key] = {"id": suggestion_id(*key), "skill": key[0], "binding": key[1],
                               "component": c["id"], "component_label": c["label"], "component_kind": c["kind"],
                               "strength": "WEAK", "signals": [], "status": "PROPOSED",
                               "proposed_by": proposal["proposed_by"]}
            added.append(row)
        else:
            row = rows[key] = {**row, "signals": list(row["signals"])}
        row["signals"].append({"signal": "AGENT_PROPOSAL", "strength": "WEAK",
                               "detail": f"{proposal['proposed_by']} for {proposal['on_behalf_of']}: {proposal['reason']}"})
        shown += 1
    ranked = [rows[(r["skill"], r["binding"], r["component"])] for r in result["suggestions"]]
    return {**result, "suggestions": ranked + added,
            "agent_proposals": {"shown": shown, "new_suggestions": len(added), "not_shown": hidden}}


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


def link_key(row: dict) -> tuple[str, str, str]:
    return (row["skill"], row["binding"], row["component"])


def apply_decisions(links_path: Path, by: str, note: str = "", confirm: list[dict] = (),
                    reject: list[dict] = (), undo: list[str] = ()) -> dict:
    """Confirm or reject proposed suggestions and undo decided links, in one atomic write."""
    for suggestion in (*confirm, *reject):
        require(suggestion.get("status") == "PROPOSED", "only a proposed suggestion can be decided")
    row = lambda s: {"skill": s["skill"], "binding": s["binding"], "component": s["component"]}
    return decisions.apply(links_path, read=business_view.read_link_document, kind=business_view.LINK_KIND,
                           confirmed_field="links", key=link_key, name="links",
                           max_rows=business_view.MAX_LINKS, by=by, note=note,
                           confirm=[row(s) for s in confirm], reject=[row(s) for s in reject], undo=list(undo))


def decide(links_path: Path, suggestion: dict, decision: str, by: str, note: str = "") -> dict:
    """Append one confirmed link or one rejection. Atomic and idempotent."""
    require(decision in ("confirm", "reject"), "decision must be confirm or reject")
    result = apply_decisions(links_path, by, note, **{decision: [suggestion]})
    if not result["applied"]:
        return {"status": "UNCHANGED", "decision": decision, "link": result["unchanged"][0]}
    return {"status": "CONFIRMED" if decision == "confirm" else "REJECTED",
            "decision": decision, "link": result["applied"][0]["row"], "authenticated": False}


def main() -> int:
    import control_room
    import discover_repo

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("suggest", "confirm", "reject", "undo"):
        p = commands.add_parser(name)
        p.add_argument("--repo", type=Path, required=True)
        p.add_argument("--skill", type=Path, action="append", required=True)
        p.add_argument("--links", type=Path, required=True)
        p.add_argument("--model-assist", action="store_true",
                       help="also ask the configured decision model (sends binding source to it)")
        if name != "suggest":
            p.add_argument("--id", action="append", default=[],
                           help="suggestion ID from `suggest` (or, for undo, a decided link's ID); repeatable")
            p.add_argument("--by", required=True, help="who decided (not verified)")
            p.add_argument("--note", default="")
        if name in ("confirm", "reject"):
            p.add_argument("--all-strong", action="store_true", help="decide every STRONG suggestion")
    args = parser.parse_args()
    try:
        state = control_room.build_state(discover_repo.discover(args.repo), [], {})
        links, rejected = (business_view.read_link_document(args.links)
                           if args.links.exists() else ([], []))
        client = decision_model.DecisionClient() if args.model_assist else None
        result = suggest(args.skill, state, links, rejected, client=client)
        if args.command == "undo":
            require(args.id, "pass --id for each decided link to undo (see `suggest`'s decided list)")
            result = apply_decisions(args.links, args.by, args.note, undo=args.id)
        elif args.command != "suggest":
            chosen = decisions.select(result["suggestions"], args.id, args.all_strong)
            result = apply_decisions(args.links, args.by, args.note, **{args.command: chosen})
        else:
            result["decided"] = decisions.decided(links, rejected, link_key)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (DomainError, decision_model.DecisionError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
