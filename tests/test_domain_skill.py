"""Offline regression, portability, evidence, and change-impact checks."""
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import domain_skill as ds
import policy_check as pc


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


demo = module("ontologyex_payments_demo", ROOT / "examples/payments-domain/demo.py")
scorer = module("ontologyex_fixture_scorer", ROOT / "examples/payments-domain/score.py")


class DomainSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = tempfile.TemporaryDirectory()
        cls.seed_root = Path(cls.seed.name)
        cls.source = ROOT / "examples/payments-domain/source"
        cls.seed_workspace = cls.seed_root / "workspace"
        ds.prepare(cls.source, ["requirements.md", "payments.py"], cls.seed_workspace, demo.NAME, demo.GOAL)
        demo.populate(cls.seed_workspace)
        cls.seed_skill = cls.seed_root / demo.NAME
        ds.build(cls.seed_workspace, cls.seed_skill, cls.source)
        cls.model = pc.parse_json((cls.seed_skill / "model.json").read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.seed.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(self.seed_workspace, self.workspace)
        self.out = self.root / demo.NAME
        self.contract = json.loads((self.workspace / "60-contract.json").read_text())
        self.values = {"amount_minor": 500, "captured_minor": 10000, "refunded_minor": 8000,
                       "payment_status": "captured", "requester_authorized": True}

    def save_contract(self):
        (self.workspace / "60-contract.json").write_bytes(pc.canonical(self.contract))

    def compile(self):
        self.save_contract()
        return ds.build(self.workspace, self.out)

    def reject(self):
        with self.assertRaises(pc.DomainError):
            self.compile()
        self.assertFalse(self.out.exists())

    def test_valid_build(self):
        result = self.compile()
        self.assertEqual(result["status"], "UNREVIEWED_CANDIDATE")
        self.assertEqual(pc.verify_bundle(self.out)["bundle_id"], result["bundle_id"])

    def test_deterministic_bundle(self):
        first = ds.build(self.workspace, self.out, self.source)
        other = self.root / "again" / demo.NAME
        second = ds.build(self.workspace, other, self.source)
        self.assertEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(pc.file_inventory(self.out), pc.file_inventory(other))

    def test_existing_output_not_overwritten(self):
        self.compile()
        before = pc.file_inventory(self.out)
        with self.assertRaises(pc.DomainError):
            self.compile()
        self.assertEqual(before, pc.file_inventory(self.out))

    def test_wrong_skill_directory_name(self):
        with self.assertRaises(pc.DomainError):
            ds.build(self.workspace, self.root / "wrong")

    def test_prepare_labels_unfinished(self):
        result = ds.prepare(self.source, ["requirements.md"], self.root / "prepared", "my-domain", "Map refunds.")
        self.assertEqual(result["status"], "PREPARED_NOT_EXTRACTED")
        with self.assertRaises(pc.DomainError):
            ds.build(self.root / "prepared", self.root / "my-domain")

    def test_prepare_rejects_no_sources(self):
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.source, [], self.root / "new", demo.NAME, demo.GOAL)

    def test_prepare_rejects_duplicate_sources(self):
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.source, ["payments.py"] * 2, self.root / "new", demo.NAME, demo.GOAL)

    def test_prepare_file_count_budget(self):
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.source, [str(i) for i in range(65)], self.root / "new", demo.NAME, demo.GOAL)

    def test_prepare_invalid_slug(self):
        for name in ("Bad Name", "x--y", "-x", "x" * 65):
            with self.subTest(name=name), self.assertRaises(pc.DomainError):
                ds.prepare(self.source, ["payments.py"], self.root / "new", name, demo.GOAL)

    def test_path_traversal_rejected(self):
        for path in ("../outside", "/etc/passwd", "a/../b", "a\\b", "a//b", "./x", "C:/x"):
            with self.subTest(path=path), self.assertRaises(pc.DomainError):
                ds.prepare(self.source, [path], self.root / "new", demo.NAME, demo.GOAL)

    def test_source_symlink_rejected(self):
        (self.root / "link.py").symlink_to(self.source / "payments.py")
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.root, ["link.py"], self.root / "new", demo.NAME, demo.GOAL)

    def test_secret_name_rejected(self):
        (self.root / ".env").write_text("SECRET=not-real")
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.root, [".env"], self.root / "new", demo.NAME, demo.GOAL)

    def test_private_key_marker_rejected(self):
        (self.root / "doc.txt").write_text("-----BEGIN PRIVATE KEY-----")
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.root, ["doc.txt"], self.root / "new", demo.NAME, demo.GOAL)

    def test_binary_source_rejected(self):
        (self.root / "doc.txt").write_bytes(b"a\x00b")
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.root, ["doc.txt"], self.root / "new", demo.NAME, demo.GOAL)

    def test_large_source_rejected(self):
        (self.root / "doc.txt").write_bytes(b"x" * (pc.MAX_FILE + 1))
        with self.assertRaises(pc.DomainError):
            ds.prepare(self.root, ["doc.txt"], self.root / "new", demo.NAME, demo.GOAL)

    def test_empty_contract_rejected(self):
        self.contract = {}
        self.reject()

    def test_unknown_contract_key_rejected(self):
        self.contract["confidnce"] = 1
        self.reject()

    def test_duplicate_json_key_rejected(self):
        with self.assertRaises(pc.DomainError):
            pc.parse_json(b'{"a": 1, "a": 2}')

    def test_json_nan_rejected(self):
        with self.assertRaises(pc.DomainError):
            pc.parse_json(b'{"a": NaN}')

    def test_missing_layer_rejected(self):
        (self.workspace / "30-task.yaml").unlink()
        self.reject()

    def test_broken_layer_reference_rejected(self):
        p = self.workspace / "20-domain.yaml"
        p.write_text(p.read_text().replace("upper: Intangible", "upper: Missing", 1))
        self.reject()

    def test_duplicate_yaml_key_rejected(self):
        p = self.workspace / "20-domain.yaml"
        p.write_text(p.read_text() + "\nclasses: []\n")
        self.reject()

    def test_yaml_alias_rejected(self):
        p = self.workspace / "20-domain.yaml"
        p.write_text(p.read_text() + "\ncycle: &x [*x]\n")
        self.reject()

    def test_evidence_hash_mismatch(self):
        self.contract["evidence"][0]["sha256"] = "0" * 64
        self.reject()

    def test_evidence_quote_mismatch(self):
        self.contract["evidence"][0]["quote"] = "This was not in the source."
        self.reject()

    def test_evidence_span_outside_file(self):
        self.contract["evidence"][0]["end_line"] = 10000
        self.reject()

    def test_boolean_line_number_rejected(self):
        self.contract["evidence"][0]["start_line"] = True
        self.reject()

    def test_unselected_evidence_path_rejected(self):
        self.contract["evidence"][0]["path"] = "unselected.py"
        self.reject()

    def test_duplicate_evidence_id_rejected(self):
        self.contract["evidence"].append(copy.deepcopy(self.contract["evidence"][0]))
        self.reject()

    def test_unsupported_evidence_kind_rejected(self):
        self.contract["evidence"][0]["kind"] = "model-confidence"
        self.reject()

    def test_missing_rule_evidence_rejected(self):
        self.contract["rules"][0]["evidence"] = []
        self.reject()

    def test_dangling_rule_evidence_rejected(self):
        self.contract["rules"][0]["evidence"] = ["E-missing"]
        self.reject()

    def test_layer_source_must_be_evidence_id(self):
        p = self.workspace / "20-domain.yaml"
        p.write_text(p.read_text().replace("source: E-Payment", "source: https://example.org"))
        self.reject()

    def test_snapshot_tampering_rejected(self):
        p = self.workspace / "sources/payments.py"
        p.write_text(p.read_text() + "\n# changed\n")
        self.reject()

    def test_live_source_drift_rejected(self):
        repo = self.root / "repo"
        shutil.copytree(self.source, repo)
        (repo / "requirements.md").write_text("new requirements")
        with self.assertRaises(pc.DomainError):
            ds.build(self.workspace, self.out, repo)
        self.assertFalse(self.out.exists())

    def test_source_byte_count_mismatch(self):
        p = self.workspace / "sources.json"
        obj = json.loads(p.read_text())
        obj["files"]["payments.py"]["bytes"] += 1
        p.write_bytes(pc.canonical(obj))
        self.reject()

    def test_tasks_must_match_schemas(self):
        del self.contract["inputs"]["ApproveException"]
        self.reject()

    def test_unmodeled_task_cannot_pass(self):
        self.contract["unknowns"] = []
        self.reject()

    def test_preconditions_must_match_rules(self):
        self.contract["rules"].pop()
        self.reject()

    def test_stale_saved_mapping_rejected(self):
        (self.workspace / "50-mappings.yaml").write_text("mappings: []\n")
        self.reject()

    def test_duplicate_rule_id_rejected(self):
        self.contract["rules"].append(copy.deepcopy(self.contract["rules"][0]))
        self.reject()

    def test_unknown_expression_operator_rejected(self):
        self.contract["rules"][0]["check"]["op"] = "eval"
        self.reject()

    def test_python_expression_rejected(self):
        self.contract["rules"][0]["check"]["right"] = {"python": "__import__('os').system('false')"}
        self.reject()

    def test_unknown_field_rejected(self):
        self.contract["rules"][0]["check"]["left"] = "nonexistent"
        self.reject()

    def test_check_operand_types_must_match(self):
        self.contract["rules"][0]["check"]["right"] = {"value": "0"}
        self.reject()

    def test_string_ordering_rejected(self):
        self.contract["rules"][4]["check"]["op"] = "gte"
        self.reject()

    def test_overflow_constant_rejected(self):
        self.contract["rules"][0]["check"]["right"] = {"value": 2**80}
        self.reject()

    def test_inferred_rule_requires_review(self):
        model = copy.deepcopy(self.model)
        model["rules"][0]["status"] = "inferred"
        self.assertEqual(pc.evaluate(model, "IssueRefund", self.values)["status"], "NEEDS_REVIEW")

    def test_manual_rule_requires_review(self):
        model = copy.deepcopy(self.model)
        model["rules"][0]["check"] = None
        self.assertEqual(pc.evaluate(model, "IssueRefund", self.values)["status"], "NEEDS_REVIEW")

    def test_conflict_requires_review(self):
        model = copy.deepcopy(self.model)
        model["conflicts"] = [{"id": "conflict-1", "task": "IssueRefund", "statement": "Policies disagree.",
                              "evidence": ["E-Payment", "E-Refund"]}]
        self.assertEqual(pc.evaluate(model, "IssueRefund", self.values)["status"], "NEEDS_REVIEW")

    def test_conflict_requires_two_sources(self):
        self.contract["conflicts"] = [{"id": "conflict-1", "task": "IssueRefund", "statement": "Policies disagree.", "evidence": ["E-Payment"]}]
        self.reject()

    def test_unknown_exception_stays_unknown(self):
        self.assertEqual(pc.evaluate(self.model, "ApproveException", {})["status"], "NEEDS_REVIEW")

    def test_valid_refund_passes_without_authorizing_execution(self):
        result = pc.evaluate(self.model, "IssueRefund", self.values)
        self.assertEqual(result["status"], "CHECKS_PASS")
        self.assertFalse(result["execution_authorized"])

    def test_over_refund_fails(self):
        self.values["amount_minor"] = 3000
        result = pc.evaluate(self.model, "IssueRefund", self.values)
        self.assertEqual(result["status"], "CHECKS_FAIL")
        self.assertIn("remaining-balance", result["failed"])

    def test_boolean_amount_rejected(self):
        self.values["amount_minor"] = True
        with self.assertRaises(pc.DomainError):
            pc.evaluate(self.model, "IssueRefund", self.values)

    def test_float_amount_rejected(self):
        self.values["amount_minor"] = 5.0
        with self.assertRaises(pc.DomainError):
            pc.evaluate(self.model, "IssueRefund", self.values)

    def test_missing_input_rejected(self):
        del self.values["requester_authorized"]
        with self.assertRaises(pc.DomainError):
            pc.evaluate(self.model, "IssueRefund", self.values)

    def test_extra_input_rejected(self):
        self.values["ignore_policy"] = True
        with self.assertRaises(pc.DomainError):
            pc.evaluate(self.model, "IssueRefund", self.values)

    def test_unknown_action_rejected(self):
        with self.assertRaises(pc.DomainError):
            pc.evaluate(self.model, "UnknownAction", {})

    def test_integer_subtraction_overflow_rejected(self):
        self.values.update(captured_minor=pc.MAX_INTEGER, refunded_minor=-pc.MAX_INTEGER)
        with self.assertRaises(pc.DomainError):
            pc.evaluate(self.model, "IssueRefund", self.values)

    def test_integrity_detects_added_file(self):
        self.compile()
        (self.out / "unexpected.txt").write_text("unexpected")
        with self.assertRaises(pc.DomainError):
            pc.verify_bundle(self.out)

    def test_integrity_detects_removed_file(self):
        self.compile()
        (self.out / "report.html").unlink()
        with self.assertRaises(pc.DomainError):
            pc.verify_bundle(self.out)

    def test_integrity_detects_modified_checker(self):
        self.compile()
        p = self.out / "scripts/check.py"
        p.write_text(p.read_text() + "\n# changed")
        with self.assertRaises(pc.DomainError):
            pc.verify_bundle(self.out)

    def test_integrity_rejects_symlink(self):
        self.compile()
        (self.out / "link").symlink_to(self.source)
        with self.assertRaises(pc.DomainError):
            pc.verify_bundle(self.out)

    def test_report_escapes_html(self):
        model = copy.deepcopy(self.model)
        model["rules"][0]["statement"] = "<script>alert('bad')</script>"
        _, page = ds.report(model)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("default-src 'none'", page)

    def test_root_goal_propagated(self):
        self.compile()
        self.assertIn(self.contract["root_goal_id"], (self.out / "SKILL.md").read_text())
        self.assertIn(self.contract["root_goal_id"], (self.out / "report.html").read_text())

    def test_standalone_checker_no_site_packages(self):
        self.compile()
        inputs = self.root / "input.json"
        inputs.write_bytes(pc.canonical(self.values))
        result = subprocess.run([sys.executable, "-I", "-S", str(self.out / "scripts/check.py"),
                                 "--task", "IssueRefund", "--input", str(inputs)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "CHECKS_PASS")

    def test_checker_cli_exit_codes(self):
        self.compile()
        for task, values, expected in (("IssueRefund", {**self.values, "amount_minor": 3000}, 2),
                                       ("ApproveException", {}, 3), ("IssueRefund", {}, 1)):
            with self.subTest(expected=expected):
                inputs = self.root / "input.json"
                inputs.write_bytes(pc.canonical(values))
                result = subprocess.run([sys.executable, "-I", "-S", str(self.out / "scripts/check.py"),
                    "--task", task, "--input", str(inputs)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_cli_failure_is_json(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "domain_skill.py"), "build", str(self.root / "missing"),
            "--out", str(self.out)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "ERROR")
        self.assertNotIn("Traceback", result.stderr)

    def test_compare_unchanged(self):
        self.compile()
        self.assertEqual(ds.compare(self.out, self.out)["status"], "UNCHANGED")

    def test_compare_unrelated_goals_rejected(self):
        self.compile()
        self.contract["root_goal_id"] = "goal-other"
        self.save_contract()
        other = self.root / "other" / demo.NAME
        ds.build(self.workspace, other)
        with self.assertRaises(pc.DomainError):
            ds.compare(self.out, other)

    def test_end_to_end_update_and_baselines(self):
        summary = demo.run(self.root / "demo")
        self.assertEqual(summary["fixture_checks_passed"], 26)
        self.assertEqual(summary["agent_comparison"], "NOT_RUN")
        self.assertEqual(summary["human_review"], "PENDING")
        self.assertEqual(summary["promotion"], "NOT_PERFORMED")
        impact = json.loads((self.root / "demo/impact.json").read_text())
        self.assertIn("request-limit", impact["changed_contract_items"]["rules"])
        self.assertIn("IssueRefund", impact["rerun_tasks"])
        v1 = json.loads((self.root / "demo/v1/evaluation.json").read_text())
        v2 = json.loads((self.root / "demo/v2/evaluation.json").read_text())
        self.assertEqual(next(c for c in v1["cases"] if c["id"] == "exact-remainder")["status"], "CHECKS_PASS")
        self.assertEqual(next(c for c in v2["cases"] if c["id"] == "exact-remainder")["status"], "CHECKS_FAIL")
        for arm in ("raw", "markdown", "skill"):
            path = self.root / "demo/v1/benchmark" / arm
            self.assertNotIn("expected_status", (path / "tasks.json").read_text())
            self.assertFalse((path / "oracle.json").exists())
            self.assertEqual((path / "sources/requirements.md").read_bytes(), (self.source / "requirements.md").read_bytes())

    def test_fixture_state_transitions(self):
        result = demo.run_fixture(self.model, 1)
        self.assertEqual(result["passed"], result["total"])
        self.assertEqual(result["evaluation_kind"], "DETERMINISTIC_FIXTURE_NOT_AGENT_BENCHMARK")

    def test_scorer_all_predictions(self):
        oracle = demo.cases(1)
        predictions = [{"id": c["id"], "status": c["expected_status"], "refunded_minor_after": c["expected_refunded_minor_after"]} for c in oracle]
        result = scorer.score(oracle, predictions)
        self.assertEqual(result["passed"], 13)
        self.assertEqual(result["kind"], "SUPPLIED_PREDICTIONS_NOT_VERIFIED_AGENT_RUN")

    def test_scorer_missing_predictions_rejected(self):
        with self.assertRaises(pc.DomainError):
            scorer.score(demo.cases(1), [])

    def test_scorer_duplicate_predictions_rejected(self):
        prediction = {"id": "valid-small", "status": "CHECKS_PASS", "refunded_minor_after": 8500}
        with self.assertRaises(pc.DomainError):
            scorer.score(demo.cases(1), [prediction, prediction])

    def test_scorer_wrong_final_state_is_failure(self):
        oracle = demo.cases(1)[:1]
        result = scorer.score(oracle, [{"id": oracle[0]["id"], "status": "CHECKS_PASS", "refunded_minor_after": 0}])
        self.assertEqual(result["passed"], 0)


if __name__ == "__main__":
    unittest.main()
