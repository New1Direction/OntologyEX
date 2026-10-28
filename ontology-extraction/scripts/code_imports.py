"""Bounded static ES-module import evidence for commit-pinned GitHub routes.

No execution, semantic call inference or production-health assertion. This is
an intentionally narrow syntax recognizer, not a JavaScript/TypeScript parser.
Missing/broader forms are counted as unassessed, never invented.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from urllib.parse import quote

MAX_READ_ROUTES = 36
MAX_IMPORT_BYTES = 768 * 1024
MAX_LINKS = 512
MAX_MODULES = 256
MAX_UNRESOLVED = 256
EXTENSIONS = (".js", ".mjs", ".ts", ".jsx", ".tsx")
IMPORT = re.compile(
    r"""(?m)^[ \t]*import[ \t]+(?:[^\r\n;'"]{1,500}?[ \t]+from[ \t]+)?(?P<q>['"])(?P<path>\.[^'"\r\n]{1,180})(?P=q)"""
)


def selected_routes_spread(routes):
    """Deterministic across the sorted complete tree, not just first alphabetic routes."""
    ordered = sorted(routes, key=lambda item: item["path"])
    if len(ordered) <= MAX_READ_ROUTES:
        return ordered
    return [ordered[(i * len(ordered)) // MAX_READ_ROUTES] for i in range(MAX_READ_ROUTES)]


def resolve_import(source: str, specifier: str, known: dict):
    """Resolve path-only relative imports, never package or runtime resolution."""
    if not specifier.startswith(".") or len(specifier) > 180 or "\0" in specifier:
        return None
    if "?" in specifier or "#" in specifier or "\\" in specifier:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
        return None
    candidates = [resolved] if resolved.endswith(EXTENSIONS) else (
        [resolved + ext for ext in EXTENSIONS] +
        [resolved + "/index" + ext for ext in EXTENSIONS]
    )
    for path in candidates:
        if path in known:
            return path
    return None


def attach(mapped, selected_routes, entries, client, api, commit, repo_id,
           read_blob, link_node, require):
    known = {
        item["path"]: item for item in entries
        if item.get("type") == "blob" and item.get("mode") == "100644"
        and isinstance(item.get("path"), str)
    }
    chosen = selected_routes_spread(selected_routes)
    downloaded = 0
    inspected = 0
    links = 0
    unique_modules = set()
    unrecognized = 0
    skipped = 0
    gaps = 0
    evidence_ids = {x["id"] for x in mapped["source_evidence"]}
    route_nodes = {node["label"]: node["id"] for node in mapped["nodes"]
                   if node["kind"] == "api_route"}

    def gap(path, reason):
        nonlocal gaps
        gaps += 1
        mapped["gaps"].append({"source": path, "reason": reason})

    for item in chosen:
        path, size = item["path"], item.get("size")
        if type(size) is not int or size > 256 * 1024 or size < 0:
            gap(path, "IMPORT_SOURCE_SIZE_UNSUPPORTED")
            continue
        if downloaded + size > MAX_IMPORT_BYTES:
            gap(path, "IMPORT_TOTAL_BYTES_EXCEEDED")
            continue
        try:
            raw = read_blob(client, api, item["sha"], size)
            text = raw.decode("utf-8")
        except (UnicodeError, ValueError, TypeError) as exc:
            gap(path, "IMPORT_SOURCE_UNAVAILABLE")
            continue
        downloaded += len(raw)
        inspected += 1
        source_id = hashlib.sha256(
            (str(repo_id) + "\0" + commit + "\0" + path +
             "\0" + hashlib.sha256(raw).hexdigest() +
             "\0" + "static-imports").encode("utf-8")
        ).hexdigest()
        require(source_id not in evidence_ids, "duplicate import evidence identity")
        evidence_ids.add(source_id)
        mapped["source_evidence"].append({
            "id": source_id, "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source_kind": "STATIC_IMPORT_SYNTAX",
            "authenticity": "GIT_BLOB_VERIFIED_SYNTAX_UNREVIEWED",
            "origin": "GITHUB_COMMIT_PINNED_BLOB",
            "repository_id": repo_id, "commit_sha": commit,
            "git_blob_sha": item["sha"],
        })
        route_label = "/" + path[len("app/"):].rsplit("/", 1)[0]
        route_id = route_nodes.get(route_label)
        require(route_id is not None, "unmapped source API route")
        for m in IMPORT.finditer(text):
            specifier = m.group("path")
            target = resolve_import(path, specifier, known)
            if target is None:
                unrecognized += 1
                if len(mapped["unresolved"]) < MAX_UNRESOLVED:
                    mapped["unresolved"].append({
                        "source": route_id, "target_name": specifier,
                        "relation": "STATIC_IMPORT_UNRESOLVED",
                        "evidence_id": source_id,
                    })
                else:
                    skipped += 1
                continue
            module_id = "discovery:source_module:gh" + str(repo_id) + "/" + quote(target, safe="/-._~")
            if target not in unique_modules:
                if len(unique_modules) >= MAX_MODULES:
                    skipped += 1
                    continue
                entry = known[target]
                if not isinstance(entry.get("sha"), str) or not re.fullmatch(
                    r"[0-9a-f]{40}", entry["sha"]
                ):
                    skipped += 1
                    continue
                digest = hashlib.sha256(
                    (commit + "\0" + target + "\0" + entry["sha"]).encode("utf-8")
                ).hexdigest()
                link_node(target, "source_module", target,
                          "GITHUB_TREE_ENTRY_METADATA", digest, entry["sha"],
                          "TREE_PATH_ONLY_SOURCE_BYTES_NOT_READ",
                          "GITHUB_REPOSITORY_TREE_ENTRY")
                unique_modules.add(target)
            if links >= MAX_LINKS:
                skipped += 1
                continue
            line = text.count("\n", 0, m.start()) + 1
            mapped["edges"].append({
                "source": route_id, "target": module_id,
                "relation": "STATIC_IMPORTS", "evidence_id": source_id,
                "state": "SOURCE_SYNTAX_CANDIDATE", "source_line": line,
            })
            links += 1

    if skipped:
        gap("github_static_imports", "IMPORT_RELATIONSHIPS_BOUNDED")
    mapped["unresolved"].sort(key=lambda x: (
        x["source"], x["relation"], x["target_name"]))
    mapped["coverage"]["import_route_files_selected"] = len(chosen)
    mapped["coverage"]["import_route_files_inspected"] = inspected
    mapped["coverage"]["import_route_files_not_inspected"] = len(selected_routes) - inspected
    mapped["coverage"]["import_source_bytes_read"] = downloaded
    mapped["coverage"]["import_relationships_found"] = links
    mapped["coverage"]["import_modules_found"] = len(unique_modules)
    mapped["coverage"]["import_unresolved_specifiers"] = unrecognized
    mapped["coverage"]["import_skipped_relations"] = skipped
    return mapped
