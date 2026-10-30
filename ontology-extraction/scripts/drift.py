#!/usr/bin/env python3
"""Source drift: whether the lines the model rests on are still there, moved, or changed.

    python drift.py check --repo REPO --skill SKILL [--rules RULES.json] [--format markdown]
    python drift.py hook install --repo REPO --skill SKILL [--rules RULES.json]
    python drift.py hook remove --repo REPO

Everything the model knows cites lines: a domain skill's evidence spans (an exact quote
of path:start-end), a confirmed rule's citation, a confirmed link's binding. Code moves
under them. Here each cited span is found again in the repository as it is now:

- UNCHANGED: the same text at the same lines.
- REFORMATTED: the same text once whitespace is evened out (indentation, spacing, blank
  lines) at about the same place. Comments count as text: a comment is often where a rule
  is written down.
- MOVED: the same text (exactly, or reformatted) at other lines of the file. The span is
  repaired by reading it at its new lines; nothing else changes, so nobody needs to look.
- CHANGED: the text is not in the file any more. The nearest lines that look like it are
  named, for a person to read. Anything resting on it needs repair (STALE_REPAIR).
- FILE_GONE: the file is not in the repository (or not found under one unique path).

A business object or action whose evidence CHANGED or is GONE reads STALE_REPAIR until its
skill is rebuilt from the current code (or the evidence re-cited): the model describes code
that is no longer there. Reads files only; `hook install` writes one git hook, on request,
that prints this after each commit (it never blocks a commit).
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import evidence_grade
from policy_check import DomainError, safe_file
import watermark

MAX_SPAN_LINES = 120
MAX_FILE_LINES = 50_000
NEAREST_MIN = 0.5
NEAREST_MAX_LINES = 5_000  # a file longer than this is not searched for look-alike lines
REPAIR = ("CHANGED", "FILE_GONE")
HOOK_MARK = "# ontologyex-drift-hook"
SPACE = re.compile(r"\s+")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normal(lines: list[str]) -> list[tuple[int, str]]:
    """(index, line with whitespace evened out) for each non-blank line."""
    return [(i, SPACE.sub(" ", line).strip()) for i, line in enumerate(lines) if line.strip()]


def fingerprint(text: str) -> dict:
    """What a decision records about a span: its exact hash, and the hash of its normalized lines."""
    norm = [n for _, n in normal(text.split("\n"))]
    return {"sha256": sha(text), "norm_sha256": sha("\n".join(norm)), "norm_lines": len(norm)}


def read_lines(files: watermark.RepoFiles, path: str) -> tuple[str | None, list[str] | None]:
    rel = files.find(path)
    if rel is None:
        return None, None
    try:
        text = safe_file(files.repo, rel, watermark.MAX_BYTES).decode("utf-8", "replace")
    except (DomainError, OSError, ValueError):
        return rel, None
    return rel, text.split("\n")[:MAX_FILE_LINES]


def _nearest(lines: list[str], quote: str, start: int) -> dict | None:
    """The lines most like the quote (a window of the same length), when they are close enough to name."""
    k = max(1, quote.count("\n") + 1)
    want = SPACE.sub(" ", quote).strip()
    best, at = 0.0, None
    for i in range(0, max(1, len(lines) - k + 1)):
        window = SPACE.sub(" ", "\n".join(lines[i:i + k])).strip()
        matcher = difflib.SequenceMatcher(None, want, window, autojunk=False)
        if matcher.real_quick_ratio() < best or matcher.quick_ratio() < best:
            continue
        ratio = matcher.ratio()
        if ratio > best or (ratio == best and at is not None and abs(i + 1 - start) < abs(at - start)):
            best, at = ratio, i + 1
    if at is None or best < NEAREST_MIN:
        return None
    return {"start_line": at, "end_line": at + k - 1, "similarity": round(best, 2)}


def locate(lines: list[str] | None, start: int, end: int, quote: str | None = None,
           recorded: dict | None = None) -> dict:
    """Where a cited span is now. Give the quote (skill evidence) or its recorded fingerprint (a decision)."""
    if lines is None:
        return {"status": "FILE_GONE"}
    if quote is not None:
        recorded = fingerprint(quote)
    if not recorded or not recorded.get("sha256"):
        return {"status": "NOT_RECORDED"}
    k = end - start + 1
    if not 1 <= k <= MAX_SPAN_LINES:
        return {"status": "NOT_RECORDED"}
    if end <= len(lines) and sha("\n".join(lines[start - 1:end])) == recorded["sha256"]:
        return {"status": "UNCHANGED", "start_line": start, "end_line": end}
    exact = [i + 1 for i in range(0, max(0, len(lines) - k + 1)) if sha("\n".join(lines[i:i + k])) == recorded["sha256"]]
    if exact:
        at = min(exact, key=lambda s: (abs(s - start), s))
        return {"status": "MOVED", "start_line": at, "end_line": at + k - 1, "from": [start, end],
                **({"matches": len(exact)} if len(exact) > 1 else {})}
    n = recorded.get("norm_lines")
    if type(n) is int and 0 < n <= MAX_SPAN_LINES and recorded.get("norm_sha256"):
        rows = normal(lines)
        found = [j for j in range(0, max(0, len(rows) - n + 1))
                 if sha("\n".join(t for _, t in rows[j:j + n])) == recorded["norm_sha256"]]
        if found:
            j = min(found, key=lambda j: (abs(rows[j][0] + 1 - start), j))
            s, e = rows[j][0] + 1, rows[j + n - 1][0] + 1
            near = s <= end and e >= start  # overlaps where it was: the same place, reformatted
            return {"status": "REFORMATTED" if near else "MOVED", "start_line": s, "end_line": e,
                    **({"from": [start, end]} if (s, e) != (start, end) else {}),
                    **({} if near else {"reformatted": True}),
                    **({"matches": len(found)} if len(found) > 1 else {})}
    out = {"status": "CHANGED", "from": [start, end]}
    if quote is not None and len(lines) <= NEAREST_MAX_LINES:
        nearest = _nearest(lines, quote, start)
        if nearest:
            out["nearest"] = nearest
    return out


def skill_drift(skills: list[Path], files: watermark.RepoFiles) -> dict:
    """Each evidence span of each skill found again in the repository, and the business items resting on it."""
    spans, items = [], []
    for path in skills:
        _, model = evidence_grade.load_skill(path)
        name = model["name"]
        compiled = model.get("source_files") or {}
        cache: dict[str, tuple] = {}
        status: dict[str, dict] = {}
        for e in model["evidence"]:
            cited = e["path"]
            if cited not in cache:
                rel, lines = read_lines(files, cited)
                current = files.sha256(rel) if rel is not None and lines is not None else None
                cache[cited] = (rel, lines, current)
            rel, lines, current = cache[cited]
            if current is not None and current == (compiled.get(cited) or {}).get("sha256"):
                found = {"status": "UNCHANGED", "start_line": e["start_line"], "end_line": e["end_line"]}
            else:
                found = locate(lines, e["start_line"], e["end_line"], quote=e["quote"])
            status[e["id"]] = found
            spans.append({"skill": name, "evidence": e["id"], "path": rel or cited, "cited": cited,
                          "start_line": e["start_line"], "end_line": e["end_line"], **found})
        layers = model["layers"]
        concepts = layers["40-application.yaml"].get("concepts", [])
        rows = []
        for cls in layers["20-domain.yaml"].get("classes", []):
            ids = [cls.get("source")] + [c.get("source") for c in concepts if c.get("binds") == cls["id"]]
            rows.append((f"{name}:object:{cls['id']}", cls["id"], "business_object", ids))
        for task in layers["30-task.yaml"].get("tasks", []):
            ids = ([task.get("source")] + [x for r in model["rules"] if r["task"] == task["id"] for x in r["evidence"]]
                   + [c.get("source") for c in concepts if task["id"] in c.get("used_by_tasks", [])])
            rows.append((f"{name}:action:{task['id']}", task["id"], "business_action", ids))
        for key, label, kind, ids in rows:
            cited = [i for i in dict.fromkeys(ids) if i in status]
            broken = [i for i in cited if status[i]["status"] in REPAIR]
            moved = [i for i in cited if status[i]["status"] in ("MOVED", "REFORMATTED")]
            items.append({"id": key, "label": label, "kind": kind, "skill": name,
                          "state": "STALE_REPAIR" if broken else "REPAIRED" if moved else "CURRENT",
                          "evidence": cited, "changed": broken, "moved": moved})
    counts: dict[str, int] = {}
    for s in spans:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"spans": spans, "items": items, "counts": dict(sorted(counts.items()))}


def rules_drift(rules: Path | None, files: watermark.RepoFiles) -> list[dict]:
    if rules is None or not rules.is_file():
        return []
    import rule_suggest  # noqa: PLC0415 - rule_suggest imports this module
    confirmed, _ = rule_suggest.read_rule_document(rules)
    return [{"target": r["target"], "statement": r["statement"], "citation": r["citation"],
             **rule_suggest.citation_drift(files, r)} for r in confirmed]


def scan(repo: Path, skills: list[Path], rules: Path | None = None) -> dict:
    """The drift report: skill evidence, the business items resting on it, and confirmed rules' citations."""
    files = watermark.RepoFiles(repo, ttl=0)
    return assemble(skill_drift(skills, files) if skills else None, rules_drift(rules, files))


