#!/usr/bin/env python3
"""Who owns each part of the business: declared in CODEOWNERS, and who actually changes it.

    python ownership.py --bundle BUNDLE.json --repo REPO

Two sources, both only read. CODEOWNERS (GitHub or GitLab locations) says who is
declared to own a path; the line is cited, and the last matching rule wins, as on
GitHub. Git history over the last year says who changed the files most and last
(names only, never e-mail addresses; bots are left out); that is inferred, not
declared. Each entity is owned through its files: a service through the folder its
compose file builds (or the file declaring it), a route or table through the file
declaring it, a supplier through the manifest lines naming it, and a business object
or action through its binding's source code, else the skill files it cites, else the
systems it runs on.

Flags what a manager would want to know: business actions nobody owns, and code only
one person has changed in a year. Nothing here changes anything.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import change_impact
import discover_repo
import entity_context as ec
import otel_setup

CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")
BOTS = re.compile(r"\[bot\]|dependabot|renovate|github-actions|greenkeeper|semantic-release|snyk-bot|mergify", re.I)
OWNER = re.compile(r"@[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?\Z|[^@\s]+@[^@\s]+\.[^@\s]+\Z")
WINDOW_DAYS = 365
MAX_COMMITS = 5000
MAX_RULES = 2000
TOP = 3


def _pattern(text: str) -> re.Pattern:
    """A CODEOWNERS (gitignore-style) pattern as a regex over repository paths."""
    anchored = text.startswith("/") or "/" in text.rstrip("/")
    body = text.strip("/")
    directory = text.endswith("/")
    regex, i = "", 0
    while i < len(body):
        if body.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif body.startswith("**", i):
            regex, i = regex + ".*", i + 2
        elif body[i] == "*":
            regex, i = regex + "[^/]*", i + 1
        elif body[i] == "?":
            regex, i = regex + "[^/]", i + 1
        else:
            regex, i = regex + re.escape(body[i]), i + 1
    # A named folder covers what is inside it; "docs/*" covers only the files directly in docs.
    inside = "/.*" if directory else "" if "*" in body.rsplit("/", 1)[-1] else "(?:/.*)?"
    return re.compile(("" if anchored else "(?:.*/)?") + regex + inside + r"\Z")


def read_codeowners(repo: Path) -> dict:
    """The first CODEOWNERS file GitHub or GitLab would use, as ordered rules with their lines."""
    for rel in CODEOWNERS_PATHS:
        path = repo / rel
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1_000_000:
            rules = []
            for number, raw in enumerate(path.read_text("utf-8", "replace").splitlines(), 1):
                line = raw.split(" #", 1)[0].strip()
                if not line or line.startswith("#") or line.startswith("[") or line.startswith("^["):
                    continue  # comments and GitLab section headers
                parts = line.split()
                owners = [o for o in parts[1:] if OWNER.match(o)]
                rules.append({"pattern": parts[0], "owners": owners, "line": number, "regex": _pattern(parts[0])})
                if len(rules) >= MAX_RULES:
                    break
            return {"path": rel, "rules": rules}
    return {"path": None, "rules": []}


def declared_owner(codeowners: dict, path: str) -> dict | None:
    """The last rule matching a path (a folder ends in '/'); None when no rule matches."""
    found = None
    for rule in codeowners["rules"]:
        if rule["regex"].match(path):
            found = rule
    if found is None:
        return None
    return {"owners": found["owners"], "pattern": found["pattern"], "path": codeowners["path"], "line": found["line"]}


def git_activity(repo: Path, now: datetime) -> tuple[dict[str, dict[str, list]], str]:
    """For each file changed in the window: person -> [commits, last commit time]. Read-only git."""
    since = (now - timedelta(days=WINDOW_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        log = change_impact._git(repo, "log", f"--since={since}", "--no-merges", f"-{MAX_COMMITS}",
                                 "--format=%x1e%an%x1f%cI", "--name-only", "HEAD").decode("utf-8", "replace")
    except change_impact.DiffError:
        return {}, "GIT_UNAVAILABLE"
    files: dict[str, dict[str, list]] = {}
    for record in log.split("\x1e")[1:]:
        lines = record.split("\n")
        head = lines[0].split("\x1f")
        if len(head) != 2 or not head[0].strip() or BOTS.search(head[0]):
            continue
        name, when = head[0].strip()[:120], head[1].strip()
        for path in (line.strip() for line in lines[1:]):
            if path:
                row = files.setdefault(path, {}).setdefault(name, [0, when])
                row[0] += 1
                row[1] = max(row[1], when)
    return files, "OK"


def scan_repo(repo: Path, now: datetime | None = None) -> dict:
    """What ownership reads from a repository; the control room caches it."""
    now = now or datetime.now(timezone.utc)
    repo = repo.resolve()
    try:
        builds = otel_setup._compose_builds(repo, discover_repo.discover(repo))
    except (ValueError, OSError, discover_repo.DiscoveryError):
        builds = {}
    activity, git = git_activity(repo, now)
    try:
        tracked = sorted(p for p in change_impact._git(repo, "ls-files", "-z").decode("utf-8", "replace").split("\0") if p)
    except change_impact.DiffError:
        tracked = []
    return {"codeowners": read_codeowners(repo), "activity": activity, "git": git, "tracked": tracked,
            "folders": {component: folder for folder, (_, component) in builds.items()}}


def _files(bundle: dict, folders: dict[str, str]) -> dict[str, list[dict]]:
    """Each entity's own files, strongest basis first."""
    entities = ec._entities(bundle)
    paths = {e["id"]: e.get("path") for e in bundle["state"].get("source_evidence", [])}
    out: dict[str, list[dict]] = {}

    def add(entity_id, path, basis):
        if entity_id in entities and isinstance(path, str) and path:
            rows = out.setdefault(entity_id, [])
            if not any(r["path"] == path for r in rows):
                rows.append({"path": path, "basis": basis})
    for node in bundle["state"].get("nodes", []):
        if node.get("kind") == "external_supplier":
            continue
        folder = folders.get(node["id"])
        if folder is not None:
            add(node["id"], "" if folder == "." else folder.rstrip("/") + "/", "SERVICE_FOLDER")
        else:
            add(node["id"], paths.get(node.get("evidence_id")), "DECLARING_FILE")
    for supplier in (bundle.get("suppliers") or {}).get("suppliers", []):
        for evidence in supplier.get("evidence", []):
            add(supplier["id"], evidence.get("path"), "NAMED_IN_MANIFEST")
    picture = bundle.get("business") or {}
    for row in [*picture.get("actions", []), *picture.get("objects", [])]:
        for binding in row.get("bindings", []):
            for source in (binding.get("evidence", []) if isinstance(binding, dict) else []):
                add(row["key"], source.get("path"), "BINDING_SOURCE")
        if row["key"] not in out:
            for quote in row.get("evidence", []):
                add(row["key"], quote.get("path"), "CITED_BY_SKILL")
    return out


