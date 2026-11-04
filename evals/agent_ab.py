#!/usr/bin/env python3
"""Does OntologyEX make an agent right more often? The same questions, the same model, with and without it.

    python evals/agent_ab.py check  evals/datasets/demo-shop.json        # gold answers against the real files
    python evals/agent_ab.py run    evals/datasets/demo-shop.json --out evals/runs/demo [--model claude-opus-5-5]
    python evals/agent_ab.py report evals/runs/demo [--markdown]

Each question is asked twice, of the same model, in two arms:

- **control**: the agent can list, read and search the repository's files;
- **treatment**: the same, plus OntologyEX's read-only agent tools over the same repository
  (check_answerable, search_entities, get_entity_context, explain_relationship, what_needs_attention,
  operations_status, who_owns, list_suppliers, source_drift).

Both arms end by calling `submit_answer` with the answer, whether the sources answer the question, and
the file lines it rests on. Scoring is deterministic, with no model as judge:

- **correct**: an answerable question's answer states every gold fact (any of its accepted phrasings)
  and says it is answerable; an unanswerable one is declined (`answerable: false`);
- **confidently wrong**: answered as answerable when it is not, or when a fact is missing;
- **citations**: each cited `path:start-end` must exist in the repository (`valid`), and an answer is
  `grounded` when a citation overlaps a gold span;
- **strict**: correct and grounded (for questions with gold spans).

The report gives each arm's numbers, the difference, and the questions OntologyEX fixed and broke.

Running the agents needs the `anthropic` package (`pip install anthropic`) and an API key
(`ANTHROPIC_API_KEY`, or a profile from `ant auth login`). `--agent scripted:FILE` replays a script
instead of a model (CI uses it to test the harness). Refusals are scored as they come: no fallback
model is used, so each arm measures one model. The repositories are only read.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))

KIND = "ONTOLOGYEX_AGENT_EVAL"
ARMS = ("control", "treatment")
ONTOLOGYEX_TOOLS = ("check_answerable", "search_entities", "get_entity_context", "explain_relationship",
                    "what_needs_attention", "operations_status", "who_owns", "list_suppliers", "source_drift")
SKIP = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".next", "target"}
MAX_OUTPUT = 12_000
MAX_TURNS = 24
SYSTEM = (
    "You answer questions about a business from its repository. Use the tools to find the answer in the sources. "
    "Cite the file lines your answer rests on (path relative to the repository, with start and end lines). "
    "If the sources do not contain the answer, do not guess: call submit_answer with answerable set to false and "
    "say what is missing. Keep the answer short and specific. Finish by calling submit_answer exactly once."
)
SUBMIT = {
    "name": "submit_answer",
    "description": "Give the final answer. Call it once, at the end.",
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["answer", "answerable", "citations"],
        "properties": {
            "answer": {"type": "string", "description": "the answer, short and specific"},
            "answerable": {"type": "boolean", "description": "false when the sources do not answer the question"},
            "citations": {"type": "array", "maxItems": 10, "items": {
                "type": "object", "additionalProperties": False, "required": ["path", "start_line", "end_line"],
                "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                               "end_line": {"type": "integer", "minimum": 1}}}}}},
}
REPO_TOOLS = [
    {"name": "list_files", "description": "List the repository's files, optionally under a folder.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "folder": {"type": "string", "description": "a folder relative to the repository root ('' for all)"}}}},
    {"name": "read_file", "description": "Read lines of a file, with line numbers.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["path"], "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
         "end_line": {"type": "integer", "minimum": 1}}}},
    {"name": "search_text", "description": "Search the repository's files for a regular expression; "
                                           "returns path:line: text for each match (case-insensitive).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["pattern"], "properties": {
         "pattern": {"type": "string"}, "glob": {"type": "string", "description": "e.g. *.py"}}}},
]


class EvalError(ValueError):
    pass


# ---------------------------------------------------------------- datasets and repositories

def load_dataset(path: Path) -> dict:
    data = json.loads(path.read_text("utf-8"))
    if type(data) is not dict or data.get("kind") != KIND or data.get("schema_version") != 1:
        raise EvalError(f"{path}: not an {KIND} dataset")
    ids = set()
    for q in data["questions"]:
        if q["id"] in ids or type(q.get("answerable")) is not bool:
            raise EvalError(f"{path}: question {q.get('id')} is duplicated or lacks answerable")
        ids.add(q["id"])
    return data


def prepare(dataset: dict, work: Path) -> dict:
    """The repository the questions are about, and the OntologyEX bundle for it (built once per run)."""
    source = dataset["repo"]
    work.mkdir(parents=True, exist_ok=True)
    if source.get("build") == "demo":  # the synthetic demo business, built at a fixed moment
        import importlib.util
        spec = importlib.util.spec_from_file_location("ontologyex_eval_demo", ROOT / "examples/operating-picture/demo.py")
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        target = work / "demo"
        if target.exists():
            shutil.rmtree(target)
        built = demo.build(target, now=datetime.fromisoformat(source["at"].replace("Z", "+00:00")))
        repo, command = Path(built["repo"]), built["command"]
    elif "git" in source:
        repo = work / re.sub(r"[^A-Za-z0-9_.-]", "_", source["git"].rstrip("/").rsplit("/", 1)[-1])
        if not (repo / ".git").exists():
            subprocess.run(["git", "clone", "-q", "--filter=blob:none", source["git"], str(repo)], check=True, timeout=600)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", source["commit"]], check=True, timeout=120)
        command = [sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(repo)]
    else:
        raise EvalError("repo needs build: demo, or git and commit")
    pinned = ["--now", source["at"]] if source.get("at") else []  # time-bound evidence is judged as of the dataset's moment
    run = subprocess.run([*command, "--bundle-only", "--discovery-engine", "python", "--view-engine", "python", *pinned],
                         capture_output=True, text=True, timeout=600)
    if run.returncode != 0:
        raise EvalError(f"could not build the OntologyEX bundle: {(run.stdout or run.stderr)[-300:]}")
    bundle = work / "bundle.json"
    bundle.write_text(run.stdout, "utf-8")
    return {"repo": repo, "bundle": bundle}


class Repo:
    """Read-only file tools over one repository, the same in both arms."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.files = sorted(str(p.relative_to(self.root)).replace(os.sep, "/") for p in self.root.rglob("*")
                            if p.is_file() and not p.is_symlink() and not (set(p.relative_to(self.root).parts) & SKIP))

    def resolve(self, path: str) -> str | None:
        """A cited path as the repository names it: itself, or the one file ending in it."""
        path = path.strip().lstrip("./").replace("\\", "/")
        if path in self.files:
            return path
        ends = [f for f in self.files if f.endswith("/" + path)]
        return ends[0] if len(ends) == 1 else None

    def lines(self, rel: str) -> list[str]:
        return (self.root / rel).read_text("utf-8", "replace").splitlines()

    def call(self, name: str, args: dict) -> str:
        if name == "list_files":
            folder = (args.get("folder") or "").strip("/")
            found = [f for f in self.files if not folder or f.startswith(folder + "/")]
            return "\n".join(found[:400]) + (f"\n... {len(found) - 400} more" if len(found) > 400 else "")
        if name == "read_file":
            rel = self.resolve(str(args.get("path", "")))
            if rel is None:
                return "error: no such file"
            lines = self.lines(rel)
            start = max(1, int(args.get("start_line") or 1))
            end = min(len(lines), int(args.get("end_line") or start + 199))
            return "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1)) or "(empty)"
        if name == "search_text":
            try:
                pattern = re.compile(str(args["pattern"]), re.I)
            except re.error as exc:
                return f"error: {exc}"
            glob, out = args.get("glob"), []
            for rel in self.files:
                if glob and not fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob):
                    continue
                try:
                    for n, line in enumerate(self.lines(rel), 1):
                        if pattern.search(line):
                            out.append(f"{rel}:{n}: {line.strip()[:200]}")
                except (OSError, UnicodeError):
                    continue
                if len(out) >= 200:
                    break
            return "\n".join(out) or "no matches"
        return f"error: unknown tool {name}"


