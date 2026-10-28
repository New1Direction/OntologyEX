"""Tests for deterministic, bounded, declared-reference-only change planning."""
import copy
import importlib.util
import unittest
from pathlib import Path

FILE = Path(__file__).resolve().parents[1] / "ontology-extraction/scripts/change_plan.py"
spec = importlib.util.spec_from_file_location("ontologyex_change_plan", FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def model():
    return {"layers": {
        "10-upper.yaml": {"anchors": [{"id": "Object", "iri": "urn:object"}]},
        "20-domain.yaml": {
            "classes": [
                {"id": "Payment", "upper": "Object", "source": "E-payment"},
                {"id": "Refund", "upper": "Object", "source": "E-refund"},
                {"id": "Agent", "upper": "Object", "source": "E-agent"},
            ],
            "interfaces": [],
            "relations": [{"id": "refund_of", "domain": "Refund", "range": "Payment", "source": "E-link"}],
        },
        "30-task.yaml": {"tasks": [{"id": "IssueRefund", "inputs": ["Payment"], "outputs": ["Refund"],
                                    "actor_roles": ["Agent"], "source": "E-task"}]},
        "40-application.yaml": {"concepts": [
            {"id": "refund_service", "binds": "Refund", "used_by_tasks": ["IssueRefund"], "source": "E-impl"},
            {"id": "account_service", "binds": "Payment", "used_by_tasks": [], "source": "E-impl2"},
        ]},
    }}


class ChangePlanTests(unittest.TestCase):
    def test_unchanged(self):
        old = model()
        p = module.declared_plan(old, copy.deepcopy(old), [])
        self.assertEqual(p["change_items"], [])
        self.assertEqual(p["possible_impacted_tasks"], [])
        self.assertFalse(p["execution_authorized"])

    def test_changed_relation_crosses_layers(self):
        old = model()
        new = copy.deepcopy(old)
        new["layers"]["20-domain.yaml"]["relations"][0]["source"] = "E-updated-link"
        p = module.declared_plan(old, new, ["IssueRefund"])
        self.assertEqual(p["possible_impacted_classes"], ["Payment", "Refund"])
        self.assertEqual(p["possible_impacted_tasks"], ["IssueRefund"])
        self.assertEqual(p["possible_impacted_bindings"], ["account_service", "refund_service"])
        self.assertEqual(p["change_items"][0]["change"], "modified")
        self.assertEqual(p["change_items"][0]["evidence_after"], "E-updated-link")

    def test_deleted_class_retains_old_consumers(self):
        old = model()
        new = copy.deepcopy(old)
        new["layers"]["20-domain.yaml"]["classes"] = [x for x in new["layers"]["20-domain.yaml"]["classes"] if x["id"] != "Refund"]
        p = module.declared_plan(old, new, ["IssueRefund"])
        self.assertIn("refund_service", p["possible_impacted_bindings"])
        self.assertIn("IssueRefund", p["possible_impacted_tasks"])
        self.assertTrue(any(x["kind"] == "class" and x["change"] == "removed" for x in p["change_items"]))

    def test_modified_task_reaches_bindings(self):
        old = model()
        new = copy.deepcopy(old)
        new["layers"]["30-task.yaml"]["tasks"][0]["source"] = "E-new-task"
        p = module.declared_plan(old, new, [])
        self.assertEqual(p["possible_impacted_tasks"], ["IssueRefund"])
        self.assertEqual(p["possible_impacted_bindings"], ["refund_service"])

    def test_upper_anchor_reaches_every_class(self):
        old = model()
        new = copy.deepcopy(old)
        new["layers"]["10-upper.yaml"]["anchors"][0]["iri"] = "urn:replacement"
        p = module.declared_plan(old, new, [])
        self.assertEqual(p["possible_impacted_classes"], ["Agent", "Payment", "Refund"])
        self.assertEqual(p["possible_impacted_tasks"], ["IssueRefund"])

    def test_relation_order_does_not_affect_impact(self):
        old = model()
        old["layers"]["20-domain.yaml"]["relations"].append(
            {"id": "owned_by", "domain": "Payment", "range": "Agent", "source": "E-owner"})
        new = copy.deepcopy(old)
        new["layers"]["20-domain.yaml"]["relations"][0]["source"] = "E-change"
        a = module.declared_plan(old, new, [])
        old["layers"]["20-domain.yaml"]["relations"].reverse()
        new["layers"]["20-domain.yaml"]["relations"].reverse()
        b = module.declared_plan(old, new, [])
        self.assertEqual(a, b)
        self.assertNotIn("Agent", a["possible_impacted_classes"])
        self.assertIn("owned_by", a["possible_impacted_relations"])

    def test_only_conservative_reruns_when_source_changed(self):
        old = model()
        p = module.declared_plan(old, copy.deepcopy(old), ["IssueRefund"])
        self.assertEqual(p["possible_impacted_tasks"], [])
        self.assertEqual(p["conservative_rerun_tasks"], ["IssueRefund"])
        self.assertEqual(p["runtime_impact"], "UNKNOWN")
        self.assertEqual(p["semantic_conservativity"], "NOT_EVALUATED")

    def test_deterministic_order(self):
        old = model()
        new = copy.deepcopy(old)
        new["layers"]["20-domain.yaml"]["classes"] = list(reversed(new["layers"]["20-domain.yaml"]["classes"]))
        p = module.declared_plan(old, new, [])
        self.assertEqual(p["change_items"], [])


if __name__ == "__main__":
    unittest.main()
