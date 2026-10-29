#!/usr/bin/env python3
"""The weekly digest: what changed in the business since last week, and who decided what.

    python digest.py record --bundle BUNDLE.json --history HISTORY.jsonl
    python digest.py show --bundle BUNDLE.json --history HISTORY.jsonl [--days 7] [--repo REPO] [--markdown]

The command center ranks what needs attention now; the digest compares it with how it
stood when the window began. Each item is new, fixed, worse, better, or still open
(with how long it has been open). Beside them: how the headline counts and coverage
moved, the decisions people made (links, dependencies, rules), and, with a local
repository, the commits in the window and the business they touched.

History is an append-only JSON-lines file of command-center snapshots. A snapshot is
appended only when the ranking, counts or coverage changed, or once a day when nothing
did, so the file stays small. The control room records it with --history; `record`
does the same from a bundle (for a nightly job). Only that file is written; git is
only read.

Deterministic for the same history, model and clock. Nothing here changes a system or
authorizes anything.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import change_impact
import command_center
import decisions
import entity_context
from policy_check import DomainError, canonical, digest as sha256

SNAPSHOT_KIND = "ONTOLOGYEX_COMMAND_SNAPSHOT"
KEEP_UNCHANGED = timedelta(hours=24)  # one snapshot a day even when nothing moved, so every day has a point
MAX_HISTORY_BYTES, MAX_HISTORY_LINES = 32 * 1024 * 1024, 20_000
MAX_DAYS = 90
MAX_LISTED = 200
MAX_COMMITS = 50
VERB = {"confirm": "confirmed", "reject": "rejected", "undo": "undid", "reconfirm": "re-confirmed"}


def _at(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(text) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def snapshot(report: dict, now: datetime) -> dict:
    """The parts of a command-center report a later digest compares against."""
    return {"schema_version": 1, "kind": SNAPSHOT_KIND, "at": _at(now),
            "items": [{"id": i["id"], "kind": i["kind"], "category": i["category"], "level": i["level"],
                       "score": i["score"], "title": i["title"], "subject": (i.get("subject") or {}).get("id")}
                      for i in report["items"]],
            "kpis": {k["label"]: k["value"] for k in report.get("kpis", [])},
            "coverage": {r["label"]: {"part": r["part"], "whole": r["whole"]}
                         for r in (report.get("coverage") or {}).get("rows", [])},
            "field": (report.get("field") or {}).get("field")}


def _fingerprint(snap: dict) -> str:
    return sha256(canonical({k: snap.get(k) for k in ("items", "kpis", "coverage", "field")}))


def read_history(path: Path | None) -> list[dict]:
    """Valid snapshots, oldest first; torn, foreign or undated lines are skipped."""
    rows = []
    for row in decisions.read_jsonl_tail(path, MAX_HISTORY_BYTES, MAX_HISTORY_LINES):
        if isinstance(row, dict) and row.get("kind") == SNAPSHOT_KIND and isinstance(row.get("items"), list) \
                and _parse(row.get("at")) is not None:
            rows.append(row)
    return sorted(rows, key=lambda r: _parse(r["at"]))


def record(path: Path, report: dict, now: datetime) -> bool:
    """Append a snapshot when something changed since the last one, or a day has passed. True when appended."""
    snap = snapshot(report, now)
    history = read_history(path)
    if history:
        last = history[-1]
        if _fingerprint(last) == _fingerprint(snap) and now - _parse(last["at"]) < KEEP_UNCHANGED:
            return False
    decisions.append_jsonl(path, snap)
    return True


def _open_since(history: list[dict], current_ids: set[str], now: datetime) -> dict[str, str]:
    """For each item open now, when its current unbroken run of appearances began."""
    start: dict[str, str | None] = {}
    for snap in history:
        present = {i["id"] for i in snap["items"]}
        for item_id in set(start) | present:
            start[item_id] = (start.get(item_id) or snap["at"]) if item_id in present else None
    return {i: start.get(i) or _at(now) for i in current_ids}


def labels(bundle: dict) -> dict[str, str]:
    """Entity IDs to the names people use, for the decisions a digest lists."""
    return {i: e["label"] for i, e in entity_context._entities(bundle).items()}


def _decision(kind: str, event: dict, names: dict[str, str]) -> dict:
    row = event.get("row") or {}
    if kind == "link":
        what = f"{row.get('binding')} runs on {names.get(row.get('component'), row.get('component'))}"
    elif kind == "dependency":
        what = (f"{names.get(row.get('source'), row.get('source'))} depends on "
                f"{names.get(row.get('target'), row.get('target'))}")
    else:
        what = str(row.get("statement") or row.get("id") or "a business rule")
    about = [i for i in (row.get("component"), row.get("source"), row.get("target")) if isinstance(i, str)]
    return {"kind": kind, "action": event.get("action"), "by": event.get("by"), "at": event.get("at"),
            "what": what[:300], "note": str(event.get("note") or "")[:300], "about": about}


def _code(repo: Path, bundle: dict, since: datetime) -> dict:
    """The commits in the window and what they touched in the business; read-only git."""
    try:
        base = change_impact._git(repo, "rev-list", "-1", f"--before={_at(since)}", "HEAD").decode().strip()
        log = change_impact._git(repo, "log", f"--since={_at(since)}", "--no-merges", f"-{MAX_COMMITS + 1}",
                                 "--format=%h%x00%cI%x00%s", "HEAD").decode("utf-8", "replace")
    except change_impact.DiffError as exc:
        return {"status": "GIT_UNAVAILABLE", "detail": str(exc)[:300]}
    commits = []
    for line in log.splitlines():
        parts = line.split("\0")
        if len(parts) == 3:
            commits.append({"commit": parts[0], "at": parts[1], "subject": parts[2][:200]})
    if not commits:
        return {"status": "NO_COMMITS", "commits": [], "commits_total": 0}
    if not base:
        return {"status": "HISTORY_STARTS_IN_WINDOW", "commits": commits[:MAX_COMMITS],
                "commits_total": len(commits), "detail": "No commit is older than the window, so there is "
                                                         "nothing to compare the changes with."}
    try:
        changes, omitted = change_impact.git_changes(repo, base, "HEAD")
    except change_impact.DiffError as exc:
        return {"status": "GIT_UNAVAILABLE", "detail": str(exc)[:300]}
    impact = entity_context.change_impact(bundle, changes)
    business = [t for t in impact["touched"] if t.get("layer") == "business"]
    return {"status": "OK", "commits": commits[:MAX_COMMITS], "commits_total": len(commits),
            "commits_more": len(commits) > MAX_COMMITS, "base": base[:12], "changed_files": impact["changed_files"],
            "files_omitted": omitted, "touched_business": business[:MAX_LISTED],
            "touched_total": impact["touched_total"],
            "impacted_business": [r for r in impact["impacted"] if r.get("layer") == "business"][:MAX_LISTED],
            "reconfirm_after": impact["reconfirm_after_merge"]}


def build(history: list[dict], report: dict, now: datetime, days: int = 7,
          decision_logs: dict[str, list[dict]] | None = None, code: dict | None = None,
          names: dict[str, str] | None = None) -> dict:
    """Compare the ranking now with how it stood when the window began."""
    since = now - timedelta(days=days)
    current = snapshot(report, now)
    before = [s for s in history if _parse(s["at"]) <= since]
    baseline = before[-1] if before else (history[0] if history else None)
    status = "OK" if before else "HISTORY_STARTS_IN_WINDOW" if history else "NO_HISTORY"
    then = {i["id"]: i for i in (baseline or {}).get("items", [])}
    now_items = {i["id"]: i for i in current["items"]}
    opened = _open_since(history, set(now_items), now)
    full = {i["id"]: i for i in report["items"]}

    def row(item_id: str) -> dict:
        item = dict(full.get(item_id) or now_items.get(item_id) or then[item_id])
        old = then.get(item_id)
        extra = {"score_before": old["score"], "level_before": old["level"]} if old else {}
        if item_id in now_items:
            extra["open_since"] = opened[item_id]
            extra["open_days"] = max(0, (now - _parse(opened[item_id])).days)
            if old and _parse(opened[item_id]) > _parse(baseline["at"]):
                extra["reopened"] = True  # it went away inside the window and came back
        return {**item, **extra}

    groups = {"new": [], "worse": [], "better": [], "still_open": [], "fixed": []}
    if baseline is not None:
        for item_id, item in now_items.items():
            old = then.get(item_id)
            key = ("new" if old is None else "worse" if item["score"] > old["score"]
                   else "better" if item["score"] < old["score"] else "still_open")
            groups[key].append(row(item_id))
        groups["fixed"] = [row(i) for i in then if i not in now_items]
    else:  # no history: nothing to compare with, everything open is simply open
        groups["still_open"] = [row(i) for i in now_items]
    for key, rows in groups.items():
        rows.sort(key=lambda r: (-r["score"], r["title"]))
        groups[key] = rows[:MAX_LISTED]
    base_kpis, base_cov = (baseline or {}).get("kpis", {}), (baseline or {}).get("coverage", {})
    better_when = {k["label"]: {"ok": "up", "risk": "down", "unknown": "down"}.get(k.get("tone"))
                   for k in report.get("kpis", [])}
    kpis = [{"label": label, "now": value, "before": base_kpis.get(label), "better_when": better_when.get(label),
             "delta": value - base_kpis[label] if isinstance(base_kpis.get(label), int) else None}
            for label, value in current["kpis"].items()]

    def percent(cell):
        return round(100 * cell["part"] / cell["whole"]) if cell and cell.get("whole") else None
    coverage = []
    for label, cell in current["coverage"].items():
        now_pct, then_pct = percent(cell), percent(base_cov.get(label))
        coverage.append({"label": label, "now": now_pct, "before": then_pct, "part": cell["part"],
                         "whole": cell["whole"], "delta": now_pct - then_pct
                         if now_pct is not None and then_pct is not None else None})
    made = []
    for kind, events in (decision_logs or {}).items():
        for event in events:
            moment = _parse(event.get("at"))
            if moment is not None and since < moment <= now:
                made.append(_decision(kind, event, names or {}))
    made.sort(key=lambda d: (d["at"], d["kind"], d["what"]), reverse=True)
    people: dict[str, int] = {}
    for decision in made:
        people[decision["by"]] = people.get(decision["by"], 0) + 1
    long_open = [r for r in groups["still_open"] + groups["worse"] + groups["better"]
                 if r["level"] in ("CRITICAL", "HIGH") and r.get("open_days", 0) >= days]
    long_open.sort(key=lambda r: (-r.get("open_days", 0), -r["score"]))
    result = {"schema_version": 1, "kind": "WEEKLY_DIGEST", "status": status, "days": days,
              "since": _at(since), "until": _at(now), "baseline_at": (baseline or {}).get("at"),
              "history_snapshots": len(history), **groups,
              "counts": {k: len(v) for k, v in groups.items()}, "long_open": long_open[:20],
              "kpis": kpis, "coverage": coverage, "decisions": made[:MAX_LISTED],
              "decisions_total": len(made), "decided_by": dict(sorted(people.items(), key=lambda kv: (-kv[1], kv[0]))),
              "code": code, "field": current["field"],
              "semantics": "COMPARES_RANKINGS_NOT_OUTCOMES", "execution_authorized": False}
    result["headline"] = headline(result)
    return result


def headline(d: dict) -> str:
    """One sentence a person can read first."""
    if d["status"] == "NO_HISTORY":
        open_now = len(d["still_open"])
        return (f"No history yet, so nothing to compare: {open_now} thing{'s' if open_now != 1 else ''} need attention "
                "now. Start the control room with --history to see next week's movement.")
    c = d["counts"]
    parts = []
    critical_new = sum(r["level"] == "CRITICAL" for r in d["new"])
    if c["new"]:
        parts.append(f"{c['new']} new" + (f" ({critical_new} critical)" if critical_new else ""))
    if c["fixed"]:
        parts.append(f"{c['fixed']} fixed")
    if c["worse"]:
        parts.append(f"{c['worse']} worse")
    if c["better"]:
        parts.append(f"{c['better']} better")
    reopened = sum(bool(r.get("reopened")) for k in ("worse", "better", "still_open") for r in d[k])
    if reopened:
        parts.append(f"{reopened} fixed and back again")
    parts.append(f"{c['still_open']} unchanged")
    text = f"Last {d['days']} days: " + ", ".join(parts) + "."
    if d["long_open"]:
        oldest = d["long_open"][0]
        text += f" Longest open: {oldest['title']} ({oldest['level'].lower()}, {oldest['open_days']} days)."
    if d["decisions_total"]:
        text += f" {d['decisions_total']} decision{'s' if d['decisions_total'] != 1 else ''} made."
    if d["status"] == "HISTORY_STARTS_IN_WINDOW":
        text += f" History starts {d['baseline_at'][:10]}, inside the window."
    return text


def _owner(row: dict) -> str:
    owner = row.get("owner")
    if not isinstance(owner, dict):
        return ""
    return f" (owner: {owner['owner']})" if owner.get("owner") else " (owner: nobody)"


def markdown(d: dict) -> str:
    """The digest as Markdown for email, chat or a ticket."""
    out = [f"# Business digest, {d['since'][:10]} to {d['until'][:10]}", "", d["headline"], ""]

    def items(title: str, rows: list[dict], show) -> None:
        if rows:
            out.extend([f"## {title}", ""])
            out.extend(f"- **{r['level']} {r['score']}** {r['title']}{show(r)}{_owner(r)}" for r in rows[:25])
            if len(rows) > 25:
                out.append(f"- ... and {len(rows) - 25} more")
            out.append("")
    items("New", d["new"], lambda r: f" — {r.get('next_step', '')}" if r.get("next_step") else "")
    items("Fixed", d["fixed"], lambda r: "")
    items("Worse", d["worse"], lambda r: f" (was {r['score_before']})")
    items("Fixed, then back", [r for k in ("worse", "better", "still_open") for r in d[k] if r.get("reopened")],
          lambda r: f" — back {r['open_days']} days ago")
    items("Better", d["better"], lambda r: f" (was {r['score_before']})")
    items("Open longest", d["long_open"], lambda r: f" — open {r['open_days']} days")
    if d["kpis"]:
        out.extend(["## Headline counts", ""])
        out.extend(f"- {k['label']}: {k['now']}" + (f" ({k['delta']:+d})" if k["delta"] else "") for k in d["kpis"])
        out.append("")
    if any(c["delta"] for c in d["coverage"]):
        out.extend(["## Visibility", ""])
        out.extend(f"- {c['label']}: {c['now']}% ({c['delta']:+d} points)" for c in d["coverage"] if c["delta"])
        out.append("")
    if d["decisions"]:
        out.extend(["## Decisions made", ""])
        out.extend(f"- {x['at'][:10]} {x['by']} {VERB.get(x['action'], x['action'])} a {x['kind']}: {x['what']}"
                   for x in d["decisions"][:25])
        out.append("")
    code = d.get("code") or {}
    if code.get("commits"):
        out.extend(["## Code shipped", "", f"{code['commits_total']} commit(s)."
                    + (f" They touched {len(code['touched_business'])} business object(s) or action(s): "
                       + ", ".join(t["label"] for t in code["touched_business"][:10]) + "."
                       if code.get("touched_business") else ""), ""])
        out.extend(f"- `{c['commit']}` {c['subject']}" for c in code["commits"][:15])
        out.append("")
    out.append("_Compares the command-center ranking now with the start of the window; scores rank attention, "
               "they are not outcomes. Generated by OntologyEX from the sources and feeds._")
    return "\n".join(out) + "\n"


def decision_logs(links: Path | None, dependencies: Path | None, rules: Path | None) -> dict[str, list[dict]]:
    """Every decision history the control room keeps, by kind."""
    return {kind: entity_context.read_history(decisions.history_path(path)) if path is not None else []
            for kind, path in (("link", links), ("dependency", dependencies), ("rule", rules))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "show"):
        sub = commands.add_parser(name)
        sub.add_argument("--bundle", type=Path, required=True, help="a bundle from control_room.py --bundle-only")
        sub.add_argument("--history", type=Path, required=True)
        if name == "show":
            sub.add_argument("--days", type=int, default=7)
            sub.add_argument("--repo", type=Path, help="a local git repository: adds the commits in the window")
            sub.add_argument("--links", type=Path)
            sub.add_argument("--dependencies", type=Path)
            sub.add_argument("--rules", type=Path)
            sub.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    try:
        bundle = json.loads(args.bundle.read_text("utf-8"))
        report, now = command_center.report(bundle), datetime.now(timezone.utc)
        if args.command == "record":
            print(json.dumps({"status": "RECORDED" if record(args.history, report, now) else "UNCHANGED"}))
            return 0
        if not 1 <= args.days <= MAX_DAYS:
            raise ValueError(f"--days is 1..{MAX_DAYS}")
        code = _code(args.repo.resolve(), bundle, now - timedelta(days=args.days)) if args.repo else None
        result = build(read_history(args.history), report, now, args.days,
                       decision_logs(args.links, args.dependencies, args.rules), code, labels(bundle))
    except (OSError, ValueError, KeyError, TypeError, DomainError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)[:300]}))
        return 1
    print(markdown(result) if args.markdown else json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
