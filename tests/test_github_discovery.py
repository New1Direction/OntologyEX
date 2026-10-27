"""GitHub pinned remote -> existing scanner -> real control-room state tests.

All remote calls are mocked. No token, network, or production infrastructure.
"""
import base64
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import github_discovery as gh
import control_room


def blob(data: bytes):
    sha = gh.git_blob_digest(data)
    return sha, {"sha": sha, "size": len(data), "encoding": "base64",
                 "content": base64.b64encode(data).decode()}


class GitHubFixture:
    def __init__(self, repo_id=1234, owner="testorg", name="product"):
        self.repo = owner + "/" + name
        self.repo_id = repo_id
        self.commit = "a" * 40
        self.tree_sha = "b" * 40
        self.files = {
            "compose.yml": b"services:\n  web:\n    depends_on: [api]\n  api:\n    depends_on: [db]\n  db: {}\n",
            ".github/workflows/test.yml": b"name: Tests\non: push\njobs: {}\n",
            "package.json": b'{"name":"product","scripts":{"postinstall":"do-not-execute"}}',
            "vendor/skipped/package.json": b'{"secret":"vendor-not-selected"}',
            ".env": b"SECRET=do-not-read",
        }
        self.maps = {}
        self.tree = []
        for path, raw in sorted(self.files.items()):
            sha, response = blob(raw)
            self.maps[f"/repos/{self.repo}/git/blobs/{sha}"] = response
            self.tree.append({"path": path, "mode": "100644", "type": "blob",
                              "size": len(raw), "sha": sha})
        base = f"/repos/{self.repo}"
        self.maps[base] = {"id": repo_id, "full_name": self.repo, "default_branch": "main"}
        self.maps[base + "/commits/main"] = {"sha": self.commit}
        self.maps[base + "/git/commits/" + self.commit] = {"tree": {"sha": self.tree_sha}}
        self.maps[base + "/git/trees/" + self.tree_sha + "?recursive=1"] = {
            "sha": self.tree_sha, "truncated": False, "tree": self.tree}
        self.requested = []

    def fetch(self, path):
        self.requested.append(path)
        if path not in self.maps:
            raise AssertionError("unexpected API read " + path)
        return copy.deepcopy(self.maps[path])


class GitHubDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GitHubFixture()

    def scan(self):
        return gh.discover_github(self.fixture.repo, self.fixture)

    def test_end_to_end_mock_to_control_room(self):
        topology = self.scan()
        self.assertEqual(topology["source"]["provider"], "github")
        self.assertEqual(topology["source"]["commit_sha"], "a" * 40)
        self.assertEqual(topology["coverage"]["github_file_blobs_pinned"], 3)
        self.assertEqual(topology["coverage"]["candidate_manifests"], 3)
        self.assertEqual(topology["coverage"]["files_with_evidence"], 3)
        self.assertEqual(topology["coverage"]["github_tree_entries_seen"], 5)
        self.assertEqual(topology["coverage"]["not_assessed"][-1], "production_runtime")
        ids = [n["id"] for n in topology["nodes"]]
        self.assertTrue(all("gh1234" in node_id for node_id in ids))
        self.assertEqual(topology["project"], "discovery:project:gh1234")
        self.assertFalse(topology["execution_authorized"])
        self.assertNotIn("do-not-execute", json.dumps(topology))
        self.assertNotIn("do-not-read", json.dumps(topology))
        self.assertNotIn("vendor-not-selected", json.dumps(topology))
        self.assertFalse(any("/.env" in p for p in self.fixture.requested))
        self.assertEqual(len([e for e in topology["edges"] if e["relation"] == "DEPENDS_ON"]), 2)
        state = control_room.build_state(topology, [], {})
        self.assertEqual(state["coverage"]["observed_nodes"], 0)
        self.assertTrue(all(x["status"] == "unknown" for x in state["nodes"]))
        self.assertEqual(state["runtime_discovery"], "NOT_IMPLEMENTED")

    def test_remote_inventory_refresh_is_atomic_and_failure_is_visible(self):
        first = gh.discover_github(self.fixture.repo, self.fixture)
        calls = [first, gh.GitHubDiscoveryError("simulated source outage"), first]

        def scan():
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        inventory = control_room.RemoteInventory(scan)
        with self.assertRaises(control_room.RoomError):
            inventory.snapshot()
        self.assertTrue(inventory.refresh())
        good, timestamp, failed = inventory.snapshot()
        self.assertIs(good, first)
        self.assertFalse(failed)
        self.assertIsNotNone(timestamp)
        self.assertFalse(inventory.refresh())
        stale, old_time, failed = inventory.snapshot()
        self.assertIs(stale, first)
        self.assertEqual(timestamp, old_time)
        self.assertTrue(failed)
        self.assertTrue(inventory.refresh())
        new, latest_time, failed = inventory.snapshot()
        self.assertIs(new, first)
        self.assertFalse(failed)
        self.assertGreaterEqual(latest_time, old_time)

    def test_repeated_commits_yield_deterministic_graph(self):
        a = self.scan()
        b = self.scan()
        self.assertEqual(a, b)

    def test_repo_identity_stable_under_rename(self):
        earlier = gh.discover_github(self.fixture.repo, self.fixture)
        renamed = GitHubFixture(repo_id=1234, owner="neworg", name="renamed")
        later = gh.discover_github(renamed.repo, renamed)
        self.assertEqual({x["id"] for x in earlier["nodes"]},
                         {x["id"] for x in later["nodes"]})
        self.assertEqual(earlier["project"], later["project"])

    def test_different_repos_cannot_collide(self):
        a = self.scan()
        other = GitHubFixture(repo_id=5678)
        b = gh.discover_github(other.repo, other)
        self.assertFalse({x["id"] for x in a["nodes"]} & {x["id"] for x in b["nodes"]})
        self.assertFalse({x["id"] for x in a["source_evidence"]} &
                         {x["id"] for x in b["source_evidence"]})

    def test_every_graph_edge_references_present_nodes(self):
        topology = self.scan()
        ids = {n["id"] for n in topology["nodes"]}
        evidence = {e["id"] for e in topology["source_evidence"]}
        for edge in topology["edges"]:
            self.assertIn(edge["source"], ids)
            self.assertIn(edge["target"], ids)
            self.assertIn(edge["evidence_id"], evidence)
        for e in topology["source_evidence"]:
            self.assertEqual(e["origin"], "GITHUB_COMMIT_PINNED_BLOB")
            self.assertEqual(len(e["sha256"]), 64)
            self.assertEqual(len(e["git_blob_sha"]), 40)

    def test_tree_truncation_refused(self):
        key = f"/repos/{self.fixture.repo}/git/trees/{self.fixture.tree_sha}?recursive=1"
        self.fixture.maps[key]["truncated"] = True
        with self.assertRaisesRegex(gh.GitHubDiscoveryError, "incomplete"):
            self.scan()

    def test_tampered_blob_refused(self):
        selected = next(row for row in self.fixture.tree if row["path"] == "compose.yml")
        k = f"/repos/{self.fixture.repo}/git/blobs/{selected['sha']}"
        original_size = self.fixture.maps[k]["size"]
        self.fixture.maps[k]["content"] = base64.b64encode(b"x" * original_size).decode()
        with self.assertRaisesRegex(gh.GitHubDiscoveryError, "source bytes"):
            self.scan()

    def test_duplicate_paths_refused(self):
        key = f"/repos/{self.fixture.repo}/git/trees/{self.fixture.tree_sha}?recursive=1"
        selected = next(row for row in self.fixture.tree if row["path"] == "compose.yml")
        self.fixture.maps[key]["tree"].append(copy.deepcopy(selected))
        with self.assertRaisesRegex(gh.GitHubDiscoveryError, "duplicate"):
            self.scan()

    def test_disallowed_paths_ignored(self):
        for path in ("../package.json", "/absolute/package.json",
                     "node_modules/a/package.json", "vendor/x/package.json",
                     ".env", "app\\.env", "a//package.json", "x/./package.json"):
            self.assertFalse(gh.eligible_tree_path(path), path)

    def test_slug_validation_before_any_request(self):
        for bad in ("https://github.com/testorg/product", "../escape", "A/B/C",
                    "owner/%2e%2e", "a/.", "a/..", "a/.git/secret"):
            with self.subTest(bad=bad), self.assertRaises(gh.GitHubDiscoveryError):
                gh.discover_github(bad, self.fixture)
        self.assertEqual(self.fixture.requested, [])
        # Dots may appear in GitHub owner names; they never select a network host.
        self.assertEqual(gh.parse_repository("example.com/private"),
                         ("example.com", "private"))

    def test_manifest_count_budget(self):
        tree_key = f"/repos/{self.fixture.repo}/git/trees/{self.fixture.tree_sha}?recursive=1"
        tree = self.fixture.maps[tree_key]["tree"]
        original = list(tree)
        for i in range(gh.MAX_MANIFESTS):
            tree.append({"path": f"p{i}/package.json", "mode": "100644", "type": "blob",
                         "size": 2, "sha": "c" * 40})
        with self.assertRaisesRegex(gh.GitHubDiscoveryError, "too many manifests"):
            self.scan()
        tree[:] = original

    def test_blob_validation_prevents_altered_content(self):
        a_sha, _ = blob(b"good")
        self.assertNotEqual(a_sha, gh.git_blob_digest(b"bad"))

    def test_read_only_http_client_fixed_origin_and_get(self):
        self.assertEqual(gh.API_ORIGIN, "https://api.github.com")
        self.assertEqual(gh.GitHubClient.fetch.__code__.co_consts.count("POST"), 0)


if __name__ == "__main__":
    unittest.main()
