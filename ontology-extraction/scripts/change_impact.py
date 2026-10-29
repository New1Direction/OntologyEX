#!/usr/bin/env python3
"""What a change touches in the business, and what relies on it, before anyone merges.

    python change_impact.py --repo REPO --control-room http://127.0.0.1:8765
    python change_impact.py --repo REPO --base main --head my-branch --bundle BUNDLE.json --markdown
    python change_impact.py --repo REPO --skill-from OLD_SKILL --skill-to NEW_SKILL --bundle BUNDLE.json

The change comes from git (read-only: `git diff` and `git ls-files`, no checkout, no
hooks, no external diff programs). With no refs it is the working tree against HEAD,
untracked files included; with --base it is the working tree against that ref; with
--base and --head it is what the head branch changed since it left the base
(`base...head`). --skill-from/--skill-to add the bindings a new compiled skill version
changes (change_plan.py).

The answer comes from entity_context.change_impact over the control room's model:
systems declared in changed files, business objects and actions whose skill cites a
changed file or whose binding changed, confirmed links whose recorded evidence the
change alters (they will need re-confirming once it lands), and what relies on all of
those, with how each is known. Declared reachability, not runtime impact.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import entity_context

REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@^~-]{0,199}")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
STATUS = {"A": "A", "C": "A", "M": "M", "T": "M", "U": "M", "D": "D", "R": "R"}
TIMEOUT = 60


class DiffError(ValueError):
    pass


def _git(repo: Path, *args: str) -> bytes:
    """Run a read-only git command with no user or system config, hooks or external tools."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    try:
        run = subprocess.run(["git", "-c", f"safe.directory={repo}", "-c", "core.quotePath=false", "-C", str(repo),
                              *args], capture_output=True, timeout=TIMEOUT, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiffError(f"git is not available: {exc}") from exc
    if run.returncode != 0:
        raise DiffError("git refused: " + run.stderr.decode("utf-8", "replace").strip()[:300])
    return run.stdout


def git_changes(repo: Path, base: str | None = None, head: str | None = None) -> tuple[dict, int]:
    """The changed files (path, status, changed line ranges on the new side), and how many were left out."""
    for ref in (base, head):
        if ref is not None and (not REF.fullmatch(ref) or ".." in ref):
            raise DiffError(f"not a git ref: {ref[:80]!r}")
    if head is not None and base is None:
        raise DiffError("--head needs --base")
    spec = [f"{base}...{head}"] if head else [base or "HEAD"]
    common = ["--no-color", "--no-ext-diff", "--no-textconv", "-M", *spec, "--"]
    files: dict[str, dict] = {}
    fields = _git(repo, "diff", "--name-status", "-z", *common).decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        status = STATUS.get(fields[i][:1], "M")
        if fields[i][:1] in ("R", "C"):
            path, i = fields[i + 2], i + 3
        else:
            path, i = fields[i + 1], i + 2
        files[path] = {"path": path, "status": status, **({} if status == "D" else {"ranges": []})}
    current = None
    for line in _git(repo, "diff", "-U0", *common).decode("utf-8", "replace").splitlines():
        if line.startswith("+++ "):
            name = line[4:]
            current = files.get(name[2:]) if name.startswith("b/") else None
        elif current is not None and "ranges" in current and line.startswith("@@"):
            match = HUNK.match(line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or "1")
                start = max(start, 1)
                if len(current["ranges"]) < entity_context.MAX_RANGES:
                    current["ranges"].append([start, start + max(count, 1) - 1])
    if head is None:  # the working tree: files git does not track yet are changes too
        for path in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8", "replace").split("\0"):
            if path and path not in files:
                files[path] = {"path": path, "status": "A"}
    for row in files.values():
        if row.get("ranges") == []:
            row.pop("ranges")  # a mode or rename-only change: the whole file, lines unknown
    rows = [r for r in sorted(files.values(), key=lambda r: r["path"])
            if 0 < len(r["path"]) <= 1024 and all(" " <= ch != "\x7f" for ch in r["path"])]
    keep = rows[:entity_context.MAX_CHANGED_FILES]
    return {"files": keep}, len(files) - len(keep)


def skill_changes(old: Path, new: Path) -> list[dict]:
    """The bindings a new compiled skill version changes, by change_plan's declared reachability."""
    import change_plan
    from policy_check import MAX_TOTAL, parse_json, safe_file
    plan = change_plan.plan(old, new)
    name = parse_json(safe_file(new, "model.json", MAX_TOTAL))["name"]
    return [{"skill": name, "binding": b} for b in plan["declared_impact"]["possible_impacted_bindings"]]


WORDS = {"DECLARED_IN_CHANGED_FILE": "declared in", "CITED_BY_SKILL": "skill cites",
         "CONFIRMED_LINK_EVIDENCE_CHANGES": "confirmed link rests on", "SKILL_BINDING_CHANGED": "binding changed in",
         "BINDING_SOURCE_CHANGED": "binding source in"}


def markdown(report: dict, title: str = "Business impact of this change") -> str:
    """A short review comment: what it touches, what relies on it, what to re-confirm."""
    lines = [f"### {title}", "",
             f"{report['changed_files']} changed file(s); {report['touched_total']} entit(ies) touched; "
             f"{report['impacted_total']} more rely on them. Declared reachability from the sources, "
             "not measured runtime impact.", ""]
    if report["touched"]:
        lines += ["**Touched**", ""]
        for row in report["touched"][:30]:
            why = "; ".join(f"{WORDS.get(r['reason'], r['reason'])} `{r['path']}`"
                            + (" (changed lines)" if r["line_hit"] else "") for r in row["reasons"][:4])
            lines.append(f"- {row['label']} ({row['layer']}): {why}")
        lines.append("")
    if report["impacted"]:
        lines += ["**What relies on it**", ""]
        lines += [f"- {r['label']} ({r['standing'].lower()}, {r['depth']} hop(s) from {r['via'].split(':')[-1] or r['via']})"
                  for r in report["impacted"][:30]]
        lines.append("")
    if report["reconfirm_after_merge"]:
        lines += ["**Confirmed links to re-confirm after merge** (their recorded evidence changes)", ""]
        lines += [f"- {r['binding']} → {r['component']} (`{r['path']}`)" for r in report["reconfirm_after_merge"][:30]]
        lines.append("")
    if report["unmapped_files"]:
        lines += [f"Not mapped to any entity: {', '.join('`' + p + '`' for p in report['unmapped_files'][:20])}", ""]
    return "\n".join(lines)


def main() -> int:
    import agent_tools
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, required=True, help="the git repository the change is in")
    parser.add_argument("--base", help="compare with this ref (default HEAD)")
    parser.add_argument("--head", help="what this ref changed since it left --base (base...head)")
    parser.add_argument("--skill-from", type=Path, help="the compiled skill before")
    parser.add_argument("--skill-to", type=Path, help="the compiled skill after")
    parser.add_argument("--control-room", help="a running control room, e.g. http://127.0.0.1:8765")
    parser.add_argument("--bundle", type=Path, help="a bundle file from control_room.py --bundle-only")
    parser.add_argument("--markdown", action="store_true", help="print a short review comment instead of JSON")
    args = parser.parse_args()
    try:
        bundle = agent_tools.Source(args.control_room, args.bundle).bundle()
        changes, omitted = git_changes(args.repo.resolve(), args.base, args.head)
        if args.skill_from or args.skill_to:
            if not (args.skill_from and args.skill_to):
                raise DiffError("--skill-from and --skill-to go together")
            changes["bindings"] = skill_changes(args.skill_from, args.skill_to)
        report = {**entity_context.change_impact(bundle, changes), "files_omitted": omitted}
    except (DiffError, entity_context.ContextError, agent_tools.ToolError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)[:400]}))
        return 1
    print(markdown(report) if args.markdown else json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
