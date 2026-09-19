#!/usr/bin/env python3
"""Copy the complete OntologyEX skill into a Claude Code project. No global writes."""
from __future__ import annotations

import argparse
from pathlib import Path

import domain_skill as ds
from policy_check import DomainError, canonical, digest, require, safe_file

PACKAGE = (
    "SKILL.md", "METHOD.md", "LICENSE",
    "scripts/install.py", "scripts/onboard.py", "scripts/evidence.py",
    "scripts/domain_skill.py", "scripts/policy_check.py", "scripts/scaffold.py",
    "references/guided-onboarding.md", "references/domain-skill.md",
    "references/design-principles.md", "references/source-mining.md",
    "references/reuse-catalog.md", "references/output-formats.md",
)


def install(project: Path, source: Path | None = None) -> dict:
    source = source or Path(__file__).resolve().parent.parent
    project = project.absolute()
    require(project.is_dir() and not any(p.is_symlink() for p in (project, *project.parents)), "project must be a local directory without symlinks")
    out = project / ".claude" / "skills" / "ontology-extraction"
    # Preflight before touching .claude. Only the explicit package is copied, never user data.
    files = {path: safe_file(source, path) for path in PACKAGE}
    skill = files["SKILL.md"].decode("utf-8")
    require(skill.startswith("---\n") and "\nname: ontology-extraction\n" in skill, "invalid entry-point metadata")
    ds.write_files(out, files)
    return {"status": "INSTALLED_PROJECT_FILES", "host_layout": "claude-code", "path": str(out),
            "files": {p: digest(b) for p, b in sorted(files.items())},
            "host_activation": "NOT_RUN", "agent_benchmark": "NOT_RUN",
            "next": "In Claude Code, ask /ontology-extraction to onboard one scoped workflow."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(canonical(install(args.project)).decode(), end="")
        return 0
    except (DomainError, OSError, UnicodeError, ValueError, TypeError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