def _people(activity: dict, path: str) -> dict[str, list]:
    """Commits per person on a file, or on everything under a folder ('' is the whole repository)."""
    if path == "" or path.endswith("/"):
        merged: dict[str, list] = {}
        for name_path, people in activity.items():
            if name_path.startswith(path):
                for name, (count, when) in people.items():
                    row = merged.setdefault(name, [0, when])
                    row[0] += count
                    row[1] = max(row[1], when)
        return merged
    return activity.get(path, {})


def _tracked(tracked: list[str], path: str) -> bool:
    if path == "" or path.endswith("/"):
        return any(t.startswith(path) for t in tracked)
    return path in tracked


def report(bundle: dict, scan: dict | None) -> dict:
    """Owners for every entity, who owns what, and where nobody does."""
    entities = ec._entities(bundle)
    if scan is None:
        return {"schema_version": 1, "kind": "OWNERSHIP", "status": "NO_LOCAL_REPOSITORY", "entities": [],
                "people": [], "codeowners": None, "git": None, "coverage": [], "execution_authorized": False}
    files = _files(bundle, scan["folders"])
    picture = bundle.get("business") or {}
    runs_on = {row["key"]: list(row.get("systems", [])) for row in [*picture.get("actions", []), *picture.get("objects", [])]}
    rows: dict[str, dict] = {}

    def own(entity_id: str) -> dict:
        declared, people = [], {}
        for f in files.get(entity_id, []):
            found = declared_owner(scan["codeowners"], f["path"])
            if found and found["owners"] and found not in declared:
                declared.append({**found, "for": f["path"] or "(whole repository)", "basis": f["basis"]})
            for name, (count, when) in _people(scan["activity"], f["path"]).items():
                row = people.setdefault(name, [0, when])
                row[0] += count
                row[1] = max(row[1], when)
        active = [{"name": n, "commits": c, "last": w}
                  for n, (c, w) in sorted(people.items(), key=lambda kv: (-kv[1][0], kv[0]))]
        return {"declared": declared[:5], "active": active[:TOP], "active_total": len(active),
                "commits": sum(a[0] for a in people.values())}
    for entity_id in files:
        rows[entity_id] = own(entity_id)
    for entity_id, systems in runs_on.items():  # a business item with no owner of its own: who owns what it runs on
        row = rows.get(entity_id) or {"declared": [], "active": [], "active_total": 0, "commits": 0}
        if not row["declared"] and not row["active"]:
            inherited = [rows[s] for s in systems if s in rows and (rows[s]["declared"] or rows[s]["active"])]
            if inherited:
                row = {"declared": [d for r in inherited for d in r["declared"]][:5],
                       "active": [a for r in inherited for a in r["active"]][:TOP],
                       "active_total": sum(r["active_total"] for r in inherited),
                       "commits": sum(r["commits"] for r in inherited), "through": systems}
        rows[entity_id] = row
    out = []
    for entity_id, row in rows.items():
        entity = entities[entity_id]
        status = ("DECLARED" if row["declared"] else "ACTIVE_ONLY" if row["active"] else "NO_OWNER")
        risks = []
        if row["active_total"] == 1 and row["commits"] >= 2:
            risks.append("ONE_PERSON")  # only one person changed it in a year
        if not row["active"] and scan["git"] == "OK" and any(
                _tracked(scan["tracked"], f["path"]) for f in files.get(entity_id, [])):
            risks.append("UNCHANGED_FOR_A_YEAR")  # its files are in the repository, and nobody changed them
        owner = (row["declared"][0]["owners"][0] if row["declared"] else row["active"][0]["name"] if row["active"] else None)
        out.append({"id": entity_id, "label": entity["label"], "kind": entity["kind"], "layer": entity["layer"],
                    "status": status, "owner": owner,
                    "standing": "SOURCE" if row["declared"] else "INFERRED" if row["active"] else "UNKNOWN",
                    "basis": "CODEOWNERS" if row["declared"] else "MOST_COMMITS" if row["active"] else None,
                    "through": row.get("through"), "declared": row["declared"], "active": row["active"],
                    "active_total": row["active_total"], "files": files.get(entity_id, [])[:10], "risks": risks})
    out.sort(key=lambda r: (r["layer"] != "business", r["status"] != "NO_OWNER", r["label"], r["id"]))
    people: dict[str, dict] = {}
    for row in out:
        if row["owner"] is not None:
            person = people.setdefault(row["owner"], {"owner": row["owner"], "basis": row["basis"], "owns": []})
            person["owns"].append({"id": row["id"], "label": row["label"], "layer": row["layer"], "kind": row["kind"]})
    business = [r for r in out if r["layer"] == "business" and "action" in r["id"]]

    def ratio(label, part, detail):
        return {"label": label, "part": part, "whole": len(business),
                "percent": round(100 * part / len(business)) if business else None, "detail": detail}
    contributors = sorted({name for people_on_file in scan["activity"].values() for name in people_on_file})
    return {"schema_version": 1, "kind": "OWNERSHIP", "status": "OK", "window_days": WINDOW_DAYS,
            "codeowners": scan["codeowners"]["path"], "git": scan["git"], "entities": out,
            "contributors": len(contributors),
            "people": sorted(people.values(), key=lambda p: (-len(p["owns"]), p["owner"])),
            "coverage": [ratio("Business actions with a declared owner", sum(r["status"] == "DECLARED" for r in business),
                               "CODEOWNERS names someone for the code behind it."),
                         ratio("Business actions with any owner", sum(r["status"] != "NO_OWNER" for r in business),
                               "Declared, or someone changed its code in the last year.")],
            "semantics": "DECLARED_OR_INFERRED_FROM_HISTORY_NOT_ACCOUNTABILITY", "execution_authorized": False}


def owners_by_id(found: dict | None) -> dict[str, dict]:
    """Entity ID -> the short owner line other views show."""
    return {r["id"]: {"owner": r["owner"], "basis": r["basis"], "standing": r["standing"], "status": r["status"],
                      "risks": r["risks"], "through": r.get("through")}
            for r in (found or {}).get("entities", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="a bundle from control_room.py --bundle-only")
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    found = report(json.loads(args.bundle.read_text("utf-8")), scan_repo(args.repo))
    for row in found["entities"]:
        print(f"{row['status']:<11} {row['layer']:<9} {row['label'][:40]:<40} {row['owner'] or '-'}"
              + (f"  [{', '.join(row['risks'])}]" if row["risks"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
