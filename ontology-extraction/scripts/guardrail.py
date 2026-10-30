#!/usr/bin/env python3
"""Guardrails: an agent's tool call held to the business rules before it runs.

    python guardrail.py init --repo REPO --skill SKILL          # propose REPO/.ontologyex/guardrails.json
    python guardrail.py check --repo REPO --tool NAME --input '{"amount": 500, ...}'
    python guardrail.py install --repo REPO                      # add the hook to REPO/.claude/settings.json
    python guardrail.py remove --repo REPO
    python guardrail.py hook --repo REPO                         # what Claude Code runs (stdin -> stdout)

A domain skill's rules carry checks over a task's inputs ("the amount must not exceed
captured minus refunded"). `guardrails.json` names which agent tools perform which task, and
where each input is in the tool's arguments:

    {"schema_version": 1, "skills": ["skills/payments-domain"],
     "guards": [{"tool": "mcp__payments__issue_refund", "skill": "payments-domain", "task": "IssueRefund",
                 "fields": {"amount_minor": "amount", "captured_minor": "payment.captured",
                            "payment_status": "payment.status", "refunded_minor": "payment.refunded",
                            "requester_authorized": "approved_by_finance"}}]}

Before a matching call runs, Claude Code asks this hook (PreToolUse), and the checks run on
the call's own arguments:

- a check fails: **deny**, naming the rules and what they say;
- the task has open questions (unknowns, conflicts, rules without a check), an input is
  missing or of the wrong type, the code the rules were read from changed since (drift.py),
  or the guard itself cannot be read: **ask** a person, saying why;
- every check passes: **no decision**. The normal permission flow applies. Passing the
  modeled checks is not an approval, so this never answers "allow".

A tool no guard names is left alone. Each decision is appended to
REPO/.ontologyex/guardrail-audit.jsonl with the rules and field names, never the values.

**Outcomes that are not known.** A guarded call that goes ahead is journaled as an intent
(REPO/.ontologyex/guardrail-intents.jsonl: the tool, a fingerprint of its arguments, never the
values). Claude Code reports how it ended: done (PostToolUse), failed (PostToolUseFailure),
or never run (PermissionDenied). A call with no recorded end (a timeout, a crash, a call still
running), or one that ended in an interruption or a timeout-like error, has an **unknown
outcome**: it may have taken effect. The next identical call (same tool, same arguments) is not
dispatched blindly: the hook asks a person to check the live state first. A later identical
call that completes, or `guardrail.py reconcile`, settles it. A failure the tool itself
reported ("insufficient funds") is known not to have taken effect.
Deny depends on the agent host honoring it; treat this as a strong check, not a security
boundary. `install` writes REPO/.claude/settings.json (outside .ontologyex): ask first.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

import evidence_grade
from policy_check import DomainError, evaluate, scalar_type

CONFIG = Path(".ontologyex/guardrails.json")
AUDIT = Path(".ontologyex/guardrail-audit.jsonl")
INTENTS = Path(".ontologyex/guardrail-intents.jsonl")
EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied")
UNCERTAIN_ERROR = re.compile(r"time[d ]?\s*out|timeout|deadline|connection (reset|closed|refused)|broken pipe|"
                             r"\b50[234]\b|temporarily unavailable|interrupted|aborted", re.I)
UNKNOWN_HORIZON = timedelta(hours=72)  # an unknown outcome older than this no longer holds identical calls
SETTINGS = Path(".claude/settings.json")
MARK = "guardrail.py"
MAX_GUARDS = 200
TOOL = re.compile(r"[A-Za-z0-9_*.-]{1,200}")
PATH = re.compile(r"[A-Za-z0-9_-]{1,64}(\.[A-Za-z0-9_-]{1,64}){0,7}")


class GuardError(ValueError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise GuardError(message)


def load(repo: Path) -> dict:
    """guardrails.json, checked: known fields, tool names or globs, argument paths or constants."""
    path = repo / CONFIG
    if not path.is_file():
        return {"skills": [], "guards": [], "on_error": "ask", "drift": True}
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError(f"guardrails.json: {exc}") from exc
    require(type(document) is dict and document.get("schema_version") == 1
            and set(document) <= {"schema_version", "skills", "guards", "on_error", "drift"},
            "guardrails.json: schema_version 1 with skills, guards, and optionally on_error and drift")
    skills = document.get("skills", [])
    require(type(skills) is list and len(skills) <= 32 and all(type(s) is str and s for s in skills),
            "guardrails.json: skills is a list of skill folders")
    on_error = document.get("on_error", "ask")
    require(on_error in ("ask", "deny"), "guardrails.json: on_error is ask or deny")
    require(type(document.get("drift", True)) is bool, "guardrails.json: drift is true or false")
    guards = document.get("guards", [])
    require(type(guards) is list and len(guards) <= MAX_GUARDS, "guardrails.json: guards is a list")
    for i, guard in enumerate(guards):
        where = f"guardrails.json guard {i + 1}"
        require(type(guard) is dict and set(guard) == {"tool", "skill", "task", "fields"}, f"{where}: tool, skill, "
                "task and fields")
        require(type(guard["tool"]) is str and TOOL.fullmatch(guard["tool"]) is not None,
                f"{where}: tool is a tool name (mcp__server__tool) or a glob of one")
        require(type(guard["skill"]) is str and type(guard["task"]) is str, f"{where}: skill and task are names")
        require(type(guard["fields"]) is dict and len(guard["fields"]) <= 64, f"{where}: fields maps task inputs")
        for field, source in guard["fields"].items():
            ok = (type(source) is str and PATH.fullmatch(source) is not None) or (
                type(source) is dict and set(source) == {"value"} and type(source["value"]) in (int, str, bool))
            require(ok, f"{where}: {field} is an argument path (a.b.c) or {{\"value\": constant}}")
    return {"skills": [(repo / s) if not Path(s).is_absolute() else Path(s) for s in skills], "guards": guards,
            "on_error": on_error, "drift": document.get("drift", True)}


def matching(guards: list[dict], tool: str) -> list[dict]:
    return [g for g in guards if fnmatch.fnmatchcase(tool, g["tool"])]


def argument(arguments: object, path: str):
    """A value in the tool's arguments by dotted path; (found, value)."""
    value = arguments
    for part in path.split("."):
        if type(value) is dict and part in value:
            value = value[part]
        elif type(value) is list and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def decide(repo: Path, tool: str, arguments: object, config: dict | None = None) -> dict:
    """The decision for one call: deny, ask or none (no decision), and why."""
    config = load(repo) if config is None else config
    guards = matching(config["guards"], tool)
    if not guards:
        return {"decision": "none", "tool": tool, "guarded": False}
    models = {}
    for path in config["skills"]:
        _, model = evidence_grade.load_skill(path)
        models[model["name"]] = (path, model)
    reasons, failed, review, checked = [], [], [], []
    for guard in guards:
        label = f"{guard['skill']}:{guard['task']}"
        if guard["skill"] not in models:
            review.append(f"{label}: the skill {guard['skill']} is not among the guard's skills")
            continue
        path, model = models[guard["skill"]]
        schema = model["inputs"].get(guard["task"])
        if schema is None:
            review.append(f"{label}: the skill has no task {guard['task']}")
            continue
        values, problems = {}, []
        for field, kind in schema.items():
            source = guard["fields"].get(field)
            if source is None:
                problems.append(f"{field} is not mapped to an argument")
                continue
            found, value = (True, source["value"]) if type(source) is dict else argument(arguments, source)
            if not found:
                problems.append(f"{field} ({source}) is not in the call")
            elif _kind(value) != kind:
                problems.append(f"{field} ({source if type(source) is str else 'constant'}) is not {kind}")
            else:
                values[field] = value
        if problems:
            review.append(f"{label}: " + "; ".join(problems))
            continue
        result = evaluate(model, guard["task"], values)
        statements = {r["id"]: r["statement"].rstrip(". ") for r in model["rules"]}
        blockers = {x["id"]: x["statement"].rstrip(". ") for x in (*model["unknowns"], *model["conflicts"])} | statements
        checked.append({"skill": guard["skill"], "task": guard["task"], "status": result["status"],
                        "passed": result["passed"], "failed": result["failed"], "blockers": result["blockers"],
                        "fields": sorted(values)})
        failed += [f"{label} rule {r}: {statements[r]}" for r in result["failed"]]
        review += [f"{label} needs a person for {b}: {blockers[b]}" for b in result["blockers"]]
        if config["drift"]:
            review += drifted(repo, path, guard)
    if failed:
        return {"decision": "deny", "tool": tool, "guarded": True, "checked": checked,
                "reason": "Blocked by the business rules: " + " | ".join(failed[:6])
                + ". If the rule is wrong, change the skill; a person can still do this by hand."}
    if review:
        return {"decision": "ask", "tool": tool, "guarded": True, "checked": checked,
                "reason": "A person should decide: " + " | ".join(review[:6])}
    return {"decision": "none", "tool": tool, "guarded": True, "checked": checked,
            "note": "the modeled checks passed; that is not an approval, so the normal permission flow applies"}


