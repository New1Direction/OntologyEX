#!/usr/bin/env python3
"""Can the sources answer this? A pre-flight before an agent answers a question about the business.

    python answerable.py --bundle BUNDLE.json "who approves refunds over 1000 dollars?"

No model is called. The question's words are matched against what the model holds: the business
objects and actions (their names, definitions, rules and open questions), the systems, suppliers
and routes, the business's own records (payments, refunds, payouts, spend, tickets), who owns what,
and the compliance evidence. The answer says, for each term, what covers it and the lines that
back it; which terms nothing covers; open questions the sources record on what was matched (the
skill says "unknown" there); and whether the kind of source the question needs is present at all
(ownership needs a repository's history, money needs the business's records). Then a verdict:

- ANSWERABLE: the terms are covered, the sources the question needs are present, nothing open;
- PARTLY: some terms or sources are missing, or the sources record an open question there;
- NOT_IN_SOURCES: nothing in the model covers the question: say so instead of guessing.

Matching is by words (with light stemming and a short list of business synonyms), so ANSWERABLE
means the sources name what the question is about, not that the answer is in them; PARTLY and
NOT_IN_SOURCES are the useful part: they say where an answer would be a guess.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

import entity_context as ec

MAX_QUESTION = 500
MAX_MATCHES = 5
STOP = frozenset("""
a about above after again against all am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if
in into is it its itself just me more most my no nor not now of off on once only or other our out over own same she
should so some such than that the their them then there these they this those through to too under until up very was we
were what when where which while who whom why will with would you your yours tell show give list explain describe find
know happen happens happened get gets got make makes made need needs much many there's what's who's does there today
currently current also one two kind sort way thing things anything something everything someone anyone please
""".split())
SYNONYMS = {  # word -> the canonical word the model is likely to use (only where the meaning is the same)
    "reimburse": "refund", "reimbursement": "refund", "refunded": "refund",
    "charge": "payment", "charges": "payment", "transaction": "payment", "pay": "payment", "paid": "payment",
    "client": "customer", "buyer": "customer", "user": "customer", "users": "customer",
    "maintainer": "owner", "maintains": "owner", "owns": "owner", "own": "owner", "responsible": "owner",
    "rely": "depend", "relies": "depend", "reliant": "depend", "dependency": "depend", "dependencies": "depend",
    "uses": "depend", "calls": "depend",
    "settlement": "payout", "settlements": "payout", "deposit": "payout",
    "plan": "subscription", "recurring": "subscription", "churn": "subscription",
    "complaint": "ticket", "complaints": "ticket", "support": "ticket",
    "pii": "personal", "gdpr": "personal", "privacy": "personal",
    "outage": "error", "failing": "error", "fails": "error", "failure": "error", "failed": "error",
    "vendor": "supplier", "provider": "supplier", "vendors": "supplier", "providers": "supplier",
    "endpoint": "route", "endpoints": "route", "approves": "approve", "approval": "approve", "approved": "approve",
    "system": "service", "systems": "service", "component": "service", "components": "service",
    "database": "db", "databases": "db",
    "ad": "advertising", "ads": "advertising", "advert": "advertising", "adverts": "advertising",
}
SHORT = frozenset({"ad", "ai", "db", "hr", "ip", "ml", "qa", "ui"})  # two-letter words that are subjects
# Time words narrow a question about records; they are not things the model names.
TIME = frozenset("""
today yesterday tomorrow day days daily week weeks weekly month months monthly year years yearly quarter quarters
last next past previous recent recently since ago january february march april may june july august september october
november december jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday
sunday morning evening night q1 q2 q3 q4 right currently current lately
""".split())
# What kind of source a question needs, by the words it uses, and how to tell the source is present.
NEEDS = (
    ("ownership", {"owner", "codeowners", "maintainer", "maintains", "owns"}, "who owns what (CODEOWNERS or git history)"),
    ("records", {"revenue", "rate", "spend", "spent", "cost", "costs", "money", "income", "sales", "churn", "total",
                 "average", "much", "many", "volume", "trend", "growth"} | TIME,
     "the business's own records (exports or pulls in .ontologyex/operations)"),
    ("health", {"error", "healthy", "health", "latency", "slow", "uptime", "incident", "status", "outage"},
     "live health (feeds or traces)"),
    ("compliance", {"personal", "compliance", "soc", "hipaa", "pci", "audit", "encrypt", "encryption"},
     "the compliance evidence map"),
)
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[^A-Za-z0-9]+")


def stem(word: str) -> str:
    word = SYNONYMS.get(word, word)
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return SYNONYMS.get(word, word)


def words(text: str) -> list[str]:
    """Lowercase words of a label or sentence: IssueRefund, payments.apply_refund and 'issue a refund' agree."""
    return [stem(w.lower()) for w in CAMEL.split(text or "") if w and len(w) > 1]


def terms_of(question: str) -> list[str]:
    """The words a question is about: names split (IssueRefund is issue, refund), filler and time words dropped."""
    kept = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_'.-]*", question):
        for part in CAMEL.split(token.strip("'.")):
            w = part.lower()
            if (len(w) < 3 and w not in SHORT) or w in STOP or w in TIME or w.isdigit():
                continue
            kept.append(stem(w))
    return list(dict.fromkeys(kept))


def _vocabulary(bundle: dict) -> list[dict]:
    """Everything a question can be matched against: (words, what it is, how strong a match is, evidence)."""
    out = []
    for entity in ec._entities(bundle).values():
        evidence = [{"path": q["path"], "lines": [q["start"], q["end"]]} for q in entity.get("quotes", []) if q.get("path")]
        base = {"id": entity["id"], "label": entity["label"], "layer": entity["layer"], "kind": entity["kind"]}
        out.append({**base, "via": "name", "words": set(words(entity["label"])) | set(words(entity["id"].rsplit(":", 1)[-1])),
                    "evidence": evidence})
        if entity.get("summary"):
            out.append({**base, "via": "definition", "words": set(words(entity["summary"])), "evidence": evidence})
        for rule in [*entity.get("rules", []), *entity.get("confirmed_rules", [])]:
            out.append({**base, "via": "rule", "words": set(words(rule.get("statement") or "")),
                        "rule": rule.get("statement"), "evidence": evidence})
    state = bundle.get("state") or {}
    relations = [e for e in state.get("edges", []) if e.get("relation") in ("DEPENDS_ON", "CALLS")]
    if relations:  # "what depends on X" is answered by the declared and observed dependencies
        out.append({"id": "relations:dependencies", "label": f"{len(relations)} dependencies between systems",
                    "layer": "system", "kind": "relations", "via": "records",
                    "words": {"depend", "call", "connect", "upstream", "downstream", "affect", "impact"}, "evidence": []})
    for node in state.get("nodes", []):  # "which services": a system's kind is a word too
        out.append({"id": node["id"], "label": node["label"], "layer": "system", "kind": node["kind"], "via": "definition",
                    "words": set(words(node["kind"])) - {"declared", "discovery"}, "evidence": []})
    present = _present(bundle)
    for name, cues, what in NEEDS:  # a loaded source covers the words that ask for it ("owner", "error", "rate")
        if present[name]:
            out.append({"id": f"source:{name}", "label": what, "layer": "source", "kind": name, "via": "records",
                        "words": {stem(c) for c in cues if c not in TIME}, "evidence": []})
    ops = bundle.get("operations") or {}
    for stream in ops.get("streams_present", []):
        out.append({"id": f"records:{stream}", "label": stream, "layer": "records", "kind": "record_stream",
                    "via": "records", "words": set(words(stream)), "evidence": [
                        {"path": s["file"]} for s in ops.get("sources", []) if stream in (s.get("stream") or "")][:3]})
    for c in (ops.get("written") or {}).get("commitments", []):  # what the business's own documents promise
        kind = str(c.get("kind") or "").lower().replace("_", " ")
        out.append({"id": f"written:{c.get('file')}:{c.get('line')}", "label": c.get("text"), "layer": "records",
                    "kind": "written_commitment", "via": "rule", "rule": c.get("text"),
                    "words": set(words(c.get("text") or "")) | set(words(kind)) | set(words(str(c.get("file") or "")))
                    | {"promise", "commitment", "policy"},
                    "evidence": [{"path": c.get("file"), "lines": [c.get("line"), c.get("line")]}]})
    for supplier in (bundle.get("suppliers") or {}).get("suppliers", []):
        out.append({"id": supplier.get("id"), "label": supplier.get("name"), "layer": "system", "kind": "supplier",
                    "via": "name", "words": set(words(supplier.get("name") or "")) | set(words(supplier.get("category") or "")),
                    "evidence": [{"path": e["path"], "lines": [e.get("line"), e.get("line")]}
                                 for e in supplier.get("evidence", [])[:2] if e.get("path")]})
    return out


def _present(bundle: dict) -> dict[str, bool]:
    owners = bundle.get("ownership") or {}
    return {"ownership": owners.get("status") == "OK" and bool(owners.get("codeowners") or owners.get("people")),
            "records": (bundle.get("operations") or {}).get("status") == "OK",
            "health": any(n.get("observed_at") for n in (bundle.get("state") or {}).get("nodes", [])),
            "compliance": bool((bundle.get("compliance") or {}).get("packs"))}


def _needs(terms: list[str], question: str, bundle: dict) -> list[dict]:
    plain = {w.lower() for w in re.findall(r"[A-Za-z]+", question)}
    present = _present(bundle)
    out = []
    for name, cues, what in NEEDS:
        if cues & (set(terms) | plain):
            out.append({"source": name, "present": present[name], "what": what})
    return out


def check(bundle: dict, question: str) -> dict:
    """The pre-flight: which terms the model covers (and the lines behind them), which it does not, what is open."""
    if type(question) is not str or not 0 < len(question.strip()) <= MAX_QUESTION or any(ord(c) < 32 for c in question):
        raise ValueError("a question is 1 to 500 printable characters")
    terms = terms_of(question)
    vocabulary = _vocabulary(bundle)
    strength = {"name": 0, "records": 1, "rule": 2, "definition": 3}  # how directly a match names the term
    covered, uncovered, matched_ids = [], [], []
    for term in terms:
        hits = sorted((v for v in vocabulary if term in v["words"]), key=lambda v: (strength[v["via"]], v["layer"] != "business",
                                                                                      len(v["label"] or ""), v["id"] or ""))
        if not hits:
            uncovered.append(term)
            continue
        seen, rows = set(), []
        for hit in hits:
            if hit["id"] in seen:
                continue
            seen.add(hit["id"])
            rows.append({k: hit[k] for k in ("id", "label", "layer", "kind", "via")}
                        | ({"rule": hit["rule"]} if hit.get("rule") else {}) | {"evidence": hit["evidence"][:3]})
            if len(rows) >= MAX_MATCHES:
                break
        covered.append({"term": term, "covered_by": rows})
        matched_ids += [r["id"] for r in rows if r["via"] in ("name", "rule")]
    entities = ec._entities(bundle)
    matched_ids = [i for i in matched_ids if i in entities]  # what get_entity_context can open
    open_questions = []
    for entity_id in dict.fromkeys(matched_ids):
        for q in (entities.get(entity_id) or {}).get("open_questions", []):
            if q.get("type") in ("unknown", "conflict"):
                overlap = set(words(q.get("statement") or "")) & set(terms)
                open_questions.append({"entity": entity_id, "type": q["type"], "id": q.get("id"),
                                       "statement": q.get("statement"), "about_this_question": bool(overlap)})
    needs = _needs(terms, question, bundle)
    share = len(covered) / len(terms) if terms else 0.0
    missing_sources = [n for n in needs if not n["present"]]
    if not terms or not covered:
        verdict = "NOT_IN_SOURCES"
    elif not uncovered and not missing_sources and not any(q["about_this_question"] for q in open_questions):
        verdict = "ANSWERABLE"
    else:
        verdict = "PARTLY"
    reasons = []
    if uncovered:
        reasons.append("nothing in the sources names " + ", ".join(repr(u) for u in uncovered[:6]))
    for n in missing_sources:
        reasons.append(f"it needs {n['what']}, and none is loaded")
    for q in open_questions:
        if q["about_this_question"]:
            reasons.append(f"the sources record it as open ({q['type']}): {q['statement']}")
    return {"schema_version": 1, "kind": "ANSWERABILITY", "question": question, "verdict": verdict,
            "coverage": round(share, 3), "terms": terms, "covered": covered, "uncovered": uncovered,
            "open_questions": open_questions[:10], "needs": needs, "reasons": reasons,
            "next": [{"tool": "get_entity_context", "id": i} for i in list(dict.fromkeys(matched_ids))[:5]],
            "semantics": "WORDS_MATCHED_AGAINST_THE_MODEL_NOT_PROOF_THAT_THE_ANSWER_IS_THERE",
            "execution_authorized": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="control_room.py --bundle-only output")
    parser.add_argument("question")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(check(json.loads(args.bundle.read_text("utf-8")), args.question), indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"answerable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
