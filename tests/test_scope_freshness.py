"""Integration regressions for scope preservation and portable freshness."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import domain_skill as ds
import scaffold
from policy_check import canonical, digest


class ScopeAndPortableFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.demo = cls.base / "payments"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "examples/payments-domain/demo.py"),
             "--out", str(cls.demo)], capture_output=True, text=True, timeout=30)
        if completed.returncode:
            raise RuntimeError(completed.stdout + "\n" + completed.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_compiled_scope_and_rule_predicates_are_visible(self):
        skill = self.demo / "v1/payments-domain"
        model = json.loads((skill / "model.json").read_text())
        scope = (self.demo / "v1/workspace/00-scope.md").read_text()
        self.assertEqual(model["authored_scope"], scope)
        self.assertIn("Out of scope: real APIs", model["authored_scope"])
        page = (skill / "report.html").read_text()
        self.assertIn("Authored scope and boundaries", page)
        self.assertIn("Out of scope: real APIs", page)
        self.assertIn('class=', page)
        self.assertIn("predicate", page)

    def test_scope_only_change_is_a_real_revision(self):
        base = self.demo / "v1"
        workspace = self.base / "changed_workspace"
        shutil.copytree(base / "workspace", workspace)
        scope = workspace / "00-scope.md"
        scope.write_text(scope.read_text() + "\nProduction refunds are additionally excluded.\n")
        target = self.base / "new_candidate/payments-domain"
        ds.build(workspace, target, base / "repo")
        comparison = ds.compare(base / "payments-domain", target)
        self.assertTrue(comparison["scope_changed"])
        self.assertEqual(comparison["status"], "REVIEW_REQUIRED")
        self.assertIn("IssueRefund", comparison["rerun_tasks"])
        self.assertNotEqual(json.loads((base / "payments-domain/manifest.json").read_text())["bundle_id"],
                            json.loads((target / "manifest.json").read_text())["bundle_id"])

    def test_portable_skill_freshness_no_author_workspace(self):
        base = self.demo / "v1"
        self.assertEqual(ds.freshness(base / "payments-domain", base / "repo")["status"],
                         "SELECTED_SOURCES_UNCHANGED")
        with self.assertRaisesRegex(ValueError, "live source drift"):
            ds.freshness(base / "payments-domain", self.demo / "v2/repo")

    def test_portable_handoff_freshness_and_integrity(self):
        base = self.demo / "v1"
        skill = base / "payments-domain"
        context = json.loads((skill / "model.json").read_text())
        handoff = self.base / "portable_handoff"
        handoff.mkdir()
        srcs = {}
        for name in context["source_files"]:
            data = (skill / "references/sources" / name).read_bytes()
            file = handoff / "sources" / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            srcs["sources/" + name] = digest(data)
        summary = canonical({"source_files": context["source_files"]})
        (handoff / "context.json").write_bytes(summary)
        srcs["context.json"] = digest(summary)
        (handoff / "handoff.json").write_bytes(canonical(
            {"bundle_id": "test", "task": "IssueRefund", "files": srcs}))
        self.assertEqual(ds.freshness(handoff, base / "repo")["artifact"], "handoff")
        (handoff / "sources/requirements.md").write_text("modified")
        with self.assertRaisesRegex(ValueError, "tampering"):
            ds.freshness(handoff, base / "repo")

    def test_old_validator_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "20-domain.yaml").write_text("classes: []\nclasses: []\n")
            with self.assertRaisesRegex(scaffold.WorkspaceError, "duplicate"):
                scaffold.load(folder, "20-domain.yaml")

    def test_rejected_output_parent_is_not_created(self):
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            external = r / "external"
            external.mkdir()
            (r / "alias").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                ds.write_files(r / "alias" / "new" / "payments-domain",
                               {"test.txt": b"test"})
            self.assertFalse((external / "new").exists())


if __name__ == "__main__":
    unittest.main()
