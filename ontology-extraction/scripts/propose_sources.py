#!/usr/bin/env python3
"""Propose the smallest useful source set for an onboarding goal.

Ranks candidate files by word overlap with the goal (offline), and with
--model-assist asks the configured decision model (Jev or Clef) to score each
shortlisted file's relevance from its path and a short preview. Only that preview
is sent. The output is a proposal: a person reviews the paths and passes them to
`onboard.py start --include ... --acknowledge-sources`. Nothing here starts a session.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex

import decide
import discover_repo
import link_suggest

LEVELS = ["Unrelated to the goal",
          "Background only: general context, not needed to do the goal",
          "Useful: describes or implements part of what the goal changes",
          "Essential: states the rules or holds the code the goal changes"]
MAX_SEEN = 5000
MAX_FILE_BYTES = 256 * 1024
PREVIEW_LINES = 60
PREVIEW_CHARS = 3000
SENSITIVE_NAMES = re.compile(r"(^|/)(\.env[^/]*|.*\.(pem|key|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)[^/]*|"
                             r"credentials[^/]*|secrets?[^/]*)$", re.I)
SENSITIVE_TEXT = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|"
                            r"xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}")


def inventory(repo: Path) -> tuple[list[dict], dict]:
    """Readable text files, skipping dependencies, build output, hidden paths, and secrets."""
    files, skipped = [], {"sensitive": 0, "binary_or_large": 0, "hidden_or_excluded": 0}
    seen = 0
    for root, dirs, names in os.walk(repo, followlinks=False):
        rel_root = Path(root).relative_to(repo)
        kept = []
        for d in sorted(dirs):
            if d.startswith(".") or d in discover_repo.EXCLUDED_DIRS:
                skipped["hidden_or_excluded"] += 1
            else:
                kept.append(d)
        dirs[:] = kept
        for name in sorted(names):
            seen += 1
            if seen > MAX_SEEN:
                break
            rel = (rel_root / name).as_posix()
            path = Path(root) / name
            if SENSITIVE_NAMES.search(rel):
                skipped["sensitive"] += 1
                continue
            if name.startswith(".") or path.is_symlink():
                skipped["hidden_or_excluded"] += 1
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped["binary_or_large"] += 1
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8192]:
                skipped["binary_or_large"] += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped["binary_or_large"] += 1
                continue
            if SENSITIVE_TEXT.search(text):
                skipped["sensitive"] += 1
                continue
            preview = "\n".join(text.splitlines()[:PREVIEW_LINES])[:PREVIEW_CHARS]
            files.append({"path": rel, "bytes": len(raw), "preview": preview})
    return files, {**skipped, "files_seen": min(seen, MAX_SEEN), "truncated": seen > MAX_SEEN}


def overlap(goal_words: set[str], f: dict) -> float:
    path_words = set(link_suggest.tokens(f["path"].replace("/", " ")))
    text_words = set(link_suggest.tokens(f["preview"]))
    if not goal_words:
        return 0.0
    return round((2 * len(goal_words & path_words) + len(goal_words & text_words)) / (3 * len(goal_words)), 4)


def propose(repo: Path, goal: str, client=None, shortlist: int = 80, keep: int = 8) -> dict:
    repo = repo.resolve()
    files, coverage = inventory(repo)
    goal_words = set(link_suggest.tokens(goal))
    for f in files:
        f["overlap"] = overlap(goal_words, f)
    files.sort(key=lambda f: (-f["overlap"], f["path"]))
    ranked = files[:shortlist]
    mode = "WORD_OVERLAP_ONLY"
    if client is not None:
        asks = [(f"Goal: {goal}\n\nFile: {f['path']}\n\n{f['preview']}",
                 {"relevance": {"type": "score", "criteria": LEVELS,
                                "instructions": "How relevant is this file to the goal?"}})
                for f in ranked]
        for f, answer in zip(ranked, client.decide_many(asks)):
            f["model_score"] = round(answer["relevance"]["score"], 3)
        ranked.sort(key=lambda f: (-f["model_score"], -f["overlap"], f["path"]))
        proposed = [f for f in ranked if f["model_score"] >= 2.0][:keep]
        mode = f"MODEL_SCORED:{client.label}"
    else:
        proposed = [f for f in ranked if f["overlap"] > 0][:keep]
    command = ["python", "ontology-extraction/scripts/onboard.py", "start", "--repo", str(repo),
               *[x for f in proposed for x in ("--include", f["path"])],
               "--name", "DOMAIN-NAME", "--goal", goal, "--out", "SESSION-DIR", "--acknowledge-sources"]
    return {"schema_version": 1, "kind": "SOURCE_SET_PROPOSAL", "mode": mode, "goal": goal,
            "proposed": [{k: f[k] for k in ("path", "bytes", "overlap", "model_score") if k in f}
                         for f in proposed],
            "ranked": [{k: f[k] for k in ("path", "overlap", "model_score") if k in f} for f in ranked],
            "coverage": {**coverage, "candidates": len(files), "shortlisted": len(ranked),
                         "previews_sent": len(ranked) if client is not None else 0},
            "next": " ".join(shlex.quote(x) for x in command),
            "acknowledgment": "REQUIRED_REVIEW_THE_PATHS_BEFORE_STARTING",
            "completeness": "NOT_GUARANTEED_RECORD_GAPS_AS_UNKNOWNS"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model-assist", action="store_true",
                        help="score the shortlist with the configured decision model (sends previews)")
    parser.add_argument("--shortlist", type=int, default=80)
    parser.add_argument("--keep", type=int, default=8)
    args = parser.parse_args()
    try:
        if not (args.repo.is_dir() and 0 < len(args.goal) <= 500 and 1 <= args.shortlist <= 500
                and 1 <= args.keep <= 40):
            raise ValueError("expected a repository directory, a goal, and sane limits")
        client = decide.DecisionClient() if args.model_assist else None
        print(json.dumps(propose(args.repo, args.goal, client, args.shortlist, args.keep), indent=2))
        return 0
    except (decide.DecisionError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
