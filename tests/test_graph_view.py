"""Offline regression tests for OntologyEX's typed, provenance-linked graph view."""
import copy
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import domain_skill as ds
import graph_view as gv
from policy_check import DomainError

spec = importlib.util.spec_from_file_location(
    "ontologyex_graph_view_payments_fixture",
    ROOT / "examples/payments-domain/demo.py",
)
demo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = demo
spec.loader.exec_module(demo)


def sample(scope="goal-sample"):
    evidence = [
        {"id": "E-service", "path": "code.py", "sha256": "a" * 64,
         "start_line": 1, "end_line": 1, "kind": "implementation"},
        {"id": "E-task", "path": "requirements.md", "sha256": "b" * 64,
         "start_line": 2, "end_line": 4, "kind": "requirement"},
    ]
    return {"root_goal_id": scope, "name": "sample-domain", "evidence": evidence,
            "layers": {
                "10-upper.yaml": {"anchors": [{"id": "Intangible", "iri": "https://schema.org/Intangible"}]},
                "20-domain.yaml": {
                    "classes": [
                        {"id": "Service", "upper": "Intangible", "source": "E-service",
                         "implements": ["Versioned"]},
                        {"id": "API", "upper": "Intangible", "source": "E-service"},
                        {"id": "Agent", "upper": "Intangible", "source": "E-service"},
                    ],
                    "interfaces": [{"id": "Versioned", "applies_to": ["Service"],
                                    "source": "E-service"}],
                    "relations": [{"id": "calls", "domain": "Service", "range": "API",
                                   "source": "E-service"}],
                },
                "30-task.yaml": {"tasks": [
                    {"id": "Observe", "inputs": ["API"], "outputs": ["Service"],
                     "actor_roles": ["Agent"], "decomposes_to": [], "source": "E-task"}]},
                "40-application.yaml": {"concepts": [
                    {"id": "indexer", "binds": "Service", "used_by_tasks": ["Observe"],
                     "source": "E-service"}]},
            }}


BUNDLE = "f" * 64