def _kind(value) -> str | None:
    try:
        return scalar_type(value)
    except DomainError:  # a float, null, list or object, or out of budget
        return None


def drifted(repo: Path, skill: Path, guard: dict) -> list[str]:
    """Whether the code the task's rules were read from changed since (drift.py): then the rules may be stale."""
    import drift  # noqa: PLC0415 - only when a guard matched
    import watermark  # noqa: PLC0415
    found = drift.skill_drift([skill], watermark.RepoFiles(repo, ttl=0))
    key = f"{guard['skill']}:action:{guard['task']}"
    return [f"{guard['skill']}:{guard['task']}: the code its rules were read from changed since the skill was built "
            "(source drift), so the rules may be out of date" for i in found["items"]
            if i["id"] == key and i["state"] == "STALE_REPAIR"]


def audit(repo: Path, answer: dict) -> None:
    """One line per guarded call: what was decided and by which rules; no argument values."""
    if not answer.get("guarded"):
        return
    path = repo / AUDIT
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": answer["tool"],
           "decision": answer["decision"], "checked": answer.get("checked", []), "reason": answer.get("reason")}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def summary(repo: Path | None, now: datetime, days: int = 7) -> dict | None:
    """What the guard decided in the last `days`: calls denied and asked, by task and rule (for the command center)."""
    if repo is None or not (repo / AUDIT).is_file():
        return None
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        lines = (repo / AUDIT).read_text("utf-8", "replace").splitlines()[-5000:]
    except OSError:
        return None
    counts = {"deny": 0, "ask": 0, "none": 0}
    rules: dict[str, dict] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if type(row) is not dict or str(row.get("at", "")) < since or row.get("decision") not in counts:
            continue
        counts[row["decision"]] += 1
        for c in row.get("checked") or []:
            if type(c) is not dict:
                continue
            for rule in (c.get("failed") or []) if row["decision"] == "deny" else []:
                key = f"{c.get('skill')}:{c.get('task')}:{rule}"
                hit = rules.setdefault(key, {"skill": c.get("skill"), "task": c.get("task"), "rule": rule, "denied": 0,
                                             "tools": []})
                hit["denied"] += 1
                if row.get("tool") not in hit["tools"]:
                    hit["tools"] = (hit["tools"] + [row.get("tool")])[:5]
    unknown = [r for r in outcomes(repo, now) if r["outcome"] == "UNKNOWN" and r.get("at", "") >= since]
    return {"days": days, "calls": counts, "denied_by_rule": sorted(rules.values(), key=lambda r: -r["denied"])[:20],
            "unknown_outcomes": [{"tool": r["tool"], "at": r["at"], "detail": r.get("detail") or "no result was recorded"}
                                 for r in unknown[-20:]]}


