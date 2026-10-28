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
    "scripts/evidence_grade.py", "scripts/statement_lint.py", "scripts/decide.py",
    "scripts/propose_sources.py", "scripts/conflict_scan.py", "scripts/link_suggest.py",
    "scripts/business_view.py", "scripts/discover_repo.py",
    # Operating picture runtime: control room, its views, live feeds, and RDF export.
    "scripts/control_room.py", "scripts/operating_picture.html", "scripts/layout_engine.js",
    "scripts/github_discovery.py", "scripts/code_imports.py", "scripts/snapshot_store.py",
    "scripts/graph_view_api.py", "scripts/incident_view.py", "scripts/native_view_bridge.py",
    "scripts/feeds.py", "scripts/rdf_export.py", "scripts/graph_view.py", "scripts/fetch_engine.py",
    "scripts/feed_setup.py", "scripts/operate.py", "scripts/dependency_suggest.py",
    "references/operating-picture.md",
    "scripts/domain_skill.py", "scripts/policy_check.py", "scripts/scaffold.py", "scripts/provenance.py",
    "scripts/trace_extract.py", "scripts/otlp_normalize.py", "references/trace-to-ontology.md",
    "references/guided-onboarding.md", "references/domain-skill.md", "references/provenance.md",
    "references/evidence-grades.md", "references/rule-wording.md", "references/decision-models.md",
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