class OntologyEx:
    """OntologyEX's agent tools (agent_tools.py) over the repository's bundle: read-only, under the default policy."""

    def __init__(self, bundle: Path, audit: Path):
        import agent_tools
        self.at = agent_tools
        policy = {**agent_tools.load_policy(None), "allow_tools": list(ONTOLOGYEX_TOOLS)}
        self.tools = agent_tools.Tools(agent_tools.Source(None, bundle), policy,
                                       agent_tools.Audit(audit, "eval", "agent_ab"), None)

    def schemas(self) -> list[dict]:
        return [{"name": n, "description": self.at.TOOLS[n]["description"], "input_schema": self.at.TOOLS[n]["input"]}
                for n in ONTOLOGYEX_TOOLS]

    def call(self, name: str, args: dict) -> str:
        try:
            return json.dumps(self.tools.call(name, args), sort_keys=True)
        except self.at.ToolError as exc:
            return f"error: {exc}"


# ---------------------------------------------------------------- agents

class ClaudeAgent:
    """A manual tool-use loop over the Messages API (official SDK), so every call and token is counted."""

    def __init__(self, model: str, effort: str):
        try:
            import anthropic
        except ImportError:
            raise EvalError("running agents needs the anthropic package: pip install anthropic") from None
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort = model, effort

    def answer(self, question: str, tools: list[dict], execute) -> dict:
        messages = [{"role": "user", "content": question}]
        calls, usage, nudged = [], {"input_tokens": 0, "output_tokens": 0}, False
        for turn in range(MAX_TURNS):
            response = self.client.messages.create(model=self.model, max_tokens=16000, system=SYSTEM, tools=tools,
                                                   messages=messages, output_config={"effort": self.effort})
            usage["input_tokens"] += response.usage.input_tokens
            usage["output_tokens"] += response.usage.output_tokens
            if response.stop_reason == "refusal":
                return {"stop": "refusal", "calls": calls, "usage": usage, "turns": turn + 1}
            if response.stop_reason == "max_tokens":
                return {"stop": "max_tokens", "calls": calls, "usage": usage, "turns": turn + 1}
            messages.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if b.type == "tool_use"]
            if not uses:
                if nudged:
                    return {"stop": "no_answer", "calls": calls, "usage": usage, "turns": turn + 1}
                nudged = True
                messages.append({"role": "user", "content": "Call submit_answer now."})
                continue
            results = []
            for use in uses:
                if use.name == "submit_answer":
                    return {"stop": "submitted", "submitted": dict(use.input), "calls": calls, "usage": usage,
                            "turns": turn + 1}
                calls.append(use.name)
                results.append({"type": "tool_result", "tool_use_id": use.id,
                                "content": execute(use.name, dict(use.input))[:MAX_OUTPUT]})
            messages.append({"role": "user", "content": results})
        return {"stop": "max_turns", "calls": calls, "usage": usage, "turns": MAX_TURNS}


