"""N-Triples serialization boundary regression for the OntologyEX graph export."""
import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import domain_skill as ds
import rdf_export as rdf
from policy_check import DomainError

spec = importlib.util.spec_from_file_location(
    "ontologyex_rdf_payments_fixture", ROOT / "examples/payments-domain/demo.py")
demo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = demo
spec.loader.exec_module(demo)
spec = importlib.util.spec_from_file_location(
    "ontologyex_sparql_runner", ROOT / "examples/sparql/run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def example():
    return {
        "schema_version": 1,
        "kind": "COMPILED_DECLARATION_PROJECTION",
        "execution_authorized": False,
        "nodes": [
            {"id": "goal-1:domain_class:Service", "kind": "domain_class",
             "local_id": "Service"},
            {"id": "goal-1:evidence:E-code", "kind": "evidence",
             "local_id": "E-code", "path": 'src/"strange"\\file.py',
             "sha256": "a" * 64, "start_line": 1, "end_line": 3,
             "evidence_kind": "implementation"},
        ],
        "edges": [
            {"id": "e", "source": "goal-1:domain_class:Service",
             "relation": "SUPPORTED_BY", "target": "goal-1:evidence:E-code"},
        ],
        "unresolved": [],
    }


class RDFInteropTests(unittest.TestCase):
    def test_serializes_declared_types_and_provenance(self):
        data = rdf.serialize(example())
        self.assertIn("<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", data)
        self.assertIn("<http://www.w3.org/ns/prov#wasDerivedFrom>", data)
        self.assertIn('"Service"', data)
        self.assertIn('"1"^^<http://www.w3.org/2001/XMLSchema#integer>', data)
        self.assertNotIn("PRIVATE KEY", data)
        self.assertTrue(data.endswith("\n"))
        self.assertEqual(data.splitlines(), sorted(data.splitlines()))

    def test_deterministic_reordering(self):
        a = example()
        b = copy.deepcopy(a)
        b["nodes"].reverse()
        b["edges"].reverse()
        self.assertEqual(rdf.serialize(a), rdf.serialize(b))

    def test_literal_escapes_control_characters(self):
        value = 'a"\\\b\f\t\n\r😀'
        rendered = rdf.literal(value)
        self.assertEqual(rendered, '"a\\"\\\\\\u0008\\u000C\\t\\n\\r\\U0001F600"')
        self.assertNotIn("\n", rendered)

    def test_encoded_iri_does_not_assume_relative_url(self):
        encoded = rdf.iri("node", "goal-1:domain_class:Service")
        self.assertEqual(encoded,
                         "<urn:ontologyex:v1:node:goal-1%3Adomain_class%3AService>")

    def test_unknown_predicate_rejected(self):
        graph = example()
        graph["edges"][0]["relation"] = "EXECUTE_PRODUCTION"
        with self.assertRaises(DomainError):
            rdf.serialize(graph)

    def test_unknown_kind_rejected(self):
        graph = example()
        graph["nodes"][0]["kind"] = "admin_permission"
        with self.assertRaises(DomainError):
            rdf.serialize(graph)

    def test_dangling_reference_rejected(self):
        graph = example()
        graph["edges"][0]["target"] = "goal-1:evidence:NotExists"
        with self.assertRaises(DomainError):
            rdf.serialize(graph)

    def test_duplicate_node_rejected(self):
        graph = example()
        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
        with self.assertRaises(DomainError):
            rdf.serialize(graph)

    def test_unresolved_target_refuses_incomplete_export(self):
        graph = example()
        graph["unresolved"].append({
            "source": "goal-1:domain_class:Service", "relation": "DEPENDS_ON",
            "missing_target": "goal-1:domain_class:Database"})
        with self.assertRaises(DomainError):
            rdf.serialize(graph)

    def test_never_grants_execution(self):
        graph = example()
        graph["execution_authorized"] = True
        with self.assertRaises(DomainError):
            rdf.serialize(graph)

    def test_evidence_metadata_is_bounded(self):
        graph = example()
        graph["nodes"][1]["path"] = "x" * 9000
        with self.assertRaises(DomainError):
            rdf.serialize(graph)

    def test_ntriples_are_local_urns_not_links(self):
        data = rdf.serialize(example())
        self.assertIn("urn:ontologyex:v1:", data)
        self.assertNotIn("https://", data)
        self.assertNotIn("http://example.org", data)

    def test_composed_graph_accepted(self):
        graph = example()
        graph["kind"] = "CROSS_SCOPE_DECLARATION_VIEW"
        self.assertIn("urn:ontologyex:", rdf.serialize(graph))


def contract_graph():
    graph = example()
    graph["nodes"].append({"id": "goal-1:task:Deploy", "kind": "task", "local_id": "Deploy"})
    return graph


def contract_model():
    return {"root_goal_id": "goal-1",
            "rules": [{"id": "needs-approval", "task": "Deploy", "status": "inferred",
                       "statement": "A deploy must have one approval.",
                       "evidence": ["E-code"], "check": None}],
            "unknowns": [{"id": "who-approves", "task": "Deploy",
                          "statement": "The sources do not name the approver.", "evidence": []}],
            "conflicts": []}


class ContractExportTests(unittest.TestCase):
    def test_rules_and_unknowns_are_exported_with_evidence(self):
        data = rdf.serialize(rdf.with_contract(contract_graph(), contract_model()))
        rule = "<urn:ontologyex:v1:node:goal-1%3Arule%3Aneeds-approval>"
        self.assertIn(f"{rule} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
                      "<urn:ontologyex:v1:kind:rule> .", data)
        self.assertIn(f'{rule} <urn:ontologyex:v1:attr:status> "inferred" .', data)
        self.assertIn(f'{rule} <urn:ontologyex:v1:attr:executable> '
                      '"false"^^<http://www.w3.org/2001/XMLSchema#boolean> .', data)
        self.assertIn(f"{rule} <http://www.w3.org/ns/prov#wasDerivedFrom> "
                      "<urn:ontologyex:v1:node:goal-1%3Aevidence%3AE-code> .", data)
        self.assertIn("<urn:ontologyex:v1:node:goal-1%3Aunknown%3Awho-approves> "
                      "<urn:ontologyex:v1:edge:CONCERNS_TASK> "
                      "<urn:ontologyex:v1:node:goal-1%3Atask%3ADeploy> .", data)
        self.assertEqual(data.splitlines(), sorted(data.splitlines()))

    def test_executable_rule_is_marked(self):
        model = contract_model()
        model["rules"][0].update(status="observed", check={"left": "a", "op": "eq", "right": {"value": 1}})
        data = rdf.serialize(rdf.with_contract(contract_graph(), model))
        self.assertIn('"true"^^<http://www.w3.org/2001/XMLSchema#boolean>', data)

    def test_missing_task_or_evidence_fails_closed(self):
        for field, value in (("task", "Absent"), ("evidence", ["E-absent"])):
            model = contract_model()
            model["rules"][0][field] = value
            with self.assertRaises(DomainError):
                rdf.serialize(rdf.with_contract(contract_graph(), model))

    def test_contract_id_cannot_shadow_a_node(self):
        model = contract_model()
        model["unknowns"][0]["id"] = "needs-approval"
        model["unknowns"].append(copy.deepcopy(model["unknowns"][0]))
        with self.assertRaises(DomainError):
            rdf.with_contract(contract_graph(), model)

    def test_rule_without_status_rejected(self):
        model = contract_model()
        model["rules"][0]["status"] = "approved"
        with self.assertRaises(DomainError):
            rdf.serialize(rdf.with_contract(contract_graph(), model))


class CompiledSkillExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        source = ROOT / "examples/payments-domain/source"
        workspace = root / "workspace"
        cls.skill = root / demo.NAME
        ds.prepare(source, ["requirements.md", "payments.py"], workspace, demo.NAME, demo.GOAL)
        demo.populate(workspace)
        ds.build(workspace, cls.skill, source)
        cls.graph = root / "graph.nt"
        cls.graph.write_text(rdf.export([cls.skill]), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_compiled_contract_reaches_rdf(self):
        data = self.graph.read_text(encoding="utf-8")
        self.assertEqual(data.count("<urn:ontologyex:v1:kind:rule> ."), 7)
        self.assertEqual(data.count("<urn:ontologyex:v1:kind:unknown> ."), 1)

    @unittest.skipUnless(importlib.util.find_spec("sparq"), "optional sparq-rdf not installed")
    def test_example_queries_run_under_sparq(self):
        queries = ROOT / "examples/sparql"

        def rows(name):
            return runner.run(self.graph, queries / name)

        self.assertEqual(rows("review-blockers.rq"), [{
            "task": "ApproveException", "item": "exception-authority", "reason": "unknown",
            "statement": "The supplied requirements do not specify who can approve an exception."}])
        self.assertEqual(rows("rules-without-requirement.rq"), [])
        kinds = rows("rule-evidence-kinds.rq")
        self.assertEqual(len(kinds), 7)
        self.assertTrue(all(row["kind_list"] == "requirement" for row in kinds))
        self.assertEqual({row["item"] for row in rows("depends-on-file.rq")},
                         {"payments.Payment", "payments.apply_refund"})
        context = rows("task-context.rq")
        self.assertIn({"task": "IssueRefund", "role": "produces", "name": "Refund"}, context)
        self.assertIn({"task": "IssueRefund", "role": "relation", "name": "requests_refund"}, context)


if __name__ == "__main__":
    unittest.main()