def fingerprint(tool: str, arguments: object) -> str:
    """Which call this is, without keeping its arguments: the tool and a hash of its arguments."""
    body = json.dumps({"tool": tool, "input": arguments}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def journal(repo: Path, row: dict) -> None:
    path = repo / INTENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def outcomes(repo: Path, now: datetime) -> list[dict]:
    """Every journaled intent with how it ended: DONE, FAILED, NOT_RUN, RECONCILED or UNKNOWN."""
    try:
        lines = (repo / INTENTS).read_text("utf-8", "replace").splitlines()[-20000:]
    except OSError:
        return []
    intents: dict[str, dict] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if type(row) is not dict or type(row.get("id")) is not str:
            continue
        if row.get("event") == "intent":
            intents[row["id"]] = {**row, "outcome": "UNKNOWN", "ended_at": None}
        elif row["id"] in intents and row.get("event") in ("done", "failed", "not_run", "reconciled"):
            intent = intents[row["id"]]
            if row["event"] == "failed":
                intent["outcome"] = "UNKNOWN" if row.get("uncertain") else "FAILED"
            else:
                intent["outcome"] = {"done": "DONE", "not_run": "NOT_RUN", "reconciled": "RECONCILED"}[row["event"]]
            intent["ended_at"], intent["detail"] = row.get("at"), row.get("detail")
    ordered = sorted(intents.values(), key=lambda r: r.get("at", ""))
    # A later identical call that completed settles an earlier unknown one: the person checked and it went through.
    for i, intent in enumerate(ordered):
        if intent["outcome"] == "UNKNOWN" and any(
                later["fingerprint"] == intent["fingerprint"] and later["outcome"] == "DONE" for later in ordered[i + 1:]):
            intent["outcome"], intent["detail"] = "RECONCILED", "a later identical call completed"
    return ordered


def unknown_before(repo: Path, tool: str, arguments: object, now: datetime, call_id: str | None) -> list[dict]:
    """Earlier identical calls whose outcome is not known, within the horizon."""
    mark = fingerprint(tool, arguments)
    since = (now - UNKNOWN_HORIZON).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [r for r in outcomes(repo, now) if r["fingerprint"] == mark and r["outcome"] == "UNKNOWN"
            and r.get("at", "") >= since and r["id"] != call_id]


def ended(repo: Path, event: dict, now: datetime) -> None:
    """PostToolUse, PostToolUseFailure or PermissionDenied: record how a journaled call ended."""
    call_id = event.get("tool_use_id") if type(event.get("tool_use_id")) is str else None
    if call_id is None:
        return
    known = {r["id"] for r in outcomes(repo, now)}
    if call_id not in known:
        return  # not a call the guard journaled
    kind = event.get("hook_event_name")
    if kind == "PostToolUse":
        journal(repo, {"event": "done", "id": call_id, "at": stamp(now)})
    elif kind == "PermissionDenied":
        journal(repo, {"event": "not_run", "id": call_id, "at": stamp(now)})
    else:
        error = str(event.get("error") or "")
        uncertain = bool(event.get("is_interrupt")) or UNCERTAIN_ERROR.search(error) is not None
        journal(repo, {"event": "failed", "id": call_id, "at": stamp(now), "uncertain": uncertain,
                       "detail": ("interrupted or timed out: it may have taken effect" if uncertain
                                  else "the tool reported an error")})


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def reconcile(repo: Path, tool: str | None, arguments: object, by: str, note: str) -> dict:
    """A person checked the live state: the unknown outcomes of these calls are settled."""
    require(bool(by.strip()), "--by names the person who checked")
    now = datetime.now(timezone.utc)
    pending = [r for r in outcomes(repo, now) if r["outcome"] == "UNKNOWN" and (tool is None or (
        r["tool"] == tool and (arguments is None or r["fingerprint"] == fingerprint(tool, arguments))))]
    for row in pending:
        journal(repo, {"event": "reconciled", "id": row["id"], "at": stamp(now), "by": by[:120], "detail": note[:300]})
    return {"status": "RECONCILED", "calls": len(pending)}


def hook(repo: Path, stdin: str, now: datetime | None = None) -> str:
    """Claude Code's hook input in, its decision JSON out ("" for no decision). Never raises.

    PreToolUse decides (and journals the intent of a guarded call that goes ahead); PostToolUse,
    PostToolUseFailure and PermissionDenied record how a journaled call ended."""
    now = now or datetime.now(timezone.utc)
    on_error = "ask"
    try:
        config = load(repo)
        on_error = config["on_error"]
        event = json.loads(stdin)
        require(type(event) is dict and type(event.get("tool_name")) is str, "not a tool event")
        if event.get("hook_event_name", "PreToolUse") != "PreToolUse":
            try:
                ended(repo, event, now)
            except OSError:
                pass
            return ""
        answer = decide(repo, event["tool_name"], event.get("tool_input"), config)
        call_id = event.get("tool_use_id") if type(event.get("tool_use_id")) is str else None
        if answer["guarded"] and answer["decision"] != "deny":
            earlier = unknown_before(repo, event["tool_name"], event.get("tool_input"), now, call_id)
            if earlier:
                answer = {**answer, "decision": "ask", "unknown_outcome": [r["id"] for r in earlier],
                          "reason": (f"An identical call at {earlier[-1]['at']} has no known outcome "
                                     f"({earlier[-1].get('detail') or 'no result was recorded'}): it may already "
                                     "have taken effect. Check the live state before running it again; "
                                     "guardrail.py reconcile records that someone did.")
                          + (" " + answer["reason"] if answer.get("reason") else "")}
            if call_id is not None:
                journal(repo, {"event": "intent", "id": call_id, "at": stamp(now), "tool": event["tool_name"],
                               "fingerprint": fingerprint(event["tool_name"], event.get("tool_input"))})
    except (GuardError, DomainError, OSError, ValueError, KeyError, TypeError) as exc:
        answer = {"decision": on_error, "tool": "?", "guarded": True,
                  "reason": f"The business-rule guard could not check this call ({str(exc)[:200]}); "
                            "a person should decide."}
    try:
        audit(repo, answer)
    except OSError:
        pass
    if answer["decision"] == "none":
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": answer["decision"],
                                              "permissionDecisionReason": answer["reason"][:1000]}})