class ScriptedAgent:
    """Replays {arm: {question id: {"calls": [[tool, args], ...], "submit": {...}}}}: runs the tools, then submits."""

    def __init__(self, script: Path):
        self.script = json.loads(script.read_text("utf-8"))
        self.arm, self.qid = None, None

    def answer(self, question: str, tools: list[dict], execute) -> dict:
        step = self.script.get(self.arm, {}).get(self.qid)
        if step is None:
            return {"stop": "no_answer", "calls": [], "usage": {"input_tokens": 0, "output_tokens": 0}, "turns": 0}
        names = {t["name"] for t in tools}
        calls, outputs = [], []
        for name, args in step.get("calls", []):
            if name not in names:
                raise EvalError(f"scripted call to {name}, which the {self.arm} arm does not have")
            calls.append(name)
            outputs.append(execute(name, args))
        return {"stop": "submitted", "submitted": step["submit"], "calls": calls, "outputs": outputs,
                "usage": {"input_tokens": 0, "output_tokens": 0}, "turns": len(calls) + 1}


# ---------------------------------------------------------------- scoring

def normalize(text: str) -> str:
    text = re.sub(r"[`*_]", "", (text or "").lower())
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    return re.sub(r"\s+", " ", text)


def says(answer: str, phrase: str) -> bool:
    """The phrase as whole words: '30' is not in '300', 'api' is not in 'rapid'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(normalize(phrase)) + r"(?![a-z0-9])", answer) is not None


def score(question: dict, result: dict, repo: Repo) -> dict:
    """Deterministic: the facts stated, the abstention, and whether each cited line exists and is a gold one."""
    submitted = result.get("submitted") or {}
    answer = normalize(str(submitted.get("answer", "")))
    said_answerable = submitted.get("answerable") is True
    facts = question.get("facts", [])
    stated = [any(says(answer, alt) for alt in fact["any"]) for fact in facts]
    if question["answerable"]:
        correct = said_answerable and all(stated)
    else:
        correct = submitted.get("answerable") is False
    citations, valid = submitted.get("citations") or [], []
    for c in citations if type(citations) is list else []:
        rel = repo.resolve(str(c.get("path", ""))) if type(c) is dict else None
        ok = rel is not None and type(c.get("start_line")) is int and type(c.get("end_line")) is int and (
            1 <= c["start_line"] <= c["end_line"] <= len(repo.lines(rel)))
        valid.append({"path": rel or str(c.get("path") if type(c) is dict else c), "ok": ok,
                      "lines": [c.get("start_line"), c.get("end_line")] if type(c) is dict else None})
    gold = question.get("citations", [])
    grounded = any(v["ok"] and any(v["path"] == repo.resolve(g["path"]) and v["lines"][0] <= g["lines"][1]
                                   and v["lines"][1] >= g["lines"][0] for g in gold) for v in valid) if gold else None
    return {"id": question["id"], "category": question.get("category"), "answerable": question["answerable"],
            "stop": result.get("stop"), "correct": bool(correct and result.get("stop") == "submitted"),
            "confidently_wrong": bool(said_answerable and not correct),
            "facts_stated": stated, "citations": len(valid), "citations_valid": sum(v["ok"] for v in valid),
            "grounded": grounded, "strict": bool(correct and (grounded is not False)),
            "tool_calls": len(result.get("calls", [])), "ontologyex_calls": sum(c in ONTOLOGYEX_TOOLS
                                                                            for c in result.get("calls", [])),
            "usage": result.get("usage"), "answer": submitted.get("answer"), "said_answerable": submitted.get("answerable")}


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"questions": 0}
    cited = sum(r["citations"] for r in rows)
    with_gold = [r for r in rows if r["grounded"] is not None]
    unanswerable = [r for r in rows if not r["answerable"]]
    return {"questions": n, "correct": sum(r["correct"] for r in rows), "accuracy": round(sum(r["correct"] for r in rows) / n, 3),
            "strict": sum(r["strict"] for r in rows), "confidently_wrong": sum(r["confidently_wrong"] for r in rows),
            "declined_correctly": f"{sum(r['correct'] for r in unanswerable)}/{len(unanswerable)}",
            "citation_validity": round(sum(r["citations_valid"] for r in rows) / cited, 3) if cited else None,
            "grounded": f"{sum(bool(r['grounded']) for r in with_gold)}/{len(with_gold)}",
            "tool_calls_mean": round(sum(r["tool_calls"] for r in rows) / n, 1),
            "tokens": {k: sum((r["usage"] or {}).get(k, 0) for r in rows) for k in ("input_tokens", "output_tokens")},
            "refusals": sum(r["stop"] == "refusal" for r in rows)}


def report(run: Path) -> dict:
    meta = json.loads((run / "run.json").read_text("utf-8"))
    rows = [json.loads(line) for line in (run / "scores.jsonl").read_text("utf-8").splitlines() if line.strip()]
    by = {arm: {r["id"]: r for r in rows if r["arm"] == arm} for arm in ARMS}
    both = sorted(set(by["control"]) & set(by["treatment"]))
    fixed = [i for i in both if by["treatment"][i]["correct"] and not by["control"][i]["correct"]]
    broke = [i for i in both if by["control"][i]["correct"] and not by["treatment"][i]["correct"]]
    categories = sorted({r["category"] for r in rows if r.get("category")})
    return {"schema_version": 1, "kind": "ONTOLOGYEX_AGENT_EVAL_REPORT", **meta,
            "arms": {arm: summarize(list(by[arm].values())) for arm in ARMS},
            "by_category": {c: {arm: summarize([r for r in by[arm].values() if r.get("category") == c]) for arm in ARMS}
                            for c in categories},
            "fixed_by_ontologyex": fixed, "broken_by_ontologyex": broke,
            "semantics": "SAME_MODEL_SAME_QUESTIONS_DETERMINISTIC_SCORING_NO_MODEL_JUDGE"}


def markdown(found: dict) -> str:
    c, t = found["arms"]["control"], found["arms"]["treatment"]
    lines = [f"# Agent A/B: {found['dataset']}", "",
             f"Model `{found['agent']}`, {found['questions']} questions, {found['at']}. Same model and questions in "
             "both arms; scored without a model judge.", "",
             "| | Without OntologyEX | With OntologyEX |", "|---|---|---|"]
    for key, label in (("accuracy", "Correct"), ("strict", "Correct and grounded in the gold lines"),
                       ("confidently_wrong", "Confidently wrong"), ("declined_correctly", "Declined when the sources don't say"),
                       ("citation_validity", "Cited lines that exist"), ("grounded", "Grounded (cites a gold span)"),
                       ("tool_calls_mean", "Tool calls per question"), ("refusals", "Refusals")):
        a, b = c.get(key), t.get(key)
        fmt = lambda v: f"{v:.0%}" if isinstance(v, float) and key in ("accuracy", "citation_validity") else str(v)
        lines.append(f"| {label} | {fmt(a)} | {fmt(b)} |")
    lines.append(f"| Tokens (in / out) | {c['tokens']['input_tokens']:,} / {c['tokens']['output_tokens']:,} | "
                 f"{t['tokens']['input_tokens']:,} / {t['tokens']['output_tokens']:,} |")
    lines += ["", f"**Fixed by OntologyEX:** {', '.join(found['fixed_by_ontologyex']) or 'none'}  ",
              f"**Broken by OntologyEX:** {', '.join(found['broken_by_ontologyex']) or 'none'}", "", "| Category | Without | With |",
              "|---|---|---|"]
    for cat, arms in found["by_category"].items():
        lines.append(f"| {cat} | {arms['control']['correct']}/{arms['control']['questions']} | "
                     f"{arms['treatment']['correct']}/{arms['treatment']['questions']} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- commands

def check(dataset: dict, prepared: dict) -> list[str]:
    """Every gold span exists and holds one of its anchor phrases: the answers are checked against the files."""
    repo, problems = Repo(prepared["repo"]), []
    for q in dataset["questions"]:
        for g in q.get("citations", []):
            rel = repo.resolve(g["path"])
            if rel is None:
                problems.append(f"{q['id']}: {g['path']} is not in the repository")
                continue
            lines = repo.lines(rel)
            if not 1 <= g["lines"][0] <= g["lines"][1] <= len(lines):
                problems.append(f"{q['id']}: {rel}:{g['lines']} is out of range")
                continue
            text = normalize("\n".join(lines[g["lines"][0] - 1:g["lines"][1]]))
            if g.get("anchor") and normalize(g["anchor"]) not in text:
                problems.append(f"{q['id']}: {rel}:{g['lines'][0]}-{g['lines'][1]} does not contain {g['anchor']!r}")
    return problems


def run(dataset_path: Path, out: Path, agent, agent_name: str, arms=ARMS, only: list[str] | None = None) -> dict:
    dataset = load_dataset(dataset_path)
    if out.exists() and any(out.iterdir()):
        raise EvalError(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ontologyex-eval-") as tmp:
        prepared = prepare(dataset, Path(tmp))
        problems = check(dataset, prepared)
        if problems:
            raise EvalError("the dataset's gold citations do not match the repository: " + "; ".join(problems[:5]))
        repo = Repo(prepared["repo"])
        ox = OntologyEx(prepared["bundle"], out / "ontologyex-audit.jsonl")
        questions = [q for q in dataset["questions"] if not only or q["id"] in only]
        with (out / "answers.jsonl").open("w", encoding="utf-8") as answers, \
                (out / "scores.jsonl").open("w", encoding="utf-8") as scores:
            for arm in arms:
                tools = [*REPO_TOOLS, *(ox.schemas() if arm == "treatment" else []), SUBMIT]

                def execute(name, args, arm=arm):
                    if name in ONTOLOGYEX_TOOLS:
                        return ox.call(name, args) if arm == "treatment" else "error: not available"
                    return repo.call(name, args)
                for q in questions:
                    if isinstance(agent, ScriptedAgent):
                        agent.arm, agent.qid = arm, q["id"]
                    started = time.monotonic()
                    try:
                        result = agent.answer(q["question"], tools, execute)
                    except EvalError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - an API failure is recorded, the run goes on
                        result = {"stop": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}", "calls": [],
                                  "usage": {"input_tokens": 0, "output_tokens": 0}}
                    result["seconds"] = round(time.monotonic() - started, 1)
                    answers.write(json.dumps({"arm": arm, "id": q["id"], **{k: v for k, v in result.items()
                                                                            if k != "outputs"}}, sort_keys=True) + "\n")
                    scores.write(json.dumps({"arm": arm, **score(q, result, repo)}, sort_keys=True) + "\n")
    meta = {"dataset": dataset["name"], "agent": agent_name, "questions": len(questions),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "repo": dataset["repo"]}
    (out / "run.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", "utf-8")
    found = report(out)
    (out / "report.json").write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", "utf-8")
    (out / "report.md").write_text(markdown(found), "utf-8")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="check a dataset's gold citations against its repository")
    c.add_argument("dataset", type=Path)
    r = sub.add_parser("run", help="ask every question in both arms and score it")
    r.add_argument("dataset", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--model", default="claude-opus-5-5")
    r.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="medium")
    r.add_argument("--agent", default="claude", help="claude, or scripted:FILE")
    r.add_argument("--only", nargs="*", help="question ids")
    p = sub.add_parser("report", help="the report of a finished run")
    p.add_argument("run", type=Path)
    p.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            dataset = load_dataset(args.dataset)
            with tempfile.TemporaryDirectory(prefix="ontologyex-eval-") as tmp:
                problems = check(dataset, prepare(dataset, Path(tmp)))
            print(json.dumps({"dataset": dataset["name"], "questions": len(dataset["questions"]),
                              "problems": problems}, indent=2))
            return 1 if problems else 0
        if args.command == "report":
            found = report(args.run)
            print(markdown(found) if args.markdown else json.dumps(found, indent=2, sort_keys=True))
            return 0
        if args.agent.startswith("scripted:"):
            agent, name = ScriptedAgent(Path(args.agent.split(":", 1)[1])), args.agent
        else:
            agent, name = ClaudeAgent(args.model, args.effort), f"{args.model} (effort {args.effort})"
        found = run(args.dataset, args.out, agent, name, only=args.only)
        print(markdown(found))
        return 0
    except EvalError as exc:
        print(f"agent_ab: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
