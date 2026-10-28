"""Durable single-operator GitHub graph cache; no GitHub network access."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import snapshot_store as ss
import control_room as room
import github_discovery as gh

spec = importlib.util.spec_from_file_location(
    "ontologyex_github_fixture", ROOT / "tests/test_github_discovery.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

CLOCK = datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "secure-cache"
        self.store = ss.SnapshotStore(self.path)
        self.fixture = mod.GitHubFixture()
        self.graph = gh.discover_github(self.fixture.repo, self.fixture)

    def test_roundtrip_and_explicit_history(self):
        digest = self.store.save(self.graph, CLOCK)
        restored, scanned_at = self.store.load(self.fixture.repo)
        self.assertEqual(restored, self.graph)
        self.assertEqual(scanned_at, CLOCK)
        versions = self.store.history(self.fixture.repo)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["digest"], digest)
        self.assertTrue(versions[0]["is_current"])
        self.assertEqual(len(digest), 64)
        self.assertNotIn("graph_json", versions[0])

    def test_idempotent_same_snapshot_and_revision_limit(self):
        self.store.save(self.graph, CLOCK)
        self.store.save(self.graph, CLOCK + timedelta(seconds=2))
        self.assertEqual(len(self.store.history(self.fixture.repo)), 1)
        for i in range(ss.MAX_VERSIONS + 3):
            g = copy.deepcopy(self.graph)
            g["ci_evidence"][0]["run_id"] = 10000 + i
            self.store.save(g, CLOCK + timedelta(minutes=i + 1))
        revisions = self.store.history(self.fixture.repo)
        self.assertEqual(len(revisions), ss.MAX_VERSIONS)
        self.assertEqual(sum(row["is_current"] for row in revisions), 1)
        restored, _ = self.store.load(self.fixture.repo)
        self.assertEqual(restored["ci_evidence"][0]["run_id"],
                         10000 + ss.MAX_VERSIONS + 2)

    def test_restored_cache_is_stale_until_success(self):
        self.store.save(self.graph, CLOCK)
        updated = copy.deepcopy(self.graph)

        def fails():
            raise gh.GitHubDiscoveryError("offline")

        room_cache = room.RemoteInventory(fails, store=self.store, repository=self.fixture.repo)
        snapshot, moment, stale = room_cache.snapshot()
        self.assertEqual(snapshot, self.graph)
        self.assertEqual(moment, CLOCK)
        self.assertTrue(stale)
        self.assertFalse(room_cache.refresh())
        snapshot, moment, stale = room_cache.snapshot()
        self.assertEqual(snapshot, self.graph)
        self.assertTrue(stale)
        room_cache._scan = lambda: updated
        self.assertTrue(room_cache.refresh())
        _, moment, stale = room_cache.snapshot()
        self.assertFalse(stale)
        self.assertGreater(moment, CLOCK)

    def test_write_failure_keeps_prior_snapshot_and_marks_stale(self):
        self.store.save(self.graph, CLOCK)
        inv = room.RemoteInventory(lambda: self.graph, store=self.store,
                                   repository=self.fixture.repo)
        old_graph, old_time, _ = inv.snapshot()

        def reject(graph, moment):
            raise ss.SnapshotError("disk not writable")

        inv._store.save = reject
        self.assertFalse(inv.refresh())
        result, timestamp, stale = inv.snapshot()
        self.assertIs(result, old_graph)
        self.assertEqual(timestamp, old_time)
        self.assertTrue(stale)

    def test_wrong_repo_never_loads_another_project(self):
        self.store.save(self.graph, CLOCK)
        self.assertIsNone(self.store.load("elsewhere/different"))
        self.assertEqual(self.store.history("elsewhere/different"), [])

    def test_refuse_unowned_or_public_permissions(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        os.chmod(self.store.path, 0o644)
        with self.assertRaises(ss.SnapshotError):
            ss.SnapshotStore(self.path)

    def test_symlink_directory_and_file_refused(self):
        alias = self.root / "linked-cache"
        alias.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(ss.SnapshotError):
            ss.SnapshotStore(alias)
        target = self.store.path
        target.unlink()
        target.symlink_to(self.root / "secrets")
        with self.assertRaises(ss.SnapshotError):
            ss.SnapshotStore(self.path)

    def test_corrupt_snapshot_body_refused(self):
        self.store.save(self.graph, CLOCK)
        with sqlite3.connect(self.store.path) as con:
            con.execute("UPDATE versions SET graph_json=?", (b'{"tampered":true}',))
        with self.assertRaisesRegex(ss.SnapshotError, "checksum"):
            self.store.load(self.fixture.repo)

    def test_reject_injected_token_and_dangling_reference(self):
        evil = copy.deepcopy(self.graph)
        evil["token"] = "should-not-persist"
        with self.assertRaisesRegex(ss.SnapshotError, "unrecognized"):
            self.store.save(evil)
        evil = copy.deepcopy(self.graph)
        evil["source"]["access_token"] = "should-not-persist"
        with self.assertRaisesRegex(ss.SnapshotError, "credential"):
            self.store.save(evil)
        evil = copy.deepcopy(self.graph)
        evil["edges"][0]["target"] = "discovery:service:nonexistent"
        with self.assertRaisesRegex(ss.SnapshotError, "dangling"):
            self.store.save(evil)
        self.assertEqual(self.store.history(self.fixture.repo), [])

    def test_reject_unpinned_unsupported_or_claimed_health(self):
        for mutation in ("integrity", "status", "project", "blob", "ci"):
            g = copy.deepcopy(self.graph)
            if mutation == "integrity":
                g["source"]["integrity"] = "NOT_CHECKED"
            elif mutation == "status":
                g["runtime_health"] = "HEALTHY"
            elif mutation == "project":
                g["source"]["repository_id"] += 1
            elif mutation == "blob":
                g["source_evidence"][0]["git_blob_sha"] = "bogus"
            else:
                g["ci_evidence"][0]["scope"] = "PRODUCTION_HEALTH"
            with self.subTest(mutation=mutation), self.assertRaises(ss.SnapshotError):
                self.store.save(g)

    def test_no_raw_manifest_or_auth_token_stored(self):
        self.store.save(self.graph, CLOCK)
        contents = self.store.path.read_bytes()
        self.assertNotIn(b"do-not-execute", contents)
        self.assertNotIn(b"do-not-read", contents)
        self.assertNotIn(b"Authorization", contents)
        self.assertNotIn(b"SECRET=", contents)
        self.assertIn(b"COMMIT_PINNED_BLOBS_VALIDATED", contents)

    def test_insecure_existing_directory_refused(self):
        other = self.root / "public-cache"
        other.mkdir(mode=0o755)
        with self.assertRaises(ss.SnapshotError):
            ss.SnapshotStore(other)

    def test_versioned_ci_on_same_commit_not_overwritten(self):
        first = copy.deepcopy(self.graph)
        second = copy.deepcopy(self.graph)
        second["ci_evidence"][0]["conclusion"] = "failure"
        a = self.store.save(first, CLOCK)
        b = self.store.save(second, CLOCK + timedelta(minutes=1))
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.store.history(self.fixture.repo)), 2)
        restored, _ = self.store.load(self.fixture.repo)
        self.assertEqual(restored["ci_evidence"][0]["conclusion"], "failure")
        self.assertEqual(restored["runtime_health"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