class ProjectionUnitTests(unittest.TestCase):
    def test_nodes_edges_and_evidence(self):
        graph = gv.from_model(sample(), BUNDLE)
        kinds = {node["kind"] for node in graph["nodes"]}
        self.assertEqual(kinds, {
            "evidence", "upper_anchor", "domain_class", "interface", "relation",
            "task", "application_binding"})
        relations = {edge["relation"] for edge in graph["edges"]}
        self.assertTrue({"ANCHORED_TO", "IMPLEMENTS", "APPLIES_TO", "DOMAIN",
                         "RANGE", "CONSUMES", "PRODUCES", "ACTOR_ROLE",
                         "BINDS_TO", "USED_BY_TASK", "SUPPORTED_BY"} <= relations)
        self.assertEqual(graph["unresolved"], [])
        self.assertFalse(graph["execution_authorized"])
        self.assertEqual(graph["semantic_entailment"], "NOT_EVALUATED")
        self.assertEqual(graph["runtime_observations"], "NOT_INCLUDED")
        evidence = next(n for n in graph["nodes"] if n["local_id"] == "E-task")
        self.assertEqual(evidence["path"], "requirements.md")
        self.assertEqual(evidence["start_line"], 2)
        self.assertNotIn("quote", evidence)

    def test_order_does_not_affect_graph(self):
        model = sample()
        changed = copy.deepcopy(model)
        changed["evidence"].reverse()
        changed["layers"]["20-domain.yaml"]["classes"].reverse()
        changed["layers"]["40-application.yaml"]["concepts"].reverse()
        self.assertEqual(gv.from_model(model, BUNDLE), gv.from_model(changed, BUNDLE))

    def test_identity_survives_versions(self):
        a = gv.from_model(sample(), "a" * 64)
        newer = sample()
        newer["layers"]["20-domain.yaml"]["classes"][0]["definition"] = "Changed"
        b = gv.from_model(newer, "b" * 64)
        self.assertEqual([node["id"] for node in a["nodes"]],
                         [node["id"] for node in b["nodes"]])
        self.assertNotEqual(a["bundle_id"], b["bundle_id"])

    def test_evidence_and_nodes_are_scope_isolated(self):
        a = gv.from_model(sample("goal-first"), BUNDLE)
        b = gv.from_model(sample("goal-second"), BUNDLE)
        self.assertFalse(set(node["id"] for node in a["nodes"]) &
                         set(node["id"] for node in b["nodes"]))

    def test_unknown_role_is_not_fabricated(self):
        model = sample()
        model["layers"]["30-task.yaml"]["tasks"][0]["actor_roles"] = ["UnspecifiedRole"]
        graph = gv.from_model(model, BUNDLE)
        self.assertEqual(len(graph["unresolved"]), 1)
        self.assertEqual(graph["unresolved"][0]["relation"], "ACTOR_ROLE")
        self.assertNotIn(graph["unresolved"][0]["missing_target"],
                         {node["id"] for node in graph["nodes"]})

    def test_missing_evidence_is_an_explicit_gap(self):
        model = sample()
        model["layers"]["40-application.yaml"]["concepts"][0]["source"] = "E-missing"
        graph = gv.from_model(model, BUNDLE)
        self.assertEqual(len(graph["unresolved"]), 1)
        self.assertEqual(graph["unresolved"][0]["relation"], "SUPPORTED_BY")

    def test_duplicate_identity_rejected(self):
        model = sample()
        model["layers"]["20-domain.yaml"]["classes"].append(
            copy.deepcopy(model["layers"]["20-domain.yaml"]["classes"][0]))
        with self.assertRaises(DomainError):
            gv.from_model(model, BUNDLE)

    def test_invalid_target_rejected(self):
        model = sample()
        model["layers"]["20-domain.yaml"]["relations"][0]["range"] = "../secret"
        with self.assertRaises(DomainError):
            gv.from_model(model, BUNDLE)

    def test_compose_preserves_distinct_scopes(self):
        g1 = gv.from_model(sample("goal-one"), "a" * 64)
        g2 = gv.from_model(sample("goal-two"), "b" * 64)
        with patch.object(gv, "project", side_effect=[g2, g1]):
            combined = gv.compose([Path("second"), Path("first")])
        self.assertEqual([v["root_goal_id"] for v in combined["versions"]],
                         ["goal-one", "goal-two"])
        self.assertEqual(len(combined["nodes"]),
                         len(g1["nodes"]) + len(g2["nodes"]))
        self.assertEqual(combined["cross_project_dependencies"], "NOT_INFERRED")
        self.assertFalse(combined["execution_authorized"])

    def test_compose_rejects_two_versions_same_scope(self):
        g1 = gv.from_model(sample("goal-one"), "a" * 64)
        g2 = gv.from_model(sample("goal-one"), "b" * 64)
        with patch.object(gv, "project", side_effect=[g1, g2]):
            with self.assertRaises(DomainError):
                gv.compose([Path("one"), Path("two")])

    def test_diff_reports_changed_nodes_but_not_approval(self):
        old = gv.from_model(sample(), "a" * 64)
        updated = sample()
        updated["layers"]["20-domain.yaml"]["classes"][0]["definition"] = "Changed"
        new = gv.from_model(updated, "b" * 64)
        with patch.object(gv, "project", side_effect=[old, new]):
            result = gv.diff(Path("old"), Path("new"))
        self.assertEqual(result["nodes"]["added"], [])
        self.assertEqual(result["nodes"]["removed"], [])
        # The graph intentionally doesn't carry free-text definitions: a semantic
        # text change is caught by source/bundle comparison, NOT graph node delta.
        self.assertEqual(result["nodes"]["modified"], [])
        self.assertNotEqual(result["from_bundle"], result["to_bundle"])
        self.assertEqual(result["impact_completeness"], "NOT_ESTABLISHED")
        self.assertEqual(result["promotion"], "NOT_PERFORMED")

    def test_change_in_evidence_reference_changes_edges(self):
        old_model = sample()
        new_model = sample()
        new_model["layers"]["20-domain.yaml"]["classes"][0]["source"] = "E-task"
        old = gv.from_model(old_model, "a" * 64)
        new = gv.from_model(new_model, "b" * 64)
        with patch.object(gv, "project", side_effect=[old, new]):
            result = gv.diff(Path("old"), Path("new"))
        self.assertEqual(len(result["edges"]["added"]), 1)
        self.assertEqual(len(result["edges"]["removed"]), 1)

    def test_unrelated_diff_is_refused(self):
        a = gv.from_model(sample("goal-one"), "a" * 64)
        b = gv.from_model(sample("goal-two"), "b" * 64)
        with patch.object(gv, "project", side_effect=[a, b]):
            with self.assertRaises(DomainError):
                gv.diff(Path("one"), Path("two"))


class VerifiedBundleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        source = ROOT / "examples/payments-domain/source"
        workspace = cls.root / "workspace"
        cls.skill = cls.root / demo.NAME
        ds.prepare(source, ["requirements.md", "payments.py"], workspace, demo.NAME, demo.GOAL)
        demo.populate(workspace)
        ds.build(workspace, cls.skill, source)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_compiled_skill(self):
        projected = gv.project(self.skill)
        self.assertEqual(projected["review_status"], "UNREVIEWED_CANDIDATE")
        self.assertEqual(projected["unresolved"], [])
        self.assertIn("BINDS_TO", {edge["relation"] for edge in projected["edges"]})
        self.assertTrue(any(n["kind"] == "evidence" for n in projected["nodes"]))

    def test_tampered_skill_is_refused(self):
        forged = self.root / "forged"
        shutil.copytree(self.skill, forged)
        with (forged / "model.json").open("a") as f:
            f.write("\n")
        with self.assertRaises(DomainError):
            gv.project(forged)


if __name__ == "__main__":
    unittest.main()
