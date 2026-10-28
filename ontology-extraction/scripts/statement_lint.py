#!/usr/bin/env python3
"""Advisory wording checks for rule, unknown, and conflict statements.

Heuristics in the style of ASD-STE100 Simplified Technical English plus a short
ambiguity list. Warnings never block a build and never change a statement.
Rewording a statement is a new interpretation: re-read its cited evidence after.
This does not certify ASD-STE100 compliance.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re

import domain_skill as ds
from policy_check import DomainError, MAX_TOTAL, canonical, parse_json, require, safe_file

COLLECTIONS = ("rules", "unknowns", "conflicts")
LAYERS = ("20-domain.yaml", "30-task.yaml", "40-application.yaml")
MAX_WORDS = 25
HELPING = {"should", "would", "may", "might", "shall", "ought"}
VAGUE = (
    "appropriate", "appropriately", "reasonable", "reasonably", "sufficient", "sufficiently",
    "adequate", "adequately", "timely", "properly", "relevant", "usually", "normally",
    "generally", "typically", "often", "several", "various", "etc", "as needed",
    "if necessary", "as necessary", "where applicable", "if applicable",
)
CODE = re.compile(r"`[^`\n]+`")
QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'’.-]*")
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"])")
CONTRACTION = re.compile(r"\b\w+n['’]t\b|\b\w+['’](?:re|ll|ve|m|d)\b|\b(?:it|that|there|what)['’]s\b", re.I)
PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being)\s+(?:not\s+|\w+ly\s+)?"
    r"(?:\w+ed|\w+en|done|made|set|put|kept|held|found|sent|shown|told|built|paid|"
    r"read|run|seen|given|taken|known|left|lost|met|sold|spent|split|won)\b", re.I)
# Allowed by the writing rules even where a word list omits them.
ALWAYS_APPROVED = {"a"}
NOT_PARTICIPLE = {"open", "even", "often", "seven", "eleven", "token", "hidden"}
WORD_LINE = re.compile(r"^([A-Z][A-Z'’ -]*[A-Z]|[A-Z])\s+\(([a-z, ]+)\)(?:\s+\[([^\]]*)\])?\s*$")


def words(sentence: str) -> list[str]:
    """Code spans, quotations and identifiers each count as one word."""
    return TOKEN.findall(QUOTED.sub(" QUOTED ", CODE.sub(" CODE ", sentence)))


def name_parts(identifier: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", identifier)
    parts = {p.lower() for p in re.split(r"[\s_.\-]+", spaced) if p}
    return parts | {identifier.lower()}


def load_word_list(path: Path) -> set[str]:
    """Read lines such as `ACCEPT (v) [ACCEPTS, ACCEPTED]`. Supply your own copy."""
    approved = set()
    data = path.read_bytes()
    require(len(data) <= MAX_TOTAL, "word list too large")
    for line in data.decode("utf-8").splitlines():
        match = WORD_LINE.match(line.strip())
        if not match:
            continue
        head, parts, forms = match.groups()
        approved.update(w.lower() for w in head.split())
        for form in (forms or "").split(","):
            approved.update(w.lower() for w in form.split())
        if "n" in [p.strip() for p in parts.split(",")]:
            word = head.lower()
            approved.add(word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s"))
    require(approved, "no word-list entries recognised; expected `WORD (pos) [FORMS]` lines")
    return approved


def load(target: Path) -> tuple[dict, set[str], list[str]]:
    """Contract and technical names from a compiled skill or an authoring workspace."""
    notes = []
    if (target / "model.json").is_file():
        model = parse_json(safe_file(target, "model.json", MAX_TOTAL))
        require(type(model) is dict and type(model.get("layers")) is dict, "invalid model")
        contract, layers = model, model["layers"]
    else:
        contract = parse_json(safe_file(target, "60-contract.json", MAX_TOTAL))
        require(type(contract) is dict, "invalid contract")
        layers = {}
        for name in LAYERS:
            try:
                layers[name] = ds.yaml_document(safe_file(target, name), name)
            except DomainError:
                notes.append(f"{name} unreadable; its names are not technical words here.")
    names = set()

    def collect(items: object, fields: tuple[str, ...]) -> None:
        for item in items if type(items) is list else []:
            for field in fields:
                value = item.get(field) if type(item) is dict else None
                if type(value) is str:
                    names.update(name_parts(value))

    domain = layers.get("20-domain.yaml") or {}
    collect(domain.get("classes"), ("id",))
    collect(domain.get("interfaces"), ("id",))
    collect(domain.get("relations"), ("id",))
    collect((layers.get("30-task.yaml") or {}).get("tasks"), ("id",))
    collect((layers.get("40-application.yaml") or {}).get("concepts"), ("id",))
    inputs = contract.get("inputs")
    for task, fields in (inputs.items() if type(inputs) is dict else []):
        names.update(name_parts(task))
        for field in fields if type(fields) is dict else []:
            names.update(name_parts(field))
    return contract, names, notes


def check(statement: str, approved: set[str] | None, technical: set[str]) -> list[dict]:
    found = []

    def warn(code: str, category: str, text: str, advice: str) -> None:
        found.append({"code": code, "category": category, "text": text, "advice": advice})

    plain = QUOTED.sub(" ", CODE.sub(" ", statement))
    sentences = [s for s in SENTENCE.split(statement.strip()) if words(s)]
    if len(sentences) > 1:
        warn("MORE_THAN_ONE_SENTENCE", "ste", f"{len(sentences)} sentences",
             "Give one rule per statement, or one unknown per statement.")
    for sentence in sentences:
        count = len(words(sentence))
        if count > MAX_WORDS:
            warn("SENTENCE_TOO_LONG", "ste", f"{count} words",
                 f"Use at most {MAX_WORDS} words. Divide the condition into more rules.")
    if ";" in plain:
        warn("SEMICOLON", "ste", ";", "Write two statements. A semicolon often joins two rules.")
    for token in sorted({t.lower() for t in TOKEN.findall(plain)} & HELPING):
        warn("HELPING_VERB", "ste", token,
             "Use must, can, or will. 'may' and 'should' can mean permission, possibility, or advice.")
    for match in CONTRACTION.finditer(plain):
        warn("CONTRACTION", "ste", match.group(0), "Write the full words.")
    for match in PASSIVE.finditer(plain):
        if match.group(0).split()[-1].lower() not in NOT_PARTICIPLE:
            warn("PASSIVE_VOICE", "ste", match.group(0),
                 "Name who or what does the action, in the active voice.")
    if re.search(r"\band\s*/\s*or\b", plain, re.I):
        warn("AND_OR", "ambiguity", "and/or", "Say 'A, B, or both', or write two rules.")
    lowered = plain.lower()
    for term in VAGUE:
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            warn("VAGUE_TERM", "ambiguity", term, "Name the exact value, condition, or limit.")
    if approved is not None:
        # Identifiers (with _) are technical names; each part of a hyphenated group is checked.
        parts = {p.strip(".'’") for t in TOKEN.findall(plain.lower()) if "_" not in t
                 for p in t.split("-")}
        unknown = sorted(t for t in parts if t and not t[0].isdigit() and
                         t not in approved | ALWAYS_APPROVED and t not in technical)
        for token in unknown:
            warn("NOT_IN_WORD_LIST", "ste", token,
                 "Use an approved word, or add it with --technical-word if it is a technical name.")
    return found


def lint(target: Path, word_list: Path | None = None, technical: list[str] | None = None) -> dict:
    contract, names, notes = load(target)
    names.update(w.lower() for w in technical or [])
    approved = load_word_list(word_list) if word_list else None
    warnings, checked = [], 0
    for collection in COLLECTIONS:
        items = contract.get(collection, [])
        require(type(items) is list, f"invalid {collection}")
        for item in items:
            if type(item) is not dict or type(item.get("statement")) is not str:
                continue
            checked += 1
            for warning in check(item["statement"], approved, names):
                warnings.append({"collection": collection, "id": item.get("id"), **warning})
    return {
        "schema_version": 1, "kind": "STATEMENT_WORDING_ADVICE",
        "status": "ADVISORY_WARNINGS" if warnings else "NO_WARNINGS",
        "blocking": False, "statements_checked": checked, "warnings": warnings,
        "word_list_check": "RUN" if approved is not None else "NOT_RUN",
        "notes": [*notes,
                  "Heuristic checks. They do not certify ASD-STE100 compliance.",
                  "Rewording changes the interpretation: re-read the cited evidence after each edit.",
                  "Do not reword a statement to hide an unknown, a conflict, or an inferred rule."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="authoring workspace or compiled skill")
    parser.add_argument("--word-list", type=Path,
                        help="your own ASD-STE100 word list (`WORD (pos) [FORMS]` lines)")
    parser.add_argument("--technical-word", action="append", default=[],
                        help="extra technical name or technical verb; repeat as needed")
    args = parser.parse_args()
    try:
        print(canonical(lint(args.target, args.word_list, args.technical_word)).decode(), end="")
        return 0
    except (DomainError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
