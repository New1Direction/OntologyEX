"""Ordinal evidence grades: cumulative, bound to the current claim, never numeric."""
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import domain_skill as ds
import evidence
import evidence_grade as eg
from policy_check import DomainError, canonical

spec = importlib.util.spec_from_file_location(
    "ontologyex_grade_payments_fixture", ROOT / "examples/payments-domain/demo.py")
demo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = demo
spec.loader.exec_module(demo)


def build(root: Path, name: str, statement: str | None = None) -> Path:
    """Payments fixture plus implementation evidence for remaining-balance."""
    source = ROOT / "examples/payments-domain/source"
    workspace = root / f"{name}-workspace"
    skill = root / name / demo.NAME
    ds.prepare(source, ["requirements.md", "payments.py"], workspace, demo.NAME, demo.GOAL)
    demo.populate(workspace)
    code = evidence.record(workspace, "payments.py", 25, 26, "E-remaining-code", "implementation")
    contract = json.loads((workspace / "60-contract.json").read_text())
    contract["evidence"].append(code)
    for rule in contract["rules"]:
        if rule["id"] == "remaining-balance":
            rule["evidence"].append("E-remaining-code")
            if statement:
                rule["statement"] = statement
    (workspace / "60-contract.json").write_bytes(canonical(contract))
    ds.build(workspace, skill, source)
    return skill


class EvidenceGradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.skill = build(cls.root, "v1")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def events(self, name):
        path = self.root / "events" / f"{name}.json"
        path.unlink(missing_ok=True)
        return path

    def rules(self, events=None):
        return {r["id"]: r for r in eg.grade(self.skill, events)["rules"]}

    def test_without_events_every_rule_is_g0(self):
        result = eg.grade(self.skill)
        self.assertEqual(result["distribution"], {"G0": 7, "G1": 0, "G2": 0, "G3": 0, "G4": 0})
        self.assertEqual(result["numeric_weights"], "NONE_ORDINAL_ONLY")
        self.assertFalse(result["events_authenticated"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["not_graded"], {"unknowns": 1, "conflicts": 0})

    def test_ladder_is_cumulative(self):
        events = self.events("ladder")
        # A passing check alone does not skip the missing review.
        eg.record(self.skill, events, "remaining-balance", "BEHAVIOR_CHECK_PASSED", "ci", "test_remainder")
        self.assertEqual(self.rules(events)["remaining-balance"]["grade"], "G0")
        eg.record(self.skill, events, "remaining-balance", "REVIEW_SUPPORTS", "reviewer", "")
        eg.record(self.skill, events, "positive-amount", "REVIEW_SUPPORTS", "reviewer", "")
        eg.record(self.skill, events, "positive-amount", "BEHAVIOR_CHECK_PASSED", "ci", "test_positive")
        rules = self.rules(events)
        self.assertEqual(rules["remaining-balance"]["grade"], "G3")
        self.assertEqual(rules["remaining-balance"]["evidence_kinds"], ["implementation", "requirement"])
        # One evidence kind stops at G1 whatever the checks say.
        self.assertEqual(rules["positive-amount"]["grade"], "G1")
        self.assertIn("only one evidence kind", rules["positive-amount"]["held_by"][0])

    def test_rejection_and_failure_hold_the_grade(self):
        events = self.events("negative")
        eg.record(self.skill, events, "remaining-balance", "REVIEW_SUPPORTS", "a", "")
        eg.record(self.skill, events, "remaining-balance", "BEHAVIOR_CHECK_PASSED", "ci", "t1")
        eg.record(self.skill, events, "remaining-balance", "BEHAVIOR_CHECK_FAILED", "ci", "t2")
        self.assertEqual(self.rules(events)["remaining-balance"]["grade"], "G2")
        eg.record(self.skill, events, "remaining-balance", "REVIEW_REJECTS", "b", "")
        self.assertEqual(self.rules(events)["remaining-balance"]["grade"], "G0")

    def test_g4_is_never_conferred(self):
        self.assertNotIn("FRESH_AGENT", " ".join(eg.EVENTS))
        events = self.events("top")
        for event, ref in (("REVIEW_SUPPORTS", ""), ("BEHAVIOR_CHECK_PASSED", "t")):
            eg.record(self.skill, events, "remaining-balance", event, "x", ref)
        rule = self.rules(events)["remaining-balance"]
        self.assertEqual(rule["grade"], "G3")
        self.assertIn("no tool here records one", rule["held_by"][-1])
        self.assertEqual(eg.grade(self.skill, events)["g4_producer"], "NONE_IN_THIS_REPOSITORY")

    def test_changed_claim_makes_events_stale(self):
        events = self.events("stale")
        eg.record(self.skill, events, "remaining-balance", "REVIEW_SUPPORTS", "a", "")
        changed = build(self.root, "v2", statement="The amount must not exceed the remainder.")
        result = eg.grade(changed, events)
        self.assertEqual(len(result["stale_events"]), 1)
        self.assertEqual({r["id"]: r for r in result["rules"]}["remaining-balance"]["grade"], "G0")

    def test_record_is_idempotent_and_append_only(self):
        events = self.events("append")
        first = eg.record(self.skill, events, "captured-only", "REVIEW_SUPPORTS", "a", "")
        again = eg.record(self.skill, events, "captured-only", "REVIEW_SUPPORTS", "a", "")
        self.assertEqual((first["status"], again["status"]), ("RECORDED", "UNCHANGED"))
        self.assertEqual(len(json.loads(events.read_text())["events"]), 1)

    def test_record_refuses_bad_input(self):
        events = self.events("bad")
        with self.assertRaises(DomainError):
            eg.record(self.skill, self.skill / "events.json", "captured-only", "REVIEW_SUPPORTS", "a", "")
        with self.assertRaises(DomainError):
            eg.record(self.skill, events, "absent-rule", "REVIEW_SUPPORTS", "a", "")
        with self.assertRaises(DomainError):
            eg.record(self.skill, events, "captured-only", "BEHAVIOR_CHECK_PASSED", "a", "")
        with self.assertRaises(DomainError):
            eg.record(self.skill, events, "captured-only", "APPROVED", "a", "")
        with self.assertRaises(DomainError):
            eg.grade(self.skill, self.root / "events" / "missing.json")

    def test_malformed_events_file_is_refused(self):
        events = self.events("malformed")
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text(json.dumps({"schema_version": 1, "kind": eg.EVENT_KIND, "events": [
            {"rule": "captured-only", "claim_sha256": "0" * 64, "event": "REVIEW_SUPPORTS",
             "by": "a", "ref": "", "weight": 1}]}))
        with self.assertRaises(DomainError):
            eg.grade(self.skill, events)

    def test_tampered_bundle_is_refused(self):
        forged = self.root / "forged"
        shutil.copytree(self.skill, forged)
        with (forged / "model.json").open("a") as handle:
            handle.write("\n")
        with self.assertRaises(DomainError):
            eg.grade(forged)


if __name__ == "__main__":
    unittest.main()
