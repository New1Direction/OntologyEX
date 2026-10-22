"""Provenance invariants plus actual compilation, tracked attempts and installed CLI."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import domain_skill as ds
import evidence
import install
import onboard
import policy_check as pc
import provenance as pv

spec = importlib.util.spec_from_file_location("provenance_stress_fixture", ROOT / "examples/provenance-stress/demo.py")
stress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stress)


class ProvenanceGroupingTests(unittest.TestCase):
    def setUp(self):
        self.model, self.attrs = stress.fixture(0, 5)

    def test_stress_matrix_and_behavioral_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = stress.run(Path(tmp).resolve() / "result")
            self.assertEqual(result["paired_variants"], 60)
            self.assertEqual(result["diagnostic_comparisons_passed"], 180)
            self.assertEqual(result["distinct_origin_controls"], 20)
            self.assertEqual(result["fresh_agent_evaluation"], "NOT_RUN")
            self.assertEqual(result["model_calls"], 0)

    def test_stress_output_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(pc.DomainError):
            stress.run(Path(tmp).resolve())

    def test_copies_share_one_declared_atom(self):
        result = pv.analyze(self.model, self.attrs)
        rule = next(r for r in result["items"] if r["kind"] == "rules")
        self.assertEqual(rule["evidence_records"], 5)
        self.assertEqual(rule["evidence_atoms"], 1)
        self.assertEqual(len(rule["declared_origin_ids"]), 1)
        self.assertEqual(len(result["records"]), 6)

    def test_equal_wording_different_origins_remains_distinct(self):
        model, attrs = stress.fixture(0, 5, distinct=True)
        result = pv.analyze(model, attrs)
        rule = next(r for r in result["items"] if r["kind"] == "rules")
        self.assertEqual(rule["content_groups"], 1)
        self.assertEqual(rule["evidence_atoms"], 5)
        self.assertEqual(len(rule["declared_independence_groups"]), 5)
        self.assertIn("SAME_WORDING_DISTINCT_DECLARED_ORIGINS_RETAINED", {w["code"] for w in result["warnings"]})

    def test_correlated_origins_share_declared_group(self):
        model, attrs = stress.fixture(0, 5, distinct=True)
        for attr in attrs["attributions"]:
            attr["independence_group"] = "shared-policy-family"
        result = pv.analyze(model, attrs)["summary"]
        self.assertEqual(len(result["declared_origin_ids"]), 6)
        self.assertEqual(result["declared_independence_groups"], ["shared-policy-family"])

    def test_same_origin_different_quotes_not_discarded(self):
        for row in self.attrs["attributions"]:
            row["origin_id"] = "same-origin"
            row["independence_group"] = "same-group"
        result = pv.analyze(self.model, self.attrs)
        self.assertEqual(result["summary"]["evidence_atoms"], 2)
        conflict = next(i for i in result["items"] if i["kind"] == "conflicts")
        self.assertEqual(conflict["status"], "CONFLICT_RECORDED")
        self.assertEqual(len(conflict["evidence_ids"]), 6)

    def test_unknown_lineage_not_independent(self):
        result = pv.analyze(self.model)["summary"]
        self.assertEqual(result["declared_origin_ids"], [])
        self.assertEqual(len(result["unattributed_evidence_ids"]), 6)
        self.assertIsNone(result["independent_support_count"])
        self.assertFalse(result["independence_verified"])

    def test_unattributed_identical_file_bytes_only_group(self):
        result = pv.analyze(self.model)
        self.assertEqual(result["summary"]["evidence_atoms"], 2)
        self.assertTrue(all(a["grouping_basis"] == "IDENTICAL_FILE_BYTES_AND_QUOTE_ONLY" for a in result["atoms"]))

    def test_unattributed_different_file_bytes_not_same_origin(self):
        item = self.model["evidence"][0]
        new_hash = pc.digest((item["quote"] + "\nDifferent surrounding context\n").encode())
        item["sha256"] = new_hash
        self.model["source_files"][item["path"]]["sha256"] = new_hash
        self.assertEqual(pv.analyze(self.model)["summary"]["evidence_atoms"], 3)

    def test_partial_attribution_stays_partly_unknown(self):
        self.attrs["attributions"] = self.attrs["attributions"][:1]
        result = pv.analyze(self.model, self.attrs)
        self.assertEqual(len(result["summary"]["unattributed_evidence_ids"]), 5)
        self.assertEqual(result["items"][-1]["status"], "LINEAGE_UNKNOWN")

    def test_empty_attribution_means_unknown(self):
        self.assertEqual(pv.analyze(self.model, {"schema_version": 1, "attributions": []}), pv.analyze(self.model))

    def test_evidence_kind_separates_identical_wording(self):
        self.model["evidence"][0]["kind"] = "implementation"
        self.attrs["attributions"][0]["evidence_sha256"] = pc.digest(pc.canonical(self.model["evidence"][0]))
        self.assertEqual(pv.analyze(self.model, self.attrs)["summary"]["evidence_atoms"], 3)

    def test_no_whitespace_or_semantic_normalization(self):
        item = self.model["evidence"][0]
        item["quote"] += " "
        self.attrs["attributions"][0]["evidence_sha256"] = pc.digest(pc.canonical(item))
        result = pv.analyze(self.model, self.attrs)
        self.assertEqual(result["summary"]["content_groups"], 3)
        self.assertEqual(len(result["summary"]["declared_origin_ids"]), 2)

    def test_same_rule_reference_not_extra_record(self):
        self.model["rules"][0]["evidence"] *= 3
        result = pv.analyze(self.model, self.attrs)
        rule = next(r for r in result["items"] if r["kind"] == "rules")
        self.assertEqual(rule["evidence_records"], 5)
        self.assertEqual(rule["repeated_reference_count"], 10)

    def test_input_order_does_not_change_audit(self):
        expected = pv.analyze(self.model, self.attrs)
        self.model["evidence"].reverse()
        self.model["rules"][0]["evidence"].reverse()
        self.model["conflicts"][0]["evidence"].reverse()
        self.attrs["attributions"].reverse()
        self.assertEqual(expected, pv.analyze(self.model, self.attrs))

    def test_source_id_identifies_path_and_version_not_origin(self):
        sha = "a" * 64
        self.assertEqual(pv.source_id("a.md", sha), pv.source_id("a.md", sha))
        self.assertNotEqual(pv.source_id("a.md", sha), pv.source_id("b.md", sha))
        self.assertNotEqual(pv.source_id("a.md", sha), pv.source_id("a.md", "b" * 64))

    def test_unknown_blocker_without_evidence_stays_unknown(self):
        self.model["unknowns"] = [{"id": "missing-owner", "task": "Submit", "statement": "Unknown.", "evidence": []}]
        result = pv.analyze(self.model, self.attrs)
        unknown = next(i for i in result["items"] if i["kind"] == "unknowns")
        self.assertEqual(unknown["status"], "UNKNOWN_RECORDED")
        self.assertIsNone(unknown["independent_support_count"])

    def test_task_view_does_not_drop_conflict(self):
        result = pv.analyze(self.model, self.attrs)
        view = pv.task_view(result, "Submit", {e["id"] for e in self.model["evidence"]})
        self.assertEqual(view["summary"], result["summary"])
        self.assertEqual(view["items"], result["items"])
        other = pv.task_view(result, "Other", set())
        self.assertEqual(other["records"], [])
        self.assertEqual(other["atoms"], [])
        self.assertEqual(other["items"], [])

    def test_audit_never_claims_verified_support_or_permission(self):
        result = pv.analyze(self.model, self.attrs)
        for item in (result["summary"], *result["items"]):
            self.assertIsNone(item["independent_support_count"])
            self.assertIs(item["independence_verified"], False)
        self.assertIs(result["execution_authorized"], False)
        self.assertIn("NO_CONFIDENCE", result["decision_policy"])

    def test_invalid_attribution_shapes_fail(self):
        candidates = [[], {}, {"schema_version": True, "attributions": []},
                      {"schema_version": 2, "attributions": []}, {"schema_version": 1, "attributions": None},
                      {**self.attrs, "confidence": 0.99}]
        for doc in candidates:
            with self.subTest(doc=doc), self.assertRaises(pc.DomainError):
                pv.analyze(self.model, doc)

    def test_invalid_attribution_fields_fail(self):
        for key, value in (("evidence_id", "missing"), ("evidence_id", []), ("origin_id", ""),
                           ("independence_group", None), ("basis", ""), ("basis", "x" * 4097),
                           ("evidence_sha256", "0" * 64)):
            doc = copy.deepcopy(self.attrs)
            doc["attributions"][0][key] = value
            with self.subTest(field=key), self.assertRaises(pc.DomainError):
                pv.analyze(self.model, doc)

    def test_duplicate_attributions_fail(self):
        self.attrs["attributions"].append(self.attrs["attributions"][0])
        with self.assertRaises(pc.DomainError): pv.analyze(self.model, self.attrs)

    def test_attribution_budget_fails(self):
        self.attrs["attributions"] = [self.attrs["attributions"][0]] * 257
        with self.assertRaises(pc.DomainError): pv.analyze(self.model, self.attrs)

    def test_same_origin_inconsistent_group_fails(self):
        self.attrs["attributions"][1]["independence_group"] = "contradictory-group"
        with self.assertRaisesRegex(pc.DomainError, "inconsistent"):
            pv.analyze(self.model, self.attrs)

    def test_complete_evidence_fingerprint_required(self):
        self.model["evidence"][0]["end_line"] = 2
        with self.assertRaisesRegex(pc.DomainError, "stale evidence attribution"):
            pv.analyze(self.model, self.attrs)

    def test_duplicate_evidence_ids_fail(self):
        self.model["evidence"].append(self.model["evidence"][0])
        with self.assertRaises(pc.DomainError): pv.analyze(self.model, self.attrs)

    def test_dangling_claim_evidence_fails(self):
        self.model["rules"][0]["evidence"].append("missing")
        with self.assertRaises(pc.DomainError): pv.analyze(self.model, self.attrs)

    def test_invalid_source_identity_fails(self):
        self.model["evidence"][0]["sha256"] = "0" * 64
        with self.assertRaises(pc.DomainError): pv.analyze(self.model, self.attrs)

    def test_unsafe_path_fails(self):
        with self.assertRaises(pc.DomainError): pv.source_id("../other.md", "0" * 64)


class ProvenanceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("provenance_payments_seed", ROOT / "examples/payments-domain/demo.py")
        cls.demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.demo)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = ROOT / "examples/payments-domain/source"
        self.workspace = self.root / "workspace"
        ds.prepare(self.source, ["requirements.md", "payments.py"], self.workspace, self.demo.NAME, self.demo.GOAL)
        self.demo.populate(self.workspace)
        self.contract = pc.parse_json((self.workspace / "60-contract.json").read_bytes())
        self.eid = self.contract["evidence"][0]["id"]
        self.out = self.root / self.demo.NAME

    def attribute(self, **kwargs):
        return pv.attribute(self.workspace, self.eid, kwargs.get("origin", "policy-v1"),
                            kwargs.get("group", "policy-family"), kwargs.get("basis", "Selected fixture source history; author declaration."))

    def build(self):
        ds.build(self.workspace, self.out, self.source)
        return pc.parse_json((self.out / "model.json").read_bytes())

    def test_v1_contract_needs_no_sidecar(self):
        before = (self.workspace / "60-contract.json").read_bytes()
        model = self.build()
        self.assertEqual(before, (self.workspace / "60-contract.json").read_bytes())
        self.assertIsNone(model["provenance"]["summary"]["independent_support_count"])
        self.assertTrue((self.out / "references/provenance.json").is_file())
        self.assertIn("provenance_sha256", model["generator"])

    def test_attribute_only_changes_sidecar_and_is_idempotent(self):
        before = pc.file_inventory(self.workspace)
        result = self.attribute()
        self.assertEqual(result["status"], "ATTRIBUTION_RECORDED_NOT_VERIFIED")
        self.assertEqual(self.attribute()["status"], "UNCHANGED")
        after = pc.file_inventory(self.workspace)
        self.assertEqual(before, {p: h for p, h in after.items() if p != pv.SIDECAR})
        self.assertEqual(self.build()["provenance"]["summary"]["declared_origin_ids"], ["policy-v1"])

    def test_attribute_does_not_replace_different_assignment(self):
        self.attribute()
        before = (self.workspace / pv.SIDECAR).read_bytes()
        with self.assertRaises(pc.DomainError): self.attribute(origin="other")
        self.assertEqual(before, (self.workspace / pv.SIDECAR).read_bytes())

    def test_attribute_lock_serializes_with_evidence_helper(self):
        (self.workspace / ".evidence-lock").mkdir()
        with self.assertRaises(pc.DomainError): self.attribute()
        self.assertFalse((self.workspace / pv.SIDECAR).exists())

    def test_unknown_evidence_id_does_not_create_sidecar(self):
        self.eid = "missing"
        with self.assertRaises(pc.DomainError): self.attribute()
        self.assertFalse((self.workspace / pv.SIDECAR).exists())
        self.assertFalse((self.workspace / ".evidence-lock").exists())

    def test_stale_attribution_blocks_compile(self):
        self.attribute()
        self.contract["evidence"][0]["kind"] = ("test" if self.contract["evidence"][0]["kind"] != "test" else "documentation")
        (self.workspace / "60-contract.json").write_bytes(pc.canonical(self.contract))
        with self.assertRaisesRegex(pc.DomainError, "stale evidence attribution"):
            self.build()
        self.assertFalse(self.out.exists())

    def test_false_quote_blocks_attribution(self):
        self.contract["evidence"][0]["quote"] = "invented"
        (self.workspace / "60-contract.json").write_bytes(pc.canonical(self.contract))
        with self.assertRaises(pc.DomainError): self.attribute()
        self.assertFalse((self.workspace / pv.SIDECAR).exists())

    def test_malformed_present_sidecar_fails_not_ignored(self):
        for raw in (b"null", b"[]", b"{", b'{"schema_version":1,"attributions":[],"schema_version":1}'):
            (self.workspace / pv.SIDECAR).write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(pc.DomainError): self.build()
            self.assertFalse(self.out.exists())

    def test_broken_symlink_sidecar_fails_not_ignored(self):
        (self.workspace / pv.SIDECAR).symlink_to(self.root / "missing.json")
        with self.assertRaises(pc.DomainError): self.build()

    def test_oversized_sidecar_fails(self):
        (self.workspace / pv.SIDECAR).write_bytes(b" " * (pc.MAX_TOTAL + 1))
        with self.assertRaises(pc.DomainError): self.build()

    def test_provenance_file_in_integrity_manifest(self):
        self.attribute()
        self.build()
        (self.out / "references/provenance.json").write_text("{}")
        with self.assertRaises(pc.DomainError): pc.verify_bundle(self.out)

    def test_provenance_only_change_triggers_review_and_rerun(self):
        self.attribute()
        self.build()
        doc = pv.read_attributions(self.workspace)
        doc["attributions"][0]["basis"] = "Corrected provenance explanation; semantic review pending."
        (self.workspace / pv.SIDECAR).write_bytes(pc.canonical(doc))
        other = self.root / "v2" / self.demo.NAME
        ds.build(self.workspace, other, self.source)
        impact = ds.compare(self.out, other)
        self.assertTrue(impact["provenance_changed"])
        self.assertFalse(impact["layers_changed"])
        self.assertEqual(impact["changed_sources"], [])
        self.assertEqual(impact["status"], "REVIEW_REQUIRED")
        self.assertTrue(impact["rerun_tasks"])

    def test_basis_html_is_escaped(self):
        self.attribute(basis='<script>alert("untrusted")</script>')
        self.build()
        page = (self.out / "report.html").read_text()
        self.assertNotIn('<script>alert', page)
        self.assertIn('&lt;script&gt;alert', page)
        self.assertIn("Unknown", page)
        self.assertIn("Evidence provenance", page)

    def test_tracked_attempt_retains_sidecar_and_handoff(self):
        session = self.root / "session"
        onboard.start(self.source, ["requirements.md", "payments.py"], session, self.demo.NAME, self.demo.GOAL, True)
        self.demo.populate(session / "workspace")
        pv.attribute(session / "workspace", self.eid, "policy-v1", "policy-family", "Declared fixture origin.")
        result = onboard.attempt(session)
        self.assertEqual(result["status"], "CANDIDATE_READY_FOR_REVIEW")
        self.assertTrue((session / "attempts/01/input" / pv.SIDECAR).is_file())
        out = self.root / "handoff"
        onboard.handoff(session, "IssueRefund", out)
        context = pc.parse_json((out / "context.json").read_bytes())
        self.assertIn("provenance", context)
        self.assertEqual({e["id"] for e in context["evidence"]},
                         {r["evidence_id"] for r in context["provenance"]["records"]})
        self.assertFalse(context["execution_authorized"])

    def test_bad_sidecar_uses_attempt_and_can_be_repaired(self):
        session = self.root / "session"
        onboard.start(self.source, ["requirements.md", "payments.py"], session, self.demo.NAME, self.demo.GOAL, True)
        self.demo.populate(session / "workspace")
        (session / "workspace" / pv.SIDECAR).write_text("{")
        self.assertEqual(onboard.attempt(session)["status"], "REPAIR_REQUIRED")
        (session / "workspace" / pv.SIDECAR).write_text('{"schema_version":1,"attributions":[]}')
        result = onboard.attempt(session)
        self.assertEqual(result["attempts_used"], 2)
        self.assertEqual(result["status"], "CANDIDATE_READY_FOR_REVIEW")
        self.assertEqual((session / "attempts/01/input" / pv.SIDECAR).read_text(), "{")

    def test_installed_provenance_cli_from_unrelated_directory(self):
        project = self.root / "project with spaces"
        project.mkdir()
        installed = Path(install.install(project)["path"])
        command = [sys.executable, "-B", str(installed / "scripts/provenance.py")]
        result = subprocess.run([*command, "attribute", str(self.workspace), "--evidence", self.eid,
                                 "--origin", "policy-v1", "--group", "policy-family", "--basis", "Declared test origin."],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        inspection = subprocess.run([*command, "inspect", str(self.workspace)], cwd=self.root,
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(inspection.returncode, 0, inspection.stderr + inspection.stdout)
        self.assertEqual(json.loads(inspection.stdout)["summary"]["declared_origin_ids"], ["policy-v1"])
        self.assertTrue((installed / "references/provenance.md").is_file())

    def test_cli_invalid_sidecar_returns_structured_error(self):
        (self.workspace / pv.SIDECAR).write_text("[]")
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "provenance.py"), "inspect", str(self.workspace)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "ERROR")
        self.assertNotIn("Traceback", result.stderr)

    def test_standalone_checker_remains_dependency_free(self):
        self.attribute()
        self.build()
        self.assertEqual((self.out / "scripts/check.py").read_bytes(), (SCRIPTS / "policy_check.py").read_bytes())
        values = {"amount_minor": 500, "captured_minor": 10000, "refunded_minor": 8000,
                  "payment_status": "captured", "requester_authorized": True}
        inputs = self.root / "inputs.json"
        inputs.write_bytes(pc.canonical(values))
        result = subprocess.run([sys.executable, "-I", "-S", str(self.out / "scripts/check.py"),
                                 "--task", "IssueRefund", "--input", str(inputs)],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(json.loads(result.stdout)["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
