"""Metadata-only API routes plus reviewed-required Drizzle schema candidates."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import github_discovery as gh
import snapshot_store as ss
import control_room as room

spec = importlib.util.spec_from_file_location(
    "ontologyex_route_fixture", ROOT / "tests/test_github_discovery.py")
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)


class CodeSurfaceTests(unittest.TestCase):
    def fixture(self):
        mock = fixtures.GitHubFixture()
        base = f"/repos/{mock.repo}"
        files = {
            "app/api/health/route.js": (
                b"import { database } from '../../lib/server.mjs'\n"
                b"export async function GET() { return Response.json({ ok: true }) }\n"
            ),
            "app/api/orders/[id]/route.ts": (
                b"import { createClaim } from '../../../../src/claim.mjs'\n"
                b"export async function POST() { return new Response('ok') }\n"
            ),
        }
        for path, raw in files.items():
            sha, response = fixtures.blob(raw)
            mock.tree.append({
                "path": path, "type": "blob", "mode": "100644",
                "size": len(raw), "sha": sha,
            })
            mock.maps[base + "/git/blobs/" + sha] = response
        module_contents = {
            "app/lib/server.mjs": (
                b"import { createDatabasePool } from '../../src/database-pool.mjs'\n"
            ),
            "src/claim.mjs": (
                b"import { users, orders } from './db/schema.mjs'\n"
            ),
            "src/database-pool.mjs": b"export function createDatabasePool() {}\n",
        }
        for path, raw in module_contents.items():
            sha, response = fixtures.blob(raw)
            mock.tree.append({
                "path": path, "type": "blob", "mode": "100644",
                "size": len(raw), "sha": sha,
            })
            mock.maps[base + "/git/blobs/" + sha] = response
        schema = (b"import { pgTable } from 'drizzle-orm/pg-core'\n"
                  b"export const users = pgTable('users', {})\n"
                  b"export const orders = pgTable(\"orders\", {})\n"
                  b"// pgTable('not_a_real_declaration', {})\n")
        sha, response = fixtures.blob(schema)
        mock.tree.append({"path": "src/db/schema.mjs", "type": "blob",
                          "mode": "100644", "sha": sha, "size": len(schema)})
        mock.maps[base + "/git/blobs/" + sha] = response
        return mock

    def test_routes_from_tree_and_schema_from_verified_blob(self):
        mock = self.fixture()
        graph = gh.discover_github(mock.repo, mock)
        routes = [n for n in graph["nodes"] if n["kind"] == "api_route"]
        tables = [n for n in graph["nodes"] if n["kind"] == "declared_db_table"]
        self.assertEqual({n["label"] for n in routes},
                         {"/api/health", "/api/orders/[id]"})
        self.assertEqual({n["label"] for n in tables}, {"users", "orders"})
        self.assertEqual({n["source_line"] for n in tables}, {2, 3})
        self.assertEqual(graph["coverage"]["api_route_paths_found"], 2)
        self.assertEqual(graph["coverage"]["db_schema_candidates_found"], 2)
        self.assertEqual(graph["coverage"]["api_route_source_files_read"], 0)
        self.assertEqual(graph["coverage"]["schema_source_files_read"], 1)
        self.assertEqual(graph["coverage"]["import_route_files_inspected"], 2)
        self.assertEqual(graph["coverage"]["import_relationships_found"], 2)
        self.assertEqual(graph["coverage"]["import_modules_found"], 2)
        self.assertEqual(graph["coverage"]["second_hop_modules_read"], 2)
        self.assertEqual(graph["coverage"]["second_hop_import_edges"], 2)
        self.assertEqual(graph["coverage"]["second_hop_schema_table_declarations"], 2)
        self.assertEqual(len([e for e in graph["edges"]
                              if e["relation"] == "DECLARES_TABLE_CANDIDATE"]), 2)
        self.assertTrue(any(
            e["relation"] == "STATIC_IMPORTS" and
            "src/claim.mjs" in e["source"] and
            "src/db/schema.mjs" in e["target"]
            for e in graph["edges"]))
        self.assertTrue(any(n["label"] == "src/db/schema.mjs"
                            for n in graph["nodes"] if n["kind"] == "source_module"))
        imported = [e for e in graph["edges"] if e["relation"] == "STATIC_IMPORTS"]
        self.assertEqual({e["source_line"] for e in imported}, {1})
        self.assertEqual({n["label"] for n in graph["nodes"]
                          if n["kind"] == "source_module"},
                         {"src/claim.mjs", "app/lib/server.mjs",
                          "src/db/schema.mjs", "src/database-pool.mjs"})
        self.assertTrue(all(e["state"] == "SOURCE_SYNTAX_CANDIDATE"
                            for e in imported))
        self.assertEqual(graph["runtime_health"], "UNKNOWN")
        self.assertFalse(graph["execution_authorized"])
        self.assertFalse(any("/git/blobs/" + "c"*40 in call for call in mock.requested))
        ev = {e["id"]: e for e in graph["source_evidence"]}
        for node in routes:
            self.assertEqual(ev[node["evidence_id"]]["origin"],
                             "GITHUB_REPOSITORY_TREE_ENTRY")
            self.assertEqual(ev[node["evidence_id"]]["authenticity"],
                             "TREE_PATH_ONLY_SOURCE_BYTES_NOT_READ")
        for node in tables:
            self.assertEqual(node["state"], "SOURCE_SYNTAX_CANDIDATE")
            self.assertEqual(ev[node["evidence_id"]]["origin"],
                             "GITHUB_COMMIT_PINNED_BLOB")
        state = room.build_state(graph, [], {})
        self.assertTrue(all(n["status"] == "unknown" for n in state["nodes"]))
        with tempfile.TemporaryDirectory() as tmp:
            store = ss.SnapshotStore(Path(tmp) / "private")
            store.save(graph)
            restored, _ = store.load(mock.repo)
            self.assertEqual(restored, graph)

    def test_schema_blob_corruption_exposes_gap_without_fake_table(self):
        mock = self.fixture()
        base = f"/repos/{mock.repo}"
        e = next(r for r in mock.tree if r["path"] == "src/db/schema.mjs")
        mock.maps[base + "/git/blobs/" + e["sha"]]["content"] = "not-base64!"
        graph = gh.discover_github(mock.repo, mock)
        self.assertEqual(graph["coverage"]["db_schema_candidates_found"], 0)
        self.assertEqual(len([n for n in graph["nodes"]
                              if n["kind"] == "declared_db_table"]), 0)
        self.assertEqual(graph["coverage"]["api_route_paths_found"], 2)
        self.assertIn("SCHEMA_SYMBOL_SCAN_UNAVAILABLE",
                      [g["reason"] for g in graph["gaps"]])

    def test_all_evidence_unique_and_graph_consistent(self):
        mock = self.fixture()
        graph = gh.discover_github(mock.repo, mock)
        ids = [e["id"] for e in graph["source_evidence"]]
        self.assertEqual(len(ids), len(set(ids)))
        nodes = {n["id"] for n in graph["nodes"]}
        for edge in graph["edges"]:
            self.assertIn(edge["source"], nodes)
            self.assertIn(edge["target"], nodes)
        table_edges = [e for e in graph["edges"]
                       if e["state"] == "SOURCE_SYNTAX_CANDIDATE"
                       and e["relation"] == "DECLARES"]
        self.assertEqual(len(table_edges), 2)
        self.assertFalse(any(e["relation"] == "DEPENDS_ON"
                             for e in table_edges))

    def test_bad_schema_not_executed_and_not_health(self):
        mock = self.fixture()
        graph = gh.discover_github(mock.repo, mock)
        self.assertNotIn("not_a_real_declaration",
                         [n["label"] for n in graph["nodes"]])
        self.assertEqual(graph["coverage"]["schema_source_files_read"], 1)
        self.assertNotIn("import { pgTable", str(graph))

    def test_determinism_and_identity_across_remapped_repository(self):
        mock = self.fixture()
        a = gh.discover_github(mock.repo, mock)
        b = gh.discover_github(mock.repo, mock)
        self.assertEqual(a, b)
        self.assertEqual(len([n for n in a["nodes"]
                              if n["kind"] == "declared_db_table"]), 2)
        self.assertTrue(all("gh1234" in n["id"] for n in a["nodes"]))


if __name__ == "__main__":
    unittest.main()