def assemble(found: dict | None, rule_rows: list[dict]) -> dict:
    """The report from the skills' part (a background scan in the control room) and the rules' part (read live)."""
    found = found or {"spans": [], "items": [], "counts": {}}
    stale = [i for i in found["items"] if i["state"] == "STALE_REPAIR"]
    return {"schema_version": 1, "kind": "SOURCE_DRIFT", "counts": found["counts"],
            "summary": {"spans": len(found["spans"]), "stale_items": len(stale),
                        "repaired_items": sum(i["state"] == "REPAIRED" for i in found["items"]),
                        "rules_changed": sum(r["status"] in REPAIR for r in rule_rows),
                        "rules_moved": sum(r["status"] in ("MOVED", "REFORMATTED") for r in rule_rows)},
            "items": found["items"], "spans": found["spans"], "rules": rule_rows,
            "semantics": "CITED_LINES_FOUND_AGAIN_NOT_WHETHER_THEY_STILL_SUPPORT_THE_CLAIM",
            "execution_authorized": False}


def markdown(report: dict) -> str:
    s = report["summary"]
    lines = ["# Source drift", "",
             f"{s['spans']} cited span(s): " + (", ".join(f"{v} {k.lower().replace('_', ' ')}"
                                                          for k, v in report["counts"].items()) or "none") + ".", ""]
    stale = [i for i in report["items"] if i["state"] == "STALE_REPAIR"]
    if stale:
        lines += ["## Needs repair", ""]
        by_id = {(x["skill"], x["evidence"]): x for x in report["spans"]}
        for item in stale:
            lines.append(f"- **{item['label']}** ({item['kind'].replace('_', ' ')}, {item['skill']})")
            for e in item["changed"]:
                x = by_id[(item["skill"], e)]
                near = x.get("nearest")
                lines.append(f"  - `{x['path']}:{x['start_line']}-{x['end_line']}` {x['status'].lower().replace('_', ' ')}"
                             + (f"; nearest now `{x['path']}:{near['start_line']}-{near['end_line']}` "
                                f"({int(near['similarity'] * 100)}% alike)" if near else ""))
        lines.append("")
    moved = [x for x in report["spans"] if x["status"] in ("MOVED", "REFORMATTED")]
    if moved:
        lines += ["## Moved (repaired by reading them at their new lines)", ""] + [
            f"- `{x['path']}:{x['start_line']}-{x['end_line']}` ({x['status'].lower()}"
            + (f" from {x['from'][0]}-{x['from'][1]}" if x.get("from") else "") + f"), {x['skill']} {x['evidence']}"
            for x in moved[:50]] + [""]
    rules = [r for r in report["rules"] if r["status"] not in ("UNCHANGED", "NOT_RECORDED")]
    if rules:
        lines += ["## Confirmed rules", ""] + [
            f"- {r['statement']}: `{r['citation']['path']}:{r['citation']['start_line']}-{r['citation']['end_line']}` "
            f"{r['status'].lower().replace('_', ' ')}" + (f" (now {r['start_line']}-{r['end_line']})" if r.get("start_line") else "")
            for r in rules] + [""]
    if not stale and not moved and not rules:
        lines += ["Every cited span is where it was.", ""]
    return "\n".join(lines)


