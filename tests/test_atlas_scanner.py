"""Offline safety + determinism for the Atlas GitHub scanner and public HTML sample."""
import base64
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "atlas/scan_github.py"
spec = importlib.util.spec_from_file_location("atlas_scanner", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scanner
spec.loader.exec_module(scanner)
SHA = "e" * 40


def fake(repo="Example/one", private=False, package=None, compose=None):
    package = package or {"scripts": {"build": "touch none", "worker": "node worker"},
                          "dependencies": {"react": "*", "next": "*"}}
    paths = ["package.json", ".github/workflows/ci.yml"]
    if compose is not None:
        paths.append("deploy/compose.yaml")
    tree = [{"path": "src", "type": "tree"}, {"path": "deploy", "type": "tree"}]
    tree += [{"path": path, "type": "blob"} for path in paths]

    def api(path):
        if path == "/repos/" + repo:
            return {"full_name": repo, "name": repo.split("/")[1],
                    "private": private, "default_branch": "main"}
        if path == "/repos/" + repo + "/branches/main":
            return {"commit": {"sha": SHA}}
        if path == "/repos/" + repo + "/git/trees/" + SHA + "?recursive=1":
            return {"tree": tree, "truncated": False}
        if "/contents/" in path:
            name = path.split("/contents/")[1].split("?")[0]
            raw = (json.dumps(package) if name == "package.json" else compose).encode()
            return {"encoding": "base64", "content": base64.b64encode(raw).decode(),
                    "size": len(raw)}
        raise AssertionError("Unexpected API request " + path)
    return api


class AtlasTests(unittest.TestCase):
    def test_project_graph(self):
        g = scanner.scan(["Example/one"], fake())
        self.assertEqual(len(g["projects"]), 1)
        self.assertEqual(g["projects"][0]["commit"], SHA)
        self.assertTrue(any(n["kind"] == "workflow" for n in g["nodes"]))
        self.assertTrue(any(n["kind"] == "module" for n in g["nodes"]))
        self.assertTrue(any(n["label"] == "worker" and n["kind"] == "script" for n in g["nodes"]))
        self.assertTrue(any(n["label"] == "react" and n["kind"] == "technology" for n in g["nodes"]))
        self.assertNotIn("touch none", json.dumps(g))
        self.assertEqual(g["coverage"]["runtime_sources_connected"], 0)
        self.assertTrue(g["source_evidence"][0]["sha256"])

    def test_private_metadata_is_explicit(self):
        g = scanner.scan(["Example/private"], fake(repo="Example/private", private=True))
        self.assertTrue(g["projects"][0]["private"])
        self.assertEqual(g["coverage"]["private_repositories"], 1)

    def test_compose_dependency_and_alias(self):
        compose = ("services:\n  web: &default\n    image: nginx\n"
                   "  gateway:\n    <<: *default\n    depends_on: [web]\n")
        g = scanner.scan(["Example/one"], fake(compose=compose))
        deps = [e for e in g["edges"] if e["relation"] == "DEPENDS_ON"]
        self.assertEqual(len(deps), 1)
        self.assertIn("gateway", deps[0]["source"])
        self.assertIn("web", deps[0]["target"])

    def test_broken_service_declaration_rolls_back(self):
        g = scanner.scan(["Example/one"], fake(compose="services:\n  web: {}\n  bad: text\n"))
        self.assertFalse(any(n["kind"] == "service" for n in g["nodes"]))
        self.assertTrue(any(x["reason"] == "MANIFEST_PARSE_FAILED" for x in g["gaps"]))

    def test_unknown_dependency_is_gap_not_node(self):
        g = scanner.scan(["Example/one"], fake(compose="services:\n  web:\n    depends_on: [missing]\n"))
        self.assertTrue(any(x["reason"] == "UNRESOLVED_COMPOSE_DEPENDENCY" for x in g["gaps"]))
        self.assertFalse(any(n["label"] == "missing" for n in g["nodes"]))

    def test_reject_truncated_tree(self):
        def api(path):
            result = fake()(path)
            if "?recursive=1" in path:
                result["truncated"] = True
            return result
        with self.assertRaises(scanner.ScanError):
            scanner.scan_repository("Example/one", api)

    def test_auth_token_is_not_part_of_graph(self):
        g = scanner.scan(["Example/one"], fake())
        serialized = json.dumps(g)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("GITHUB_TOKEN", serialized)

    def test_duplicates_and_bad_identifiers_refused(self):
        with self.assertRaises(scanner.ScanError):
            scanner.scan(["Example/one", "Example/one"], fake())
        with self.assertRaises(scanner.ScanError):
            scanner.scan_repository("https://github.com/Example/one", fake())

    def test_shared_technology_identity(self):
        a, b = fake("Example/one"), fake("Example/two")
        g = scanner.scan(["Example/one", "Example/two"],
                         lambda p: a(p) if "/Example/one" in p else b(p))
        self.assertEqual(sum(n["id"] == "tech:react" for n in g["nodes"]), 1)
        self.assertEqual(sum(e["target"] == "tech:react" for e in g["edges"]), 2)

    def test_write_is_private_and_refuses_overwrite(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as folder:
            target = pathlib.Path(folder) / "snapshot.json"
            with patch.object(scanner, "access_token", return_value=""), \
                 patch.object(scanner, "request", side_effect=lambda path, token="": fake()(path)):
                self.assertEqual(scanner.main(["Example/one", "--out", str(target)]), 0)
                self.assertEqual(scanner.main(["Example/one", "--out", str(target)]), 1)
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
            self.assertEqual(json.loads(target.read_text())["schema_version"], 1)

    def test_html_embeds_only_public_example(self):
        page = (ROOT / "atlas/index.html").read_text()
        start = '<script id="atlas-data" type="application/json">'
        self.assertIn(start, page)
        raw = page.split(start, 1)[1].split("</script>", 1)[0]
        data = json.loads(raw)
        self.assertTrue(data["projects"])
        self.assertTrue(all(not p["private"] for p in data["projects"]))
        self.assertTrue(all(n["project"] in {p["id"] for p in data["projects"]}
                            for n in data["nodes"] if n.get("project")))
        self.assertNotIn("New1Direction/FYNCH", page)
        self.assertNotIn("New1Direction/ImOrb", page)
        self.assertNotIn("New1Direction/colony", page)
        self.assertNotIn("github.com/", data["disclaimer"])


if __name__ == "__main__":
    unittest.main()
