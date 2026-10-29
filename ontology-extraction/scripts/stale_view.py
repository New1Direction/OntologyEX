"""The stale-view check: a decision applies only to what the person saw.

A page shows a suggestion, a person reads it and clicks Confirm. Between the two, the
evidence can change: a cited file is edited, a link's binding code moves, a dependency
gains calls. Finishing the click is not the same as being safe to apply. So every page
carries a fingerprint of each thing it lets a person decide, and the decision request
sends back the fingerprints it showed. Inside the decision lock the control room
computes them again from the same state it is about to record; if any differ, nothing
is written and the answer is STALE_VIEW with what changed, so the person reloads and
decides on what is there now.

What a fingerprint covers is what the person relies on:
- a link suggestion: its strength, its kinds of signal, and the evidence hashes its
  watermark would record (the declaring file, the skill, the binding's source);
- a dependency suggestion: its strength and kinds of signal (not the live call count);
- a rule proposal: the hash of the cited lines and whether they were found;
- a confirmed link or rule to re-confirm: the evidence hashes as they are now.
"""
from __future__ import annotations

from policy_check import canonical, digest

import rule_suggest


class StaleView(Exception):
    """What the person saw changed before the decision could be applied."""

    def __init__(self, changed: list[str]):
        super().__init__("changed since the page loaded: " + ", ".join(changed))
        self.changed = changed


def fingerprint(value) -> str:
    return digest(canonical(value))[:24]


def _signals(row: dict) -> list[str]:
    return sorted({str(s.get("signal")) for s in row.get("signals", [])})


def views(picture: dict, marks, files) -> dict:
    """kind -> {"suggestion": {id: fingerprint}, "decided": {id: fingerprint}} for everything a page lets a person decide."""
    links = {s["id"]: fingerprint({"strength": s.get("strength"), "signals": _signals(s),
                                   "evidence": marks.now(s["skill"], s["binding"], s["component"]) if marks else {}})
             for s in (picture.get("link_suggestions") or {}).get("suggestions", [])}
    relinks = {m["id"]: fingerprint({"now": m.get("now")}) for m in picture.get("link_watermarks", [])}
    dependencies = {s["id"]: fingerprint({"strength": s.get("strength"), "signals": _signals(s)})
                    for s in (picture.get("dependency_suggestions") or {}).get("suggestions", [])}
    rules = {s["id"]: fingerprint({"citation": s.get("citation"),
                                   "lines": (s.get("citation_check") or {}).get("lines_sha256"),
                                   "status": (s.get("citation_check") or {}).get("status")})
             for s in (picture.get("rule_suggestions") or {}).get("suggestions", [])}
    rerules = {}
    for r in picture.get("rule_decided", []):
        if r.get("citation") and files is not None:
            check = rule_suggest.check_citation(files, r["citation"])
            rerules[r["id"]] = fingerprint({"lines": check.get("lines_sha256"), "status": check.get("status")})
    return {"links": {"suggestion": links, "decided": relinks},
            "dependencies": {"suggestion": dependencies, "decided": {}},
            "rules": {"suggestion": rules, "decided": rerules}}


def check(seen: dict, ids: list[str], current: dict[str, str]) -> None:
    """Refuse the whole batch when any ID's fingerprint is missing or differs from what was shown."""
    changed = [i for i in dict.fromkeys(ids) if seen.get(i) is None or current.get(i) != seen[i]]
    if changed:
        raise StaleView(changed)


def valid_seen(seen) -> bool:
    return (type(seen) is dict and len(seen) <= 200
            and all(type(k) is str and type(v) is str and 0 < len(k) <= 200 and 0 < len(v) <= 64
                    for k, v in seen.items()))