def matcher(guards: list[dict]) -> str:
    """Claude Code's matcher for the guarded tools: an exact list, or an anchored regex when a guard is a glob."""
    tools = sorted({g["tool"] for g in guards})
    if all(re.fullmatch(r"[A-Za-z0-9_-]+", t) for t in tools):
        return "|".join(tools)
    parts = [re.sub(r"\\\*", ".*", re.escape(t)) for t in tools]
    return "^(?:" + "|".join(parts) + ")$"


def command(repo: Path) -> str:
    return " ".join(shlex.quote(str(p)) for p in (sys.executable, Path(__file__).resolve(), "hook", "--repo",
                                                    repo.resolve()))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False,
                                     encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    Path(handle.name).replace(path)


def install(repo: Path) -> dict:
    """Add (or refresh) the guard's PreToolUse entry in the project's Claude Code settings, keeping the rest."""
    config = load(repo)
    require(bool(config["guards"]), "no guards in .ontologyex/guardrails.json; run init and edit it first")
    path = repo / SETTINGS
    settings = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    require(type(settings) is dict, f"{SETTINGS} is not a JSON object")
    hooks = settings.setdefault("hooks", {})
    require(type(hooks) is dict, f"{SETTINGS}: hooks is not an object")
    found = matcher(config["guards"])
    for event in EVENTS:  # decide before; record how each call ended after
        groups = [g for g in hooks.get(event, []) if not _ours(g)]
        groups.append({"matcher": found, "hooks": [{"type": "command", "command": command(repo), "timeout": 30}]})
        hooks[event] = groups
    write_json(path, settings)
    return {"status": "INSTALLED", "settings": str(path), "matcher": found, "events": list(EVENTS)}


