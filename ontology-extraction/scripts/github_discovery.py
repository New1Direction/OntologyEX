#!/usr/bin/env python3
"""Read-only GitHub API -> source-pinned OntologyEX discovery preview.

For selected repositories only; bounded manifest bytes are materialized in a
temporary server-side directory for the existing offline scanner. No clones,
source execution, recursive downloads, credential persistence, or write APIs.
This is *not* GitHub App OAuth, live production health, or a full business map.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

import discover_repo

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_API_BYTES = 8 * 1024 * 1024
MAX_TREE_ENTRIES = discover_repo.MAX_WALK_FILES
MAX_MANIFESTS = discover_repo.MAX_MANIFESTS
MAX_TOTAL_BYTES = discover_repo.MAX_TOTAL_BYTES
MAX_FILE_BYTES = discover_repo.MAX_FILE_BYTES
API_ORIGIN = "https://api.github.com"


class GitHubDiscoveryError(ValueError):
    """Safe, redacted remote discovery failure."""


def require(ok, message):
    if not ok:
        raise GitHubDiscoveryError(message)


def parse_repository(value: str) -> tuple[str, str]:
    require(type(value) is str and REPOSITORY.fullmatch(value) is not None
            and all(part not in (".", "..") for part in value.split("/")),
            "repository must be an owner/name slug")
    return tuple(value.split("/", 1))


def oid(value) -> str:
    require(type(value) is str and GIT_OID.fullmatch(value) is not None,
            "invalid Git object identity")
    return value


def parse_json(payload: bytes):
    def unique(pairs):
        value = {}
        for key, field in pairs:
            require(key not in value, "duplicate GitHub API response key")
            value[key] = field
        return value
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              GitHubDiscoveryError("nonfinite GitHub API value")))
    except (UnicodeError, ValueError) as exc:
        raise GitHubDiscoveryError("malformed GitHub API response") from exc


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GitHubDiscoveryError("unexpected GitHub API redirect")


class GitHubClient:
    def __init__(self, token=None):
        require(token is None or (type(token) is str and 0 < len(token) <= 2048
                                  and "\n" not in token and "\r" not in token),
                "invalid credential configuration")
        self._token = token
        self._opener = build_opener(NoRedirects())

    def fetch(self, path: str) -> dict:
        require(type(path) is str and path.startswith("/repos/") and
                not any(x in path for x in ("\r", "\n", "\\", "#")),
                "invalid API path")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "OntologyEX-readonly-discovery/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(API_ORIGIN + path, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=12) as response:
                require(response.status == 200, "unexpected GitHub API status")
                require(response.geturl().startswith(API_ORIGIN + "/repos/"),
                        "unexpected GitHub API response origin")
                data = response.read(MAX_API_BYTES + 1)
                require(len(data) <= MAX_API_BYTES, "GitHub response too large")
        except HTTPError as exc:
            # Do not print response headers, body, Authorization, or query URLs.
            raise GitHubDiscoveryError(f"GitHub HTTP {exc.code}; access or quota issue") from exc
        except (OSError, URLError) as exc:
            raise GitHubDiscoveryError("GitHub read unavailable") from exc
        result = parse_json(data)
        require(type(result) is dict, "GitHub API response must be an object")
        return result


def eligible_tree_path(value: str) -> bool:
    if type(value) is not str or len(value) > 512:
        return False
    if value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    parts = value.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        return False
    if len(parts) > discover_repo.MAX_DEPTH + 1:
        return False
    if any(part in discover_repo.EXCLUDED_DIRS or
           (part.startswith(".") and part != ".github") for part in parts[:-1]):
        return False
    if ".github" in parts[:-1] and parts[:2] != [".github", "workflows"]:
        return False
    return discover_repo.eligible(Path(*parts))


def git_blob_digest(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def read_blob(client: GitHubClient, api_base: str, sha: str, size: int) -> bytes:
    sha = oid(sha)
    require(len(sha) == 40, "SHA-256 Git repositories require a separate verifier")
    require(type(size) is int and 0 <= size <= MAX_FILE_BYTES, "manifest byte limit")
    response = client.fetch(api_base + "/git/blobs/" + sha)
    require(response.get("encoding") == "base64" and
            type(response.get("content")) is str and
            type(response.get("size")) is int and response["size"] == size and
            response.get("sha") == sha, "GitHub blob identity/size mismatch")
    try:
        raw = base64.b64decode("".join(response["content"].split()), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise GitHubDiscoveryError("invalid GitHub blob encoding") from exc
    require(len(raw) == size and git_blob_digest(raw) == sha,
            "source bytes do not match commit-pinned Git blob")
    return raw


def scoped(topology: dict, repo_id: int, full_name: str,
           commit_sha: str, tree_sha: str, entries_seen: int, blobs: dict) -> dict:
    """Give all nodes and evidence stable repository-scoped identities."""
    require(type(repo_id) is int and 0 < repo_id < 2**63, "invalid numeric repository ID")
    prefix = f"gh{repo_id}"
    mapped = dict(topology)
    id_map = {}
    for node in topology["nodes"]:
        old = node["id"]
        if node["kind"] == "project":
            new = f"discovery:project:{prefix}"
        else:
            pieces = old.split(":", 2)
            require(len(pieces) == 3 and pieces[0] == "discovery",
                    "invalid scanner node identity")
            new = f"discovery:{pieces[1]}:{prefix}/{pieces[2]}"
        require(new not in id_map.values(), "duplicate remapped graph node")
        id_map[old] = new
    evidence_map = {}
    for item in topology["source_evidence"]:
        evidence_map[item["id"]] = hashlib.sha256(
            (prefix + "\0" + item["id"]).encode("utf-8")).hexdigest()
    mapped["nodes"] = []
    for item in topology["nodes"]:
        node = {**item, "id": id_map[item["id"]]}
        if node["kind"] == "project":
            node["label"] = full_name
        if "evidence_id" in node:
            node["evidence_id"] = evidence_map[node["evidence_id"]]
        mapped["nodes"].append(node)
    mapped["edges"] = [{
        **edge,
        "source": id_map[edge["source"]],
        "target": id_map[edge["target"]],
        "evidence_id": evidence_map[edge["evidence_id"]],
    } for edge in topology["edges"]]
    mapped["unresolved"] = [{
        **item,
        "source": id_map[item["source"]],
        "evidence_id": evidence_map[item["evidence_id"]],
    } for item in topology["unresolved"]]
    mapped["source_evidence"] = [{
        **item, "id": evidence_map[item["id"]],
        "origin": "GITHUB_COMMIT_PINNED_BLOB",
        "repository_id": repo_id,
        "commit_sha": commit_sha,
        "git_blob_sha": blobs[item["path"]],
        "authenticity": "GIT_BLOB_HASH_MATCHED_NOT_SEMANTICALLY_VERIFIED",
    } for item in topology["source_evidence"]]
    mapped["project"] = f"discovery:project:{prefix}"
    mapped["source"] = {
        "provider": "github", "repository_id": repo_id, "full_name": full_name,
        "commit_sha": commit_sha, "tree_sha": tree_sha,
        "integrity": "COMMIT_PINNED_BLOBS_VALIDATED",
        "credential_authorization": "EXTERNAL_GITHUB_TOKEN_NOT_STORED",
    }
    mapped["coverage"] = {
        **topology["coverage"],
        "github_tree_entries_seen": entries_seen,
        "github_file_blobs_pinned": len(blobs),
        "files_seen": entries_seen,
        "not_assessed": sorted(set(topology["coverage"]["not_assessed"]) |
                               {"github_actions_ci", "live_production_health"}),
    }
    return mapped


def discover_github(repository: str, client: GitHubClient) -> dict:
    owner, repo = parse_repository(repository)
    full_name = owner + "/" + repo
    api = "/repos/" + quote(owner, safe="") + "/" + quote(repo, safe="")
    info = client.fetch(api)
    repo_id = info.get("id")
    require(type(repo_id) is int and 0 < repo_id < 2**63, "missing numeric GitHub repository ID")
    require(info.get("full_name", "").lower() == full_name.lower(),
            "GitHub repository identity mismatch")
    branch = info.get("default_branch")
    require(type(branch) is str and 0 < len(branch) <= 200 and
            not any(c in branch for c in ("\r", "\n", "\\")),
            "invalid default branch")
    head = client.fetch(api + "/commits/" + quote(branch, safe=""))
    commit = oid(head.get("sha"))
    require(len(commit) == 40, "unsupported Git SHA-256 commit format")
    revision = client.fetch(api + "/git/commits/" + commit)
    tree_sha = oid(revision.get("tree", {}).get("sha"))
    tree = client.fetch(api + "/git/trees/" + tree_sha + "?recursive=1")
    require(tree.get("sha") == tree_sha and tree.get("truncated") is False,
            "GitHub tree incomplete; refusing to claim repository coverage")
    entries = tree.get("tree")
    require(type(entries) is list and len(entries) <= MAX_TREE_ENTRIES,
            "GitHub tree exceeds the discovery coverage budget")
    selected = []
    seen = set()
    for item in entries:
        require(type(item) is dict, "invalid GitHub tree entry")
        path = item.get("path")
        if item.get("type") != "blob" or not eligible_tree_path(path):
            continue
        require(path not in seen, "duplicate manifest path in GitHub tree")
        seen.add(path)
        size = item.get("size")
        if type(size) is not int or size < 0 or size > MAX_FILE_BYTES:
            # Distinguish omissions from a full inventory (no claim of completeness).
            continue
        selected.append((path, oid(item.get("sha")), size))
    require(len(selected) <= MAX_MANIFESTS, "too many manifests; narrow discovery scope")
    total = sum(size for _, _, size in selected)
    require(total <= MAX_TOTAL_BYTES, "manifest budget exceeded; narrow discovery scope")

    blobs = {}
    # Bounded ephemeral workspace on the server where this is invoked.
    with tempfile.TemporaryDirectory(prefix="ontologyex-gh-") as temporary:
        root = Path(temporary) / "selected-repository"
        root.mkdir()
        for path, sha, size in sorted(selected):
            data = read_blob(client, api, sha, size)
            target = root.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            blobs[path] = sha
        snapshot = discover_repo.discover(root)
    mapped = scoped(snapshot, repo_id, info["full_name"], commit, tree_sha,
                    len(entries), blobs)
    # Non-selected huge manifests and absent provider scopes remain unknown.
    mapped["coverage"]["excluded_large_or_unreadable_manifest_count"] = sum(
        item.get("type") == "blob" and eligible_tree_path(item.get("path")) and
        (type(item.get("size")) is not int or item["size"] > MAX_FILE_BYTES)
        for item in entries
    )
    return mapped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="exact GitHub owner/name")
    args = parser.parse_args()
    try:
        token = os.environ.get("ONTOLOGYEX_GITHUB_TOKEN")
        graph = discover_github(args.repo, GitHubClient(token=token))
        print(json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        return 0
    except (GitHubDiscoveryError, discover_repo.DiscoveryError, OSError,
            ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
