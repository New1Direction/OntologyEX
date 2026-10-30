#!/usr/bin/env python3
"""Replays: what the control room concluded from what was known at a moment, held to what really happened.

    python replay.py capture --repo REPO --name payments-outage [--at TIME] [--observations FILE]
                             [--skill SKILL ...] [--links FILE] [--events FILE] [--dependencies FILE]
                             [--rules FILE] [--thresholds FILE] [--window-hours 24]
    python replay.py run REPO/.ontologyex/replays/*  [--format json|markdown]

`capture` saves what the control room reads, as of a moment, into
REPO/.ontologyex/replays/NAME/: the discovered topology, the health observations up to that
moment (the last --window-hours), the skills, links, events, dependencies and rules, and the
operations ledger (aggregates only, never an export). It writes `case.json` with an empty
`expect`: say what really happened (the verdict and the cause) once it is known.

`run` rebuilds each case through the control room's own assembly (`--bundle-only` with the saved
topology and `--now` set to the moment), so only what was known then is used, and asks:

- verdict: did it say FAULT, DEGRADED, HEALTHY or INCONCLUSIVE as expected? Missing or expired
  evidence must never read HEALTHY;
- cause: is the expected cause among the first N causes the ranking names (default 3)?
- citations: what share of the top conclusions cite evidence the case holds (an observation, a
  cited file, a decision)? Expected: all of them;
- reads only: the case is replayed from a copy, no repository is read and nothing is written.

The repository is not in a case, so confirmations are not re-checked against it in a replay.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import command_center  # noqa: E402

KIND = "ONTOLOGYEX_REPLAY_CASE"
VERDICTS = ("FAULT", "DEGRADED", "HEALTHY", "INCONCLUSIVE")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
TOP_CONCLUSIONS = 5
FILES = ("links.json", "events.json", "dependencies.json", "rules.json", "thresholds.json")


class ReplayError(ValueError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ReplayError(message)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(STAMP)


def parse(text: str) -> datetime:
    try:
        return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ReplayError(f"times are UTC, like 2026-10-30T08:14:00Z (got {text!r})") from None


# ---------------------------------------------------------------- capture

def topology_of(repo: Path) -> dict:
    """The topology the control room draws for a repository now: manifests, the code surface, the suppliers."""
    import code_surface
    import control_room
    import discover_repo
    import suppliers
    topology = discover_repo.discover(repo)
    surface = code_surface.scan(repo, "auto")
    topology = control_room.with_code_surface(topology, repo, surface)
    try:
        topology = suppliers.with_suppliers(topology, suppliers.scan(repo, topology["nodes"]), repo)
    except (ValueError, OSError):
        pass
    return topology


def capture(repo: Path, name: str, at: datetime | None = None, observations: Path | None = None,
            skills: list[Path] = (), window_hours: int = 24, out: Path | None = None, environment: str | None = None,
            **files: Path | None) -> dict:
    require(NAME.fullmatch(name) is not None, "name: lowercase letters, digits, '.', '_' and '-'")
    require(1 <= window_hours <= 24 * 30, "--window-hours is 1 to 720")
    at = at or datetime.now(timezone.utc).replace(microsecond=0)
    target = (out or repo / ".ontologyex" / "replays") / name
    require(not target.exists(), f"{target} exists; choose another name")
    import control_room
    import operations
    target.mkdir(parents=True)
    write = lambda rel, value: (target / rel).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")
    write("topology.json", topology_of(repo))
    since = at - timedelta(hours=window_hours)
    kept = 0
    if observations is not None and observations.is_file():
        rows = [r for r in control_room.read_observations(observations)
                if since <= control_room.timestamp(r["observed_at"]) <= at]
        (target / "observations.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), "utf-8")
        kept = len(rows)
    for skill in skills:
        shutil.copytree(skill, target / "skills" / skill.name, ignore=shutil.ignore_patterns("__pycache__"))
    for rel in FILES:
        source = files.get(rel.split(".")[0])
        if source is not None and source.is_file():
            shutil.copyfile(source, target / rel)
    ledger = operations.report(repo / ".ontologyex" / "operations", now=at)  # aggregates only
    if ledger.get("status") == "OK":
        write("operations.json", ledger)
    case = {"schema_version": 1, "kind": KIND, "name": name, "at": stamp(at), "environment": environment,
            "captured": {"at": stamp(datetime.now(timezone.utc)), "observations_kept": kept,
                         "window_hours": window_hours, "skills": [s.name for s in skills],
                         "topology": "as discovered when captured (the repository is not copied)"},
            "expect": {"verdict": None, "cause": [], "within": 3,
                       "note": "Say what really happened: FAULT, DEGRADED, HEALTHY or INCONCLUSIVE, and the "
                               "component or business item that was the cause."},
            "notes": ""}
    write("case.json", case)
    found = evaluate(target)
    return {"status": "CAPTURED", "case": str(target), "observations": kept, "concluded": found["verdict"],
            "top": found["top"][:5]}


# ---------------------------------------------------------------- run

def load_case(folder: Path) -> dict:
    try:
        case = json.loads((folder / "case.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayError(f"{folder}: no readable case.json ({exc})") from None
    require(type(case) is dict and case.get("kind") == KIND and case.get("schema_version") == 1,
            f"{folder}: not a replay case")
    parse(case.get("at"))
    expect = case.get("expect") or {}
    require(expect.get("verdict") in (None, *VERDICTS), f"{folder}: expect.verdict is one of {', '.join(VERDICTS)}")
    require(type(expect.get("cause", [])) is list and all(type(c) is str for c in expect.get("cause", [])),
            f"{folder}: expect.cause is a list of ids")
    require(type(expect.get("within", 3)) is int and 1 <= expect.get("within", 3) <= 20, f"{folder}: expect.within 1..20")
    require((folder / "topology.json").is_file(), f"{folder}: no topology.json")
    return case


def bundle_of(folder: Path, case: dict) -> dict:
    """The control room's bundle for the case, as of its moment, from a copy (so nothing can touch the case)."""
    with tempfile.TemporaryDirectory(prefix="ontologyex-replay-") as tmp:
        copy = Path(tmp) / "case"
        shutil.copytree(folder, copy)
        command = [sys.executable, str(HERE / "control_room.py"), "--topology", str(copy / "topology.json"),
                   "--now", case["at"], "--bundle-only", "--discovery-engine", "python", "--view-engine", "python",
                   *(["--environment", case["environment"]] if case.get("environment") else [])]
        if (copy / "observations.jsonl").is_file():
            command += ["--observations", str(copy / "observations.jsonl")]
        for skill in sorted((copy / "skills").iterdir()) if (copy / "skills").is_dir() else []:
            command += ["--skill", str(skill)]
        for rel, flag in (("links.json", "--links"), ("events.json", "--events"), ("dependencies.json", "--dependencies"),
                          ("rules.json", "--rules"), ("thresholds.json", "--thresholds"),
                          ("operations.json", "--operations-report")):
            if (copy / rel).is_file():
                command += [flag, str(copy / rel)]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ONTOLOGYEX_GITHUB", "STRIPE", "HUBSPOT",
                                                                           "ZENDESK", "AWS_"))}
        run = subprocess.run(command, capture_output=True, text=True, timeout=300, env=env, cwd=tmp)
        if run.returncode != 0:
            raise ReplayError(f"{folder.name}: the control room could not rebuild it: "
                              f"{(run.stdout or run.stderr).strip()[-300:]}")
        return json.loads(run.stdout)