def remove(repo: Path) -> dict:
    path = repo / SETTINGS
    if not path.is_file():
        return {"status": "NOT_INSTALLED"}
    settings = json.loads(path.read_text("utf-8"))
    removed = 0
    for event in EVENTS:
        groups = (settings.get("hooks") or {}).get(event, [])
        kept = [g for g in groups if not _ours(g)]
        removed += len(groups) - len(kept)
        if len(kept) != len(groups):
            settings["hooks"][event] = kept
            if not kept:
                del settings["hooks"][event]
    if not removed:
        return {"status": "NOT_INSTALLED"}
    write_json(path, settings)
    return {"status": "REMOVED", "settings": str(path)}


def _ours(group) -> bool:
    return type(group) is dict and any(type(h) is dict and MARK in str(h.get("command", "")) and " hook " in
                                       str(h.get("command", "")) for h in group.get("hooks", []))


def init(repo: Path, skills: list[Path]) -> dict:
    """A guardrails.json to edit: one guard per task that has inputs, every field mapped to an argument of its name."""
    path = repo / CONFIG
    require(not path.exists(), f"{CONFIG} exists; edit it")
    guards = []
    for skill in skills:
        _, model = evidence_grade.load_skill(skill)
        for task, fields in sorted(model["inputs"].items()):
            if fields:
                guards.append({"tool": f"mcp__SERVER__{re.sub(r'(?<!^)(?=[A-Z])', '_', task).lower()}",
                               "skill": model["name"], "task": task, "fields": {f: f for f in sorted(fields)}})
    rel = [os.path.relpath(s.resolve(), repo.resolve()) for s in skills]
    write_json(path, {"schema_version": 1, "skills": rel, "guards": guards, "on_error": "ask", "drift": True})
    return {"status": "PROPOSED", "file": str(path), "guards": len(guards),
            "next": "Set each tool to the agent tool that performs the task, and each field to where it is in that "
                    "tool's arguments; then run check, then install."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "check", "install", "remove", "hook", "reconcile", "outcomes"):
        p = sub.add_parser(name)
        p.add_argument("--repo", type=Path, required=True)
        if name == "init":
            p.add_argument("--skill", type=Path, action="append", required=True)
        if name in ("check", "reconcile"):
            p.add_argument("--tool", required=name == "check")
            p.add_argument("--input", default=None, help="the tool's arguments, as JSON")
        if name == "reconcile":
            p.add_argument("--by", required=True, help="who checked the live state")
            p.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if args.command == "hook":  # Claude Code reads stdout; nothing else may be printed
        sys.stdout.write(hook(args.repo, sys.stdin.read()))
        return 0
    try:
        require(args.repo.is_dir(), "--repo is not a directory")
        if args.command == "init":
            result = init(args.repo, args.skill)
        elif args.command == "check":
            result = decide(args.repo, args.tool, json.loads(args.input or "{}"))
        elif args.command == "reconcile":
            result = reconcile(args.repo, args.tool, json.loads(args.input) if args.input else None, args.by, args.note)
        elif args.command == "outcomes":
            result = {"outcomes": outcomes(args.repo, datetime.now(timezone.utc))[-50:]}
        elif args.command == "install":
            result = install(args.repo)
        else:
            result = remove(args.repo)
        print(json.dumps(result, indent=2))
        return 0
    except (GuardError, DomainError, OSError, ValueError) as exc:
        print(f"guardrail: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
