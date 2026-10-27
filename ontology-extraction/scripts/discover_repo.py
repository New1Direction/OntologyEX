#!/usr/bin/env python3
"""Offline, read-only manifest discovery for an OntologyEX graph preview.

No code execution, network, credentials, agent calls, or promotion. This
discovers declarations only. It does not map a whole business or prove runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote

import yaml

EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
    "target", ".next", ".turbo", ".cache", "__pycache__", "coverage",
})
MANIFESTS = frozenset({
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml",
    "Dockerfile",
})
COMPOSE_FILES = frozenset({
    "compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml",
})
MAX_WALK_FILES = 6000
MAX_MANIFESTS = 128
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 6
IDENT = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")


class DiscoveryError(ValueError):
    pass


class StrictLoader(yaml.SafeLoader):
    pass


# PyYAML's YAML 1.1 boolean resolver treats GitHub Actions "on" as True.
# Use the conservative YAML 1.2 true/false spelling here. Do not modify
# SafeLoader globally (the rest of OntologyEX depends on its behavior).
StrictLoader.yaml_implicit_resolvers = {
    key: [(tag, expression) for tag, expression in rules
          if tag != "tag:yaml.org,2002:bool"]
    for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if type(key) is not str or key in result:
            raise DiscoveryError("non-string or duplicate YAML mapping key")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def load_yaml(data: bytes):
    # Forbid anchors and aliases, including recursive alias expansion.
    for i, token in enumerate(yaml.scan(data)):
        if i >= 50000 or isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
            raise DiscoveryError("YAML exceeds token limit or contains anchors/aliases")
    return yaml.load(data, Loader=StrictLoader)


def canonical_id(kind: str, path: str, name: str = "") -> str:
    return ":".join(("discovery", kind,
                     quote(path, safe="/-._~"), quote(name, safe="-._~")))


def eligible(path: Path) -> bool:
    if path.name in MANIFESTS:
        return True
    return path.suffix.lower() in (".yml", ".yaml") and path.parts[:2] == (".github", "workflows")


def candidates(repo: Path) -> tuple[list[str], int, int]:
    matched = []
    visited = 0
    skipped_links = 0
    for directory, subdirs, files in os.walk(repo, topdown=True, followlinks=False):
        relative_dir = Path(directory).relative_to(repo)
        depth = 0 if relative_dir == Path(".") else len(relative_dir.parts)
        retained = []
        for name in sorted(subdirs):
            path = Path(directory) / name
            if path.is_symlink():
                skipped_links += 1
            elif name not in EXCLUDED_DIRS and not name.startswith(".") or name == ".github":
                if depth < MAX_DEPTH:
                    retained.append(name)
        subdirs[:] = retained
        for name in sorted(files):
            visited += 1
            if visited > MAX_WALK_FILES:
                raise DiscoveryError("repository walk exceeds file budget")
            path = Path(directory) / name
            rel = path.relative_to(repo)
            if path.is_symlink():
                skipped_links += 1
                continue
            if path.is_file() and eligible(rel):
                matched.append(rel.as_posix())
                if len(matched) > MAX_MANIFESTS:
                    raise DiscoveryError("too many candidate manifests; scope the repository")
    return sorted(matched), visited, skipped_links


def discover(repo: Path) -> dict:
    if repo.is_symlink() or not repo.is_dir():
        raise DiscoveryError("expected a real local repository directory, not a symlink")
    paths, visited, skipped_links = candidates(repo)
    project = canonical_id("project", repo.name)
    nodes = {
        project: {"id": project, "kind": "project", "label": repo.name,
                  "state": "DECLARED_ONLY"}
    }
    edges = {}
    evidence = {}
    gaps = []
    unresolved = []
    total_bytes = 0

    def add_node(uid: str, kind: str, label: str, eid: str):
        if uid in nodes:
            raise DiscoveryError("duplicate canonical node ID")
        nodes[uid] = {"id": uid, "kind": kind, "label": label,
                      "evidence_id": eid, "state": "DECLARED_ONLY"}

    def add_edge(source: str, target: str, relation: str, eid: str):
        if source not in nodes or target not in nodes:
            raise DiscoveryError("cannot invent unknown dependency node")
        ident = (source, target, relation)
        edges[ident] = {"source": source, "target": target, "relation": relation,
                        "evidence_id": eid, "state": "MANIFEST_DECLARATION"}

    for rel in paths:
        path = repo / rel
        if path.is_symlink() or not path.is_file():
            gaps.append({"source": rel, "reason": "NO_LONGER_REGULAR_FILE"})
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
            gaps.append({"source": rel, "reason": "SOURCE_SIZE_LIMIT"})
            continue
        data = path.read_bytes()
        if len(data) != size or len(data) > MAX_FILE_BYTES:
            gaps.append({"source": rel, "reason": "SOURCE_CHANGED_DURING_READ"})
            continue
        total_bytes += len(data)
        digest = hashlib.sha256(data).hexdigest()
        eid = hashlib.sha256((rel + ":" + digest).encode("utf-8")).hexdigest()
        evidence[eid] = {"id": eid, "path": rel, "sha256": digest,
                         "source_kind": "LOCAL_MANIFEST_BYTES", "authenticity": "UNVERIFIED"}
        try:
            if rel.endswith(".json"):
                doc = json.loads(data.decode("utf-8"))
            elif rel.endswith(".toml"):
                doc = tomllib.loads(data.decode("utf-8"))
            elif rel in ("go.mod",) or Path(rel).name in ("go.mod", "Dockerfile"):
                doc = None  # Presence only; never execute or interpret arbitrary instructions
            elif Path(rel).suffix in (".yaml", ".yml"):
                doc = load_yaml(data)
            else:
                doc = None
            base = Path(rel).name
            if base in COMPOSE_FILES:
                if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
                    raise DiscoveryError("compose document requires a service mapping")
                service_names = sorted(doc["services"])
                for name in service_names:
                    if type(name) is not str or not IDENT.fullmatch(name):
                        raise DiscoveryError("invalid compose service name")
                for name in service_names:
                    service = doc["services"][name]
                    if not isinstance(service, dict):
                        raise DiscoveryError("invalid compose service definition")
                    uid = canonical_id("service", rel, name)
                    add_node(uid, "declared_service", name, eid)
                    add_edge(project, uid, "DECLARES", eid)
                for name in service_names:
                    depends = doc["services"][name].get("depends_on", [])
                    if type(depends) is dict:
                        depends = list(depends)
                    if type(depends) is not list or len(depends) > 128:
                        raise DiscoveryError("invalid depends_on list")
                    for dep in depends:
                        if type(dep) is not str or not IDENT.fullmatch(dep):
                            raise DiscoveryError("invalid compose dependency ID")
                        if dep not in service_names:
                            unresolved.append({"source": canonical_id("service", rel, name),
                                               "target_name": dep, "relation": "DEPENDS_ON",
                                               "evidence_id": eid})
                        else:
                            add_edge(canonical_id("service", rel, name),
                                     canonical_id("service", rel, dep), "DEPENDS_ON", eid)
            elif rel.startswith(".github/workflows/"):
                uid = canonical_id("workflow", rel)
                title = doc.get("name", Path(rel).stem) if isinstance(doc, dict) else Path(rel).stem
                add_node(uid, "workflow", title if type(title) is str else Path(rel).stem, eid)
                add_edge(project, uid, "DECLARES", eid)
            else:
                uid = canonical_id("manifest", rel)
                add_node(uid, "source_manifest", rel, eid)
                add_edge(project, uid, "DECLARES", eid)
        except (UnicodeError, ValueError, TypeError, KeyError, yaml.YAMLError, RecursionError) as exc:
            gaps.append({"source": rel, "reason": "PARSE_OR_VALIDATION_FAILED", "error_type": type(exc).__name__})
            # Don't leave successfully parsed siblings from one malformed manifest:
            for key in list(nodes):
                if nodes[key].get("evidence_id") == eid:
                    del nodes[key]
            for key in list(edges):
                if edges[key]["evidence_id"] == eid:
                    del edges[key]
            unresolved = [u for u in unresolved if u["evidence_id"] != eid]

    return {
        "schema_version": 1,
        "kind": "UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW",
        "status": "PARTIAL_SOURCE_DECLARATION_MAP",
        "project": project,
        "nodes": [nodes[k] for k in sorted(nodes)],
        "edges": [edges[k] for k in sorted(edges)],
        "source_evidence": [evidence[k] for k in sorted(evidence)],
        "unresolved": sorted(unresolved, key=lambda x: (x["source"], x["target_name"])),
        "gaps": sorted(gaps, key=lambda x: x["source"]),
        "coverage": {
            "files_seen": visited, "candidate_manifests": len(paths),
            "files_with_evidence": len(evidence), "bytes_read": total_bytes,
            "symlinks_skipped": skipped_links,
            "not_assessed": ["business_processes", "people_permissions", "financial_flows",
                             "production_runtime", "cloud_resources", "non-manifest_source_code"],
        },
        "inferred_business_relationships": "NONE",
        "runtime_health": "UNKNOWN",
        "human_review": "REQUIRED",
        "execution_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="existing local repository")
    args = parser.parse_args()
    try:
        result = discover(args.repo)
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        return 0
    except (OSError, DiscoveryError, ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