def cites(item: dict, bundle: dict) -> bool:
    """Whether a conclusion names evidence the case holds: an observation, a cited file or a decision."""
    if item.get("evidence"):  # command_center.attach_evidence: the records a conclusion rests on
        return any(e.get("role") == "basis" and e.get("state") in ("COMPLETE", "PARTIAL") for e in item["evidence"])
    nodes = {n["id"]: n for n in bundle["state"].get("nodes", [])}
    evidence = {e["id"] for e in bundle["state"].get("source_evidence", [])}
    picture = bundle.get("business") or {}
    rows = {r["key"]: r for r in [*picture.get("actions", []), *picture.get("objects", [])]}
    for key in [*([item["subject"]["id"]] if item.get("subject") else []), *item.get("links", [])]:
        node = nodes.get(key)
        if node is not None and (node.get("observed_at") or node.get("evidence_id") in evidence):
            return True
        if key in rows and rows[key].get("evidence"):
            return True
    return False


def evaluate(folder: Path) -> dict:
    case = load_case(folder)
    before = digest(folder)
    bundle = bundle_of(folder, case)
    report = command_center.report(bundle)
    verdict = report["verdict"]
    conclusions = [i for i in report["items"] if i.get("standing") in ("SOURCE", "INFERRED")][:TOP_CONCLUSIONS]
    cited = [i for i in conclusions if cites(i, bundle)]
    expect = case.get("expect") or {}
    within = expect.get("within", 3)
    ranked = list(dict.fromkeys(verdict["causes"] + [k for i in report["items"]
                                                    for k in ([i["subject"]["id"]] if i.get("subject") else [])]))
    checks = {}
    if expect.get("verdict"):
        checks["verdict"] = verdict["status"] == expect["verdict"]
    if expect.get("verdict") in ("FAULT", "DEGRADED") and expect.get("cause"):
        checks["cause_within"] = any(c in ranked[:within] for c in expect["cause"])
    if expect.get("verdict") == "INCONCLUSIVE":
        checks["no_cause_invented"] = not verdict["causes"]
    checks["never_healthy_without_evidence"] = not (verdict["status"] == "HEALTHY" and verdict["evidence"]["missing"])
    checks["citations"] = len(cited) == len(conclusions)
    checks["read_only"] = digest(folder) == before
    return {"case": case["name"], "at": case["at"], "expected": {k: expect.get(k) for k in ("verdict", "cause", "within")},
            "verdict": verdict, "top": [{"title": i["title"], "kind": i["kind"], "score": i["score"],
                                         "subject": (i.get("subject") or {}).get("id"), "next_step": i["next_step"]}
                                        for i in report["items"][:10]],
            "causes_ranked": ranked[:10],
            "citation_coverage": round(len(cited) / len(conclusions), 3) if conclusions else None,
            "uncited": [i["title"] for i in conclusions if i not in cited],
            "checks": checks, "scored": bool(expect.get("verdict")), "passed": all(checks.values())}


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(path.relative_to(folder).as_posix().encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


def run(folders: list[Path]) -> dict:
    results = [evaluate(f) for f in folders]
    scored = [r for r in results if r["scored"]]
    faults = [r for r in scored if "cause_within" in r["checks"]]
    covered = [r["citation_coverage"] for r in results if r["citation_coverage"] is not None]
    return {"schema_version": 1, "kind": "ONTOLOGYEX_REPLAY_REPORT", "cases": results,
            "summary": {"cases": len(results), "scored": len(scored), "passed": sum(r["passed"] for r in results),
                        "verdicts_right": sum(r["checks"].get("verdict", False) for r in scored),
                        "cause_within_rate": round(sum(r["checks"]["cause_within"] for r in faults) / len(faults), 3)
                        if faults else None,
                        "citation_coverage": round(sum(covered) / len(covered), 3) if covered else None,
                        "healthy_without_evidence": sum(not r["checks"]["never_healthy_without_evidence"]
                                                        for r in results)},
            "semantics": "REPLAYED_FROM_WHAT_WAS_KNOWN_AT_EACH_MOMENT_NOT_LIVE_STATE"}


def markdown(report: dict) -> str:
    s = report["summary"]
    lines = ["# Replays", "",
             f"{s['passed']} of {s['cases']} case(s) passed; {s['verdicts_right']} of {s['scored']} verdict(s) right"
             + (f"; cause within the top N: {int(s['cause_within_rate'] * 100)}%" if s["cause_within_rate"] is not None else "")
             + (f"; citation coverage {int(s['citation_coverage'] * 100)}%" if s["citation_coverage"] is not None else "")
             + ".", "", "| Case | At | Expected | Concluded | Causes | Cited | Result |", "|---|---|---|---|---|---|---|"]
    for r in report["cases"]:
        failed = [k for k, ok in r["checks"].items() if not ok]
        lines.append(f"| {r['case']} | {r['at']} | {r['expected']['verdict'] or 'not set'} | {r['verdict']['status']} "
                     f"({r['verdict']['confidence'].lower()}) | {', '.join(c.rsplit(':', 1)[-1] for c in r['verdict']['causes'][:3]) or '–'} "
                     f"| {'–' if r['citation_coverage'] is None else str(int(r['citation_coverage'] * 100)) + '%'} "
                     f"| {'pass' if not failed else 'FAIL: ' + ', '.join(failed)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="save what the control room reads now (or at --at) as a replay case")
    cap.add_argument("--repo", type=Path, required=True)
    cap.add_argument("--name", required=True)
    cap.add_argument("--at", help="the moment, UTC (default: now)")
    cap.add_argument("--observations", type=Path)
    cap.add_argument("--skill", type=Path, action="append", default=[])
    for flag in ("links", "events", "dependencies", "rules", "thresholds"):
        cap.add_argument(f"--{flag}", type=Path)
    cap.add_argument("--window-hours", type=int, default=24)
    cap.add_argument("--environment", help="the environment the observations come from (production, staging...)")
    cap.add_argument("--out", type=Path, help="where cases go (default: REPO/.ontologyex/replays)")
    rep = sub.add_parser("run", help="replay cases and score them")
    rep.add_argument("cases", type=Path, nargs="+")
    rep.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            require(args.repo.is_dir(), "--repo is not a directory")
            print(json.dumps(capture(args.repo, args.name, parse(args.at) if args.at else None, args.observations,
                                     args.skill, args.window_hours, args.out, args.environment,
                                     links=args.links, events=args.events,
                                     dependencies=args.dependencies, rules=args.rules, thresholds=args.thresholds),
                             indent=2))
            return 0
        folders = [f for f in args.cases if f.is_dir()]
        require(bool(folders), "no case folders given")
        report = run(folders)
        print(json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else markdown(report))
        return 0 if all(r["passed"] for r in report["cases"]) else 1
    except ReplayError as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