def hook_path(repo: Path) -> Path:
    """Where git looks for hooks in this repository (core.hooksPath, worktrees), asked of git read-only."""
    run = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"], capture_output=True, text=True,
                         timeout=30)
    if run.returncode != 0:
        raise DomainError("not a git repository")
    hooks = Path(run.stdout.strip())
    return (hooks if hooks.is_absolute() else repo / hooks) / "post-commit"


def install_hook(repo: Path, skills: list[Path], rules: Path | None) -> dict:
    target = hook_path(repo)
    if target.exists() and HOOK_MARK not in target.read_text("utf-8", "replace"):
        raise DomainError(f"{target} exists and is not ours; add this line to it instead: "
                          + hook_command(repo, skills, rules))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"#!/bin/sh\n{HOOK_MARK}: prints what this commit moved or changed under the model; "
                      "never blocks\n" + hook_command(repo, skills, rules) + " || true\n", "utf-8")
    target.chmod(0o755)
    return {"status": "INSTALLED", "hook": str(target)}


def hook_command(repo: Path, skills: list[Path], rules: Path | None) -> str:
    return " ".join(shlex.quote(str(p)) for p in [
        sys.executable, Path(__file__).resolve(), "check", "--repo", repo.resolve(), "--quiet-when-current",
        *[x for s in skills for x in ("--skill", s.resolve())], *(["--rules", rules.resolve()] if rules else [])])


