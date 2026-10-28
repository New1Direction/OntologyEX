"""Read-only first-pass business graph discovery regression tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import discover_repo as scanner


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "example-company"
        self.repo.mkdir()

    def write(self, path, contents):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        return target

    def test_empty_project_is_explicitly_partial(self):
        report = scanner.discover(self.repo)
        self.assertEqual(len(report["nodes"]), 1)
        self.assertEqual(report["coverage"]["candidate_manifests"], 0)
        self.assertEqual(report["runtime_health"], "UNKNOWN")
        self.assertEqual(report["human_review"], "REQUIRED")
        self.assertFalse(report["execution_authorized"])
        self.assertIn("business_processes", report["coverage"]["not_assessed"])

    def test_compose_services_and_declared_dependency(self):
        self.write("compose.yaml", """services:
  api:
    image: example/api
    depends_on:
      db:
        condition: service_healthy
  db:
    image: postgres
""")
        report = scanner.discover(self.repo)
        services = [n for n in report["nodes"] if n["kind"] == "declared_service"]
        self.assertEqual({n["label"] for n in services}, {"api", "db"})
        links = [e for e in report["edges"] if e["relation"] == "DEPENDS_ON"]
        self.assertEqual(len(links), 1)
        self.assertIn("api", links[0]["source"])
        self.assertIn("db", links[0]["target"])
        self.assertEqual(links[0]["state"], "MANIFEST_DECLARATION")
        self.assertEqual(len(report["source_evidence"]), 1)

    def test_unresolved_dependency_not_invented(self):
        self.write("compose.yml", "services:\n  web:\n    depends_on: [missing]\n")
        report = scanner.discover(self.repo)
        self.assertEqual(len(report["unresolved"]), 1)
        self.assertEqual(report["unresolved"][0]["target_name"], "missing")
        self.assertFalse(any("missing" in x["id"] for x in report["nodes"]))

    def test_scans_monorepo_and_workflows(self):
        self.write("package.json", '{"name":"root","private":true}')
        self.write("apps/api/pyproject.toml", '[project]\nname="api"\nversion="0.1.0"\n')
        self.write(".github/workflows/ci.yml", "name: Tests\non: push\njobs: {}\n")
        report = scanner.discover(self.repo)
        self.assertEqual(report["coverage"]["candidate_manifests"], 3)
        self.assertEqual(len([n for n in report["nodes"] if n["kind"] == "workflow"]), 1)
        self.assertEqual(len([n for n in report["nodes"] if n["kind"] == "source_manifest"]), 2)

    def test_bad_yaml_preserves_gap_not_false_success(self):
        self.write("compose.yaml", "services:\n  api: {}\n  api: {}\n")
        report = scanner.discover(self.repo)
        self.assertEqual(report["gaps"][0]["reason"], "PARSE_OR_VALIDATION_FAILED")
        self.assertEqual(len(report["nodes"]), 1)
        self.assertEqual(len(report["edges"]), 0)

    def test_aliases_and_anchors_are_refused(self):
        self.write("compose.yml", "services:\n  db: &x {}\n  web: *x\n")
        report = scanner.discover(self.repo)
        self.assertEqual(report["gaps"][0]["reason"], "PARSE_OR_VALIDATION_FAILED")
        self.assertEqual(len(report["nodes"]), 1)

    def test_parse_failures_report_a_stable_category(self):
        self.write("a/compose.yml", "services:\n  a: !reset {}\n")       # explicit tag
        self.write("b/compose.yml", "services:\n  a:\t{}\n")             # tab cannot start a token
        self.write("c/compose.yml", "services: 1\n")                     # valid YAML, invalid compose
        self.write("d/package.json", "[" * 513 + "]" * 513)              # nested too deeply
        self.write("e/Cargo.toml", "a = 1e400\n")                        # float overflow is not infinity
        self.write("f/pyproject.toml", "a = inf\n")                      # inf is a TOML float
        self.write("g/package.json", "[" * 512 + "]" * 512)
        (self.repo / "h").mkdir()
        (self.repo / "h" / "package.json").write_bytes(b'{"a": "\xff"}')
        gaps = {g["source"]: g["error_type"] for g in scanner.discover(self.repo)["gaps"]}
        self.assertEqual(gaps, {"a/compose.yml": "YAMLError", "b/compose.yml": "YAMLError",
                                "c/compose.yml": "DiscoveryError", "d/package.json": "JSONDecodeError",
                                "e/Cargo.toml": "TOMLDecodeError", "h/package.json": "UnicodeError"})

    def test_yaml_nesting_limit(self):
        self.write("ok/compose.yml", "x: " + "[" * 63 + "]" * 63 + "\nservices: {}\n")
        self.write("deep/compose.yml", "x: " + "[" * 64 + "]" * 64 + "\nservices: {}\n")
        gaps = [g["source"] for g in scanner.discover(self.repo)["gaps"]]
        self.assertEqual(gaps, ["deep/compose.yml"])

    def test_malformed_json_is_reported(self):
        self.write("package.json", '{"name":')
        report = scanner.discover(self.repo)
        self.assertEqual(report["gaps"][0]["reason"], "PARSE_OR_VALIDATION_FAILED")
        self.assertEqual(report["source_evidence"][0]["path"], "package.json")

    def test_symlinks_not_followed(self):
        external = Path(self.temp.name) / "outside"
        external.mkdir()
        (external / "package.json").write_text('{"name":"private"}')
        (self.repo / "linked").symlink_to(external, target_is_directory=True)
        (self.repo / "package.json").symlink_to(external / "package.json")
        report = scanner.discover(self.repo)
        self.assertEqual(report["coverage"]["candidate_manifests"], 0)
        self.assertEqual(report["coverage"]["symlinks_skipped"], 2)
        self.assertFalse(any(n["kind"] == "source_manifest" for n in report["nodes"]))

    def test_reject_symlink_root(self):
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(scanner.DiscoveryError):
            scanner.discover(alias)

    def test_skip_build_and_dependency_directories(self):
        self.write("node_modules/lib/package.json", '{"name":"node"}')
        self.write("dist/package.json", '{"name":"dist"}')
        self.write(".venv/pyproject.toml", "[project]\nname='venv'\n")
        self.write("package.json", '{"name":"real"}')
        report = scanner.discover(self.repo)
        self.assertEqual(report["coverage"]["candidate_manifests"], 1)

    def test_deterministic_and_evidence_hashed(self):
        self.write("package.json", '{"name":"sample"}')
        self.write("api/Dockerfile", "FROM scratch\n")
        a = scanner.discover(self.repo)
        b = scanner.discover(self.repo)
        self.assertEqual(a, b)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertTrue(all(len(e["sha256"]) == 64 for e in a["source_evidence"]))
        self.assertFalse(any("FROM scratch" in json.dumps(n) for n in a["nodes"]))

    def test_source_byte_limit_is_visible(self):
        self.write("package.json", "x" * (scanner.MAX_FILE_BYTES + 1))
        report = scanner.discover(self.repo)
        self.assertEqual(report["gaps"][0]["reason"], "SOURCE_SIZE_LIMIT")
        self.assertEqual(len(report["nodes"]), 1)

    def test_visited_budget_fails_closed(self):
        self.write("package.json", '{"name":"example"}')
        with patch.object(scanner, "MAX_WALK_FILES", 0):
            with self.assertRaises(scanner.DiscoveryError):
                scanner.discover(self.repo)

    def test_manifest_budget_fails_closed(self):
        self.write("package.json", '{"name":"example"}')
        with patch.object(scanner, "MAX_MANIFESTS", 0):
            with self.assertRaises(scanner.DiscoveryError):
                scanner.discover(self.repo)

    def test_no_environment_or_secret_file_contents(self):
        self.write(".env", "TOKEN=secret-test-value\n")
        self.write("package.json", '{"name":"safe","scripts":{"postinstall":"exit 1"}}')
        report = scanner.discover(self.repo)
        encoded = json.dumps(report)
        self.assertNotIn("secret-test-value", encoded)
        self.assertNotIn("postinstall", encoded)
        self.assertEqual(report["coverage"]["candidate_manifests"], 1)

    def test_service_name_validation_rolls_back_entire_manifest(self):
        self.write("compose.yaml", "services:\n  okay: {}\n  'bad service': {}\n")
        report = scanner.discover(self.repo)
        self.assertEqual(report["gaps"][0]["reason"], "PARSE_OR_VALIDATION_FAILED")
        self.assertEqual(len(report["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
