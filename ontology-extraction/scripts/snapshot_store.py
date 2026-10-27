#!/usr/bin/env python3
"""Opt-in private SQLite snapshots for the read-only GitHub discovery prototype.

This is a SINGLE-OPERATOR server-side cache, not hosted tenant isolation,
authenticated provenance, an encrypted database or backup infrastructure.
A stored graph is always explicitly stale until a new source scan succeeds.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

MAX_GRAPH_BYTES = 4 * 1024 * 1024
MAX_VERSIONS = 8
ID = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class SnapshotError(ValueError):
    pass


def require(predicate: bool, reason: str):
    if not predicate:
        raise SnapshotError(reason)


def encode(graph: dict) -> tuple[bytes, int, str, str]:
    """Validate the minimum trustworthy boundary of a discover_github output."""
    require(type(graph) is dict and
            graph.get("kind") == "UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW" and
            graph.get("runtime_health") == "UNKNOWN" and
            graph.get("execution_authorized") is False, "invalid source preview")
    src = graph.get("source")
    require(type(src) is dict and src.get("provider") == "github" and
            src.get("integrity") == "COMMIT_PINNED_BLOBS_VALIDATED",
            "expected commit-pinned GitHub data")
    repo_id, repo_name, commit = src.get("repository_id"), src.get("full_name"), src.get("commit_sha")
    require(type(repo_id) is int and 0 < repo_id < 2**63,
            "invalid repository ID")
    require(type(repo_name) is str and 0 < len(repo_name) <= 220 and
            repo_name.count("/") == 1 and
            all(c.isascii() and (c.isalnum() or c in "._-/") for c in repo_name),
            "invalid repository label")
    require(type(commit) is str and ID.fullmatch(commit), "invalid commit SHA")
    nodes, edges, evidence = graph.get("nodes"), graph.get("edges"), graph.get("source_evidence")
    require(type(nodes) is list and type(edges) is list and type(evidence) is list and
            len(nodes) <= 4096 and len(edges) <= 16384 and len(evidence) <= 128,
            "invalid or excessive graph collection")
    node_ids, evidence_ids = set(), set()
    for n in nodes:
        require(type(n) is dict and type(n.get("id")) is str and
                n["id"].startswith("discovery:") and len(n["id"]) <= 1024 and
                n["id"] not in node_ids, "invalid or duplicate graph node")
        node_ids.add(n["id"])
    for e in evidence:
        require(type(e) is dict and type(e.get("id")) is str and
                SHA256.fullmatch(e["id"]) and e["id"] not in evidence_ids and
                type(e.get("sha256")) is str and SHA256.fullmatch(e["sha256"]) and
                type(e.get("git_blob_sha")) is str and ID.fullmatch(e["git_blob_sha"]),
                "invalid evidence integrity metadata")
        evidence_ids.add(e["id"])
    for e in edges:
        require(type(e) is dict and e.get("source") in node_ids and
                e.get("target") in node_ids and e.get("evidence_id") in evidence_ids,
                "graph contains dangling dependency")
    for n in nodes:
        if "evidence_id" in n:
            require(n["evidence_id"] in evidence_ids,
                    "graph node has missing evidence")
    require(graph.get("project") == f"discovery:project:gh{repo_id}",
            "project does not match GitHub numeric identity")
    try:
        raw = json.dumps(graph, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise SnapshotError("cannot serialize graph") from exc
    require(len(raw) <= MAX_GRAPH_BYTES, "graph exceeds storage byte budget")
    return raw, repo_id, repo_name, commit


class SnapshotStore:
    """Private server-side snapshot cache, keyed by GitHub numeric repository ID."""

    def __init__(self, directory: Path):
        self.root = Path(directory)
        if self.root.is_symlink():
            raise SnapshotError("snapshot directory symlinks are forbidden")
        if not self.root.exists():
            self.root.mkdir(mode=0o700, parents=False)
        meta = os.lstat(self.root)
        require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.getuid() and
                (stat.S_IMODE(meta.st_mode) & 0o077) == 0,
                "snapshot directory must be private (0700) and owned by this user")
        self.path = self.root / "inventory.sqlite3"
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                 getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        st = os.lstat(self.path)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and
                st.st_uid == os.getuid() and
                (stat.S_IMODE(st.st_mode) & 0o077) == 0,
                "snapshot database must be a private, regular, singly-linked file")
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS versions (
              repo_id INTEGER NOT NULL, digest TEXT NOT NULL,
              repo_name TEXT NOT NULL, commit_sha TEXT NOT NULL,
              scanned_at TEXT NOT NULL, graph_json BLOB NOT NULL,
              PRIMARY KEY (repo_id,digest))""")
            db.execute("""CREATE TABLE IF NOT EXISTS heads (
              repo_id INTEGER PRIMARY KEY, digest TEXT NOT NULL,
              FOREIGN KEY (repo_id,digest) REFERENCES versions(repo_id,digest))""")
            db.execute("CREATE INDEX IF NOT EXISTS versions_repo ON versions(repo_id,scanned_at)")
            db.commit()

    def _connect(self):
        # The containing directory is private; opening the fixed file via SQLite
        # must still fail if a previously verified path is replaced.
        st = os.lstat(self.path)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and
                st.st_uid == os.getuid() and
                (stat.S_IMODE(st.st_mode) & 0o077) == 0,
                "private database file has changed")
        db = sqlite3.connect(str(self.path), timeout=4)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA trusted_schema = OFF")
        db.execute("PRAGMA journal_mode = DELETE")
        db.execute("PRAGMA synchronous = FULL")
        return closing(db)

    def save(self, graph: dict, scanned_at: datetime | None = None) -> str:
        raw, repo_id, name, commit = encode(graph)
        digest = hashlib.sha256(raw).hexdigest()
        moment = scanned_at or datetime.now(timezone.utc)
        require(type(moment) is datetime and moment.tzinfo is not None and
                moment.utcoffset() is not None, "scan time must be timezone-aware")
        when = moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as db:
            # One transaction covers the immutable version, head and pruning.
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO versions
                    (repo_id,digest,repo_name,commit_sha,scanned_at,graph_json)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(repo_id,digest) DO UPDATE SET
                      scanned_at=excluded.scanned_at,repo_name=excluded.repo_name""",
                           (repo_id, digest, name, commit, when, raw))
                db.execute("""INSERT INTO heads(repo_id,digest) VALUES (?,?)
                    ON CONFLICT(repo_id) DO UPDATE SET digest=excluded.digest""",
                           (repo_id, digest))
                old = db.execute("""SELECT digest FROM versions WHERE repo_id=?
                    AND digest NOT IN (SELECT digest FROM heads WHERE repo_id=?)
                    ORDER BY scanned_at DESC,digest DESC LIMIT -1 OFFSET ?""",
                                 (repo_id, repo_id, MAX_VERSIONS-1)).fetchall()
                for (old_digest,) in old:
                    db.execute("DELETE FROM versions WHERE repo_id=? AND digest=?",
                               (repo_id, old_digest))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return digest

    def load(self, requested_repo_name: str) -> tuple[dict, datetime] | None:
        require(type(requested_repo_name) is str and
                requested_repo_name.count("/") == 1 and
                len(requested_repo_name) <= 220, "invalid requested GitHub source")
        with self._connect() as db:
            row = db.execute("""SELECT v.repo_id,v.repo_name,v.commit_sha,
                v.scanned_at,v.digest,v.graph_json
                FROM heads h JOIN versions v
                ON h.repo_id=v.repo_id AND h.digest=v.digest
                WHERE lower(v.repo_name)=lower(?)""", (requested_repo_name,)).fetchone()
        if row is None:
            return None
        repo_id, repo_name, commit, when, digest, raw = row
        require(type(raw) is bytes and len(raw) <= MAX_GRAPH_BYTES and
                hashlib.sha256(raw).hexdigest() == digest,
                "snapshot checksum mismatch or oversized record")
        try:
            graph = json.loads(raw.decode("utf-8"))
            verified, stored_id, stored_name, stored_commit = encode(graph)
            moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise SnapshotError("stored snapshot is malformed") from exc
        require(verified == raw and stored_id == repo_id and
                stored_name == repo_name and stored_commit == commit and
                moment.tzinfo is not None,
                "stored snapshot metadata conflicts with graph")
        return graph, moment.astimezone(timezone.utc)

    def history(self, requested_repo_name: str) -> list[dict]:
        """Return only compact local revision metadata, never raw graph content."""
        require(type(requested_repo_name) is str and requested_repo_name.count("/") == 1
                and len(requested_repo_name) <= 220,
                "invalid requested GitHub source")
        with self._connect() as db:
            rows = db.execute("""SELECT v.commit_sha,v.scanned_at,v.digest,
                v.repo_id, (h.digest = v.digest)
                FROM versions v JOIN heads h ON h.repo_id=v.repo_id
                WHERE lower(v.repo_name)=lower(?)
                ORDER BY v.scanned_at DESC,v.digest DESC LIMIT ?""",
                              (requested_repo_name, MAX_VERSIONS)).fetchall()
        return [{"commit_sha": sha, "scanned_at": at, "digest": digest,
                 "repository_id": repo_id, "is_current": bool(current)}
                for sha, at, digest, repo_id, current in rows]