def remove_hook(repo: Path) -> dict:
    target = hook_path(repo)
    if not target.exists():
        return {"status": "NOT_INSTALLED"}
    if HOOK_MARK not in target.read_text("utf-8", "replace"):
        raise DomainError(f"{target} is not ours; leaving it")
    target.unlink()
    return {"status": "REMOVED", "hook": str(target)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="find every cited span again and say what moved or changed")
    hook = sub.add_parser("hook", help="install or remove a post-commit hook that prints the check")
    hook.add_argument("action", choices=("install", "remove"))
    for p in (check, hook):
        p.add_argument("--repo", type=Path, required=True)
        p.add_argument("--skill", type=Path, action="append", default=[])
        p.add_argument("--rules", type=Path)
    check.add_argument("--format", choices=("json", "markdown"), default="markdown")
    check.add_argument("--quiet-when-current", action="store_true", help="print nothing when nothing needs repair")
    check.add_argument("--fail-on-stale", action="store_true", help="exit 1 when something needs repair (for CI)")
    args = parser.parse_args(argv)
    try:
        if not args.repo.is_dir():
            raise DomainError("--repo is not a directory")
        if args.command == "hook":
            result = (install_hook(args.repo, args.skill, args.rules) if args.action == "install"
                      else remove_hook(args.repo))
            print(json.dumps(result, indent=2))
            return 0
        report = scan(args.repo, args.skill, args.rules)
        needs = report["summary"]["stale_items"] + report["summary"]["rules_changed"]
        if not (args.quiet_when_current and not needs):
            print(json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else markdown(report))
        return 1 if args.fail_on_stale and needs else 0
    except (DomainError, OSError, ValueError, KeyError) as exc:
        print(f"drift: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
