#!/usr/bin/env python3
"""Evidence watermarks: the file hashes a link decision rested on, so a change is noticed.

When a person confirms that a binding runs on a component, the decision records the
SHA-256 of what they were looking at:

- the file that declares the component (from discovery, the repository's current bytes);
- the binding's cited lines in the repository (`span:PATH`, the hash of the quoted lines with
  whitespace evened out), found wherever they are in that file now (drift.py), so editing
  other parts of the file, moving the lines or re-indenting them is not a change. The file
  is found by its path or by a unique path suffix (a skill cites paths relative to its own
  sources). When the quote is not in the file, the whole file's hash is recorded instead;
  decisions recorded before span hashes keep being compared by whole file;
- the hash the domain skill was compiled from (`skill:NAME/PATH`), which changes when
  the skill is rebuilt from changed code.

Later, the same hashes are computed again. If any differs, appears or disappears, the
confirmation no longer reads as current: entity_context shows the link as INFERRED,
"confirmed before the evidence changed", with the paths that changed, until a person
re-confirms it. Links decided before watermarks existed carry none and stay CONFIRMED
with the check NOT_RECORDED. This reads files only.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import time

import evidence_grade
from policy_check import DomainError, safe_file

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist", "build", "__pycache__", ".ontologyex",
             ".next", ".cache", "vendor"}
MAX_FILES = 100_000
MAX_BYTES = 8 * 1024 * 1024
MAX_PATHS = 16


class RepoFiles:
    """Where a cited path lives in the repository, re-indexed at most every `ttl` seconds."""

    def __init__(self, repo: Path | None, ttl: float = 30.0):
        self.repo, self.ttl = repo, ttl
        self._index: dict[str, list[str]] | None = None
        self._built = 0.0

    def _files(self) -> dict[str, list[str]]:
        if self._index is None or time.monotonic() - self._built > self.ttl:
            index: dict[str, list[str]] = {}
            count = 0
            if self.repo is not None and self.repo.is_dir():
                for root, dirs, files in os.walk(self.repo):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(root, d)))
                    for name in sorted(files):
                        rel = os.path.relpath(os.path.join(root, name), self.repo).replace(os.sep, "/")
                        index.setdefault(name, []).append(rel)
                        count += 1
                    if count >= MAX_FILES:
                        break
            self._index, self._built = index, time.monotonic()
        return self._index

    def find(self, path: str) -> str | None:
        """The repository path a cited path names: itself, or the one file ending in it."""
        if not path or self.repo is None:
            return None
        candidates = [p for p in self._files().get(PurePosixPath(path).name, [])
                      if p == path or p.endswith("/" + path)]
        exact = [p for p in candidates if p == path]
        chosen = exact or candidates
        return chosen[0] if len(chosen) == 1 else None

    def sha256(self, rel: str) -> str | None:
        try:
            return hashlib.sha256(safe_file(self.repo, rel, MAX_BYTES)).hexdigest()
        except (DomainError, OSError, ValueError):
            return None


def binding_sources(skills: list[Path]) -> dict[tuple[str, str], tuple[str, str | None, dict | None]]:
    """(skill, binding) -> (cited source path, the sha256 the skill was compiled from, the cited span)."""
    found = {}
    for path in skills:
        _, model = evidence_grade.load_skill(path)
        records = {e["id"]: e for e in model["evidence"]}
        compiled = model.get("source_files") or {}
        for concept in model["layers"]["40-application.yaml"].get("concepts", []):
            source = records.get(concept.get("source"))
            cited = source["path"] if source else ""
            sha = (compiled.get(cited) or {}).get("sha256") if cited else None
            span = ({"quote": source["quote"], "start_line": source["start_line"], "end_line": source["end_line"]}
                    if source and type(source.get("quote")) is str else None)
            found[(model["name"], concept["id"])] = (cited, sha if type(sha) is str else None, span)
    return found


class Marks:
    """The evidence a link rests on now, as {path: sha256}."""

    def __init__(self, files: RepoFiles, skills: list[Path], state: dict):
        self.files = files
        self.bindings = binding_sources(skills)
        evidence = {e["id"]: e for e in state.get("source_evidence", [])}
        self.components = {}
        for node in state.get("nodes", []):
            record = evidence.get(node.get("evidence_id"))
            if record and type(record.get("path")) is str and type(record.get("sha256")) is str:
                self.components[node["id"]] = (record["path"], record["sha256"])

    def now(self, skill: str, binding: str, component: str, recorded: dict | None = None) -> dict[str, str]:
        """The evidence as it is now, keyed as `recorded` keyed it (whole file or span), or as a new decision records it."""
        import drift  # noqa: PLC0415 - drift imports this module
        out: dict[str, str] = {}
        if component in self.components:
            path, sha = self.components[component]
            out[path] = sha
        cited, compiled, span = self.bindings.get((skill, binding), ("", None, None))
        if compiled:
            out[f"skill:{skill}/{cited}"] = compiled
        rel = self.files.find(cited)
        if rel is not None:
            whole = isinstance(recorded, dict) and rel in recorded  # decided before span hashes
            found = (drift.locate(drift.read_lines(self.files, rel)[1], span["start_line"], span["end_line"],
                                  quote=span["quote"]) if span and not whole else {"status": "NOT_RECORDED"})
            if found["status"] in ("UNCHANGED", "MOVED", "REFORMATTED"):
                out[f"span:{rel}"] = drift.fingerprint(span["quote"])["norm_sha256"]
            else:
                sha = self.files.sha256(rel)
                if sha is not None:
                    out[rel] = sha
        return dict(sorted(out.items())[:MAX_PATHS])
