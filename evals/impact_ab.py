#!/usr/bin/env python3
"""Compare change-impact answers with the same offline read-only coding shell.

The benchmark is predeclared in change-impact.json. Both arms get shell access
inside the same Docker image; the treatment additionally gets OntologyEX's
read-only tools, including analyze_change and analyze_impact. Source anchors
are checked before calls. Citation validity is mechanical; semantic grading is
intentionally left to a reviewer. Run only on public or explicitly authorized
repositories because questions and tool output are sent to the model provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import agent_tools  # noqa: E402

ONTOLOGY_TOOLS = ("check_answerable", "search_entities", "get_entity_context", "explain_relationship",
                  "analyze_impact", "analyze_change", "what_needs_attention", "source_drift")
SYSTEM = (
    "You review proposed code changes in the repository available at /repo. Inspect source and relevant tests "
    "to answer the question. You have an offline read-only shell; do not run repository code or install "
    "dependencies. Separate what the sources show from what is unknown or only a potential impact. "
    "Give concise, distinct claims, each with file-relative line citations; report uncertainty explicitly. "
    "Finish by calling submit_answer. Do not claim a change is deployed or a test has run unless shown."
)
SHELL = {"name": "run_shell", "description": "Run a shell command for static repository inspection inside "
         "an offline read-only container at /repo. Common tools include rg, grep, sed, nl, git and Python 3. "
         "Do not execute repository code, install dependencies, or access the network.",
         "input_schema": {"type": "object", "additionalProperties": False, "required": ["command"],
                          "properties": {"command": {"type": "string", "maxLength": 4000}}}}
CITATION = {"type": "object", "additionalProperties": False, "required": ["path", "start_line", "end_line"],
            "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                           "end_line": {"type": "integer", "minimum": 1}}}
SUBMIT = {"name": "submit_answer", "description": "Submit source-backed claims and any uncertainty.",
          "input_schema": {"type": "object", "additionalProperties": False,
                           "required": ["claims", "uncertainty"],
                           "properties": {
                               "claims": {"type": "array", "maxItems": 15, "items": {
                                   "type": "object", "additionalProperties": False,
                                   "required": ["claim", "citations"],
                                   "properties": {"claim": {"type": "string"},
                                                  "citations": {"type": "array", "maxItems": 5,
                                                                "items": CITATION}}}},
                               "uncertainty": {"type": "array", "items": {"type": "string"}, "maxItems": 10}}}}
MAX_OUTPUT = 40_000
MAX_TURNS = 18


class EvalError(ValueError):
    pass


def clean_env() -> dict:
    """Docker and source scanning never need credentials forwarded to their subprocesses."""
    return {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in
            ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "PASSWORD", "SECRET"))}


def check_dataset(dataset: dict, repo_id: str, repo: Path) -> list[str]:
    entries = [r for r in dataset.get("repositories", []) if r.get("id") == repo_id]
    if dataset.get("kind") != "ONTOLOGYEX_CHANGE_IMPACT_EVAL" or len(entries) != 1:
        raise EvalError("Invalid dataset or repository id")
    entry = entries[0]
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                                   env=clean_env()).strip()
    problems = [] if head == entry["commit"] else [f"checkout has {head}, expected {entry['commit']}"]
    status = subprocess.check_output(["git", "-C", str(repo), "status", "--short"], text=True,
                                     env=clean_env()).strip()
    if status:
        problems.append("repository checkout is not clean")
    for task in entry["tasks"]:
        for fact in task["facts"]:
            for ref in fact["evidence"]:
                path = (repo / ref["path"]).resolve()
                if not path.is_relative_to(repo.resolve()) or not path.is_file():
                    problems.append(f"{task['id']}: missing {ref['path']}")
                    continue
                lines = path.read_text("utf-8", errors="replace").splitlines()
                start, end = ref["start"], ref["end"]
                if not (1 <= start <= end <= len(lines)) or ref["anchor"] not in "\n".join(lines[start - 1:end]):
                    problems.append(f"{task['id']}: invalid anchor {ref['path']}:{start}-{end}")
    return problems


def validate_submission(submitted) -> list[str]:
    """Validate tool output; model tool arguments are not assumed to obey the schema."""
    if type(submitted) is not dict:
        return ["submission must be an object"]
    errors = []
    claims, uncertainty = submitted.get("claims"), submitted.get("uncertainty")
    if type(claims) is not list or len(claims) > 15:
        errors.append("claims must be an array of at most 15 objects")
        claims = []
    if type(uncertainty) is not list or len(uncertainty) > 10 or any(type(x) is not str for x in uncertainty):
        errors.append("uncertainty must be an array of at most 10 strings")
    for index, claim in enumerate(claims):
        if type(claim) is not dict or type(claim.get("claim")) is not str or type(claim.get("citations")) is not list:
            errors.append(f"claim {index + 1} needs a string claim and a citations array")
            continue
        if len(claim["citations"]) > 5:
            errors.append(f"claim {index + 1} has more than 5 citations")
        for cite in claim["citations"]:
            if (type(cite) is not dict or type(cite.get("path")) is not str
                    or type(cite.get("start_line")) is not int or type(cite.get("end_line")) is not int):
                errors.append(f"claim {index + 1} has a malformed citation")
    return errors


def citation_check(submitted: dict, repo: Path) -> dict:
    total = valid = 0
    bad = []
    errors = validate_submission(submitted)
    claims = submitted.get("claims", []) if type(submitted) is dict else []
    if type(claims) is not list:
        claims = []
    for claim in claims:
        if type(claim) is not dict or type(claim.get("citations")) is not list:
            continue
        for cite in claim["citations"]:
            total += 1
            if type(cite) is not dict:
                bad.append(cite)
                continue
            rel = str(cite.get("path", ""))
            path = (repo / rel).resolve()
            start, end = cite.get("start_line"), cite.get("end_line")
            ok = (bool(rel) and not Path(rel).is_absolute() and path.is_relative_to(repo.resolve())
                  and path.is_file() and type(start) is int and type(end) is int)
            if ok:
                lines = path.read_text("utf-8", errors="replace").splitlines()
                ok = 1 <= start <= end <= len(lines)
            valid += bool(ok)
            if not ok:
                bad.append(cite)
    return {"total": total, "valid": valid, "invalid": bad, "schema_errors": errors,
            "meaning": "Existence and line range only; no semantic verification."}


class Shell:
    def __init__(self, repo: Path, image: str):
        self.repo = repo.resolve()
        self.image = image
        self.name = "ontologyex-impact-" + uuid.uuid4().hex[:12]
        self.container = None

    def __enter__(self):
        cmd = ["docker", "run", "-d", "--rm", "--name", self.name, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
               "--memory", "256m", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
               "--mount", f"type=bind,src={self.repo},dst=/repo,readonly", "--workdir", "/repo",
               "--env", "HOME=/tmp", "--env", "PATH=/usr/bin:/bin", "--env", "PYTHONDONTWRITEBYTECODE=1",
               "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory",
               "--env", "GIT_CONFIG_VALUE_0=/repo", self.image, "/usr/bin/sleep", "1200"]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=clean_env())
        if run.returncode:
            raise EvalError("Could not start isolated shell: " + run.stderr[-500:])
        self.container = run.stdout.strip()
        return self

    def call(self, command: str) -> str:
        if not isinstance(command, str) or not command or len(command) > 4000:
            return "error: command must contain 1-4000 characters"
        # The subprocess receives a fixed wrapper and the command as an argument; it runs entirely
        # inside the offline container. The wrapper bounds output while preserving pipelines.
        wrapper = 'set -o pipefail; { eval "$1"; } 2>&1 | head -c 40001'
        try:
            run = subprocess.run(["docker", "exec", self.container, "/bin/bash", "-c", wrapper, "--", command],
                                 capture_output=True, text=True, timeout=30, env=clean_env())
        except subprocess.TimeoutExpired:
            return "error: command exceeded 30 seconds"
        out = run.stdout
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + "\n[output truncated]"
        if run.stderr:
            out += "\n" + run.stderr[:1000]
        return f"exit_code={run.returncode}\n{out}"

    def __exit__(self, *_):
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=30, env=clean_env())


class Ontology:
    def __init__(self, bundle: Path, audit: Path):
        policy = {**agent_tools.load_policy(None), "allow_tools": list(ONTOLOGY_TOOLS)}
        self.tools = agent_tools.Tools(agent_tools.Source(None, bundle), policy,
                                       agent_tools.Audit(audit, "eval", "change_impact_ab"), None)

    def schemas(self):
        return [{"name": n, "description": agent_tools.TOOLS[n]["description"],
                 "input_schema": agent_tools.TOOLS[n]["input"]} for n in ONTOLOGY_TOOLS]

    def call(self, name, args):
        try:
            return json.dumps(self.tools.call(name, args), sort_keys=True)
        except agent_tools.ToolError as exc:
            return "error: " + str(exc)


def build_bundle(repo: Path, out: Path):
    cmd = [sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(repo), "--bundle-only",
           "--discovery-engine", "python", "--view-engine", "python"]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=clean_env())
    if run.returncode:
        raise EvalError("Could not build OntologyEX bundle: " + (run.stderr or run.stdout)[-500:])
    json.loads(run.stdout)
    out.write_text(run.stdout, encoding="utf-8")


def answer(client, model: str, effort: str, task: dict, arm: str, shell: Shell, ox: Ontology, trace,
           direct_impact_file: str | None = None) -> dict:
    tools = [SHELL, *(ox.schemas() if arm == "treatment" else []), SUBMIT]
    question = task["question"]
    if direct_impact_file and arm == "treatment":
        question += ("\nBefore answering, call analyze_change with this changed file: " + direct_impact_file
                     + ". You may then inspect the source using the shell.")
    messages = [{"role": "user", "content": question}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    calls = []
    nudged = False
    started = time.monotonic()
    for turn in range(MAX_TURNS):
        response = client.messages.create(model=model, max_tokens=6000, system=SYSTEM, tools=tools,
                                          messages=messages, output_config={"effort": effort})
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        if response.stop_reason in ("refusal", "max_tokens"):
            return {"stop": response.stop_reason, "usage": usage, "calls": calls,
                    "turns": turn + 1, "seconds": round(time.monotonic() - started, 1)}
        messages.append({"role": "assistant", "content": response.content})
        uses = [block for block in response.content if block.type == "tool_use"]
        if not uses:
            if nudged:
                return {"stop": "no_answer", "usage": usage, "calls": calls,
                        "turns": turn + 1, "seconds": round(time.monotonic() - started, 1)}
            nudged = True
            messages.append({"role": "user", "content": "Call submit_answer now."})
            continue
        results = []
        for use in uses:
            args = dict(use.input)
            if use.name == "submit_answer":
                errors = validate_submission(args)
                if direct_impact_file and arm == "treatment" and "analyze_change" not in calls:
                    errors.append("call analyze_change on the supplied file before submitting")
                trace.write(json.dumps({"task": task["id"], "arm": arm, "tool": use.name,
                                        "input": args, "output": errors or "accepted"}, sort_keys=True) + "\n")
                trace.flush()
                if not errors:
                    return {"stop": "submitted", "submitted": args, "usage": usage, "calls": calls,
                            "turns": turn + 1, "seconds": round(time.monotonic() - started, 1)}
                results.append({"type": "tool_result", "tool_use_id": use.id,
                                "content": "Invalid submission: " + "; ".join(errors) + ". Submit again."})
                continue
            calls.append(use.name)
            if use.name == "run_shell":
                output = shell.call(args.get("command", ""))
            elif arm == "treatment" and use.name in ONTOLOGY_TOOLS:
                output = ox.call(use.name, args)
                if len(output) > MAX_OUTPUT:
                    output = output[:MAX_OUTPUT] + "\n[output truncated]"
            else:
                output = "error: tool not available"
            trace.write(json.dumps({"task": task["id"], "arm": arm, "tool": use.name,
                                    "input": args, "output": output}, sort_keys=True) + "\n")
            trace.flush()
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": output})
        messages.append({"role": "user", "content": results})
    return {"stop": "max_turns", "usage": usage, "calls": calls, "turns": MAX_TURNS,
            "seconds": round(time.monotonic() - started, 1)}


def run(dataset_path: Path, repo_id: str, repo: Path, image: str, out: Path,
        model: str, effort: str, key_env: str, only: list[str] | None = None,
        arms: list[str] | None = None, direct_impact_file: str | None = None):
    import anthropic
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    problems = check_dataset(dataset, repo_id, repo)
    if problems:
        raise EvalError("Preflight failed: " + "; ".join(problems))
    if out.exists() and any(out.iterdir()):
        raise EvalError("Output directory is not empty")
    out.mkdir(parents=True, exist_ok=True)
    (out / "dataset.json").write_bytes(dataset_bytes)
    entry = next(r for r in dataset["repositories"] if r["id"] == repo_id)
    tasks = [t for t in entry["tasks"] if not only or t["id"] in only]
    if only and len(tasks) != len(set(only)):
        raise EvalError("Unknown or duplicate requested question IDs")
    if direct_impact_file and (len(tasks) != 1 or arms != ["treatment"]):
        raise EvalError("A directed-tool diagnostic requires one question and --arms treatment")
    build_bundle(repo, out / "bundle.json")
    ox = Ontology(out / "bundle.json", out / "ontologyex-audit.jsonl")
    key = os.environ.get(key_env)
    if not key:
        raise EvalError(f"{key_env} is not set")
    client = anthropic.Anthropic(api_key=key)
    meta = {"repository": repo_id, "url": entry["url"], "commit": entry["commit"],
            "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(), "model": model, "effort": effort,
            "arms": "same offline read-only shell; treatment adds read-only OntologyEX tools",
            "arm_order": "alternating control-first and treatment-first by task index",
            "selected_arms": arms or ["control", "treatment"],
            "image": image, "question_ids": [t["id"] for t in tasks],
            "direct_impact_file": direct_impact_file,
            "at_start_utc": datetime.now(timezone.utc).isoformat()}
    (out / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    with (out / "answers.jsonl").open("w", encoding="utf-8") as answers, \
            (out / "trace.jsonl").open("w", encoding="utf-8") as trace:
        for index, task in enumerate(tasks):
            order = ("control", "treatment") if index % 2 == 0 else ("treatment", "control")
            for arm in order:
                if arms and arm not in arms:
                    continue
                try:
                    with Shell(repo, image) as shell:
                        result = answer(client, model, effort, task, arm, shell, ox, trace, direct_impact_file)
                except Exception as exc:  # preserve failed calls without treating them as incorrect answers
                    result = {"stop": "error", "error_type": type(exc).__name__, "calls": [], "usage": {}}
                result["citation_check"] = citation_check(result.get("submitted", {}), repo)
                answers.write(json.dumps({"id": task["id"], "arm": arm, **result}, sort_keys=True) + "\n")
                answers.flush()
                print(f"{repo_id}: {task['id']} {arm}: {result['stop']}", flush=True)
    meta["at_end_utc"] = datetime.now(timezone.utc).isoformat()
    (out / "run.json").write_text(json.dumps(meta, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/change-impact.json")
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--key-env", default="ONTOLOGYEX_EVAL_API_KEY")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--arms", nargs="+", choices=("control", "treatment"),
                        help="select arms only for a documented retry")
    parser.add_argument("--direct-impact-file", help="separate one-question treatment diagnostic: require analyze_change")
    args = parser.parse_args()
    try:
        run(args.dataset, args.repo_id, args.repo.resolve(), args.image, args.out, args.model,
            args.effort, args.key_env, args.only, args.arms, args.direct_impact_file)
    except EvalError as exc:
        print("impact_ab:", exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
