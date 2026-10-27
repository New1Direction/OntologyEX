"""N-Triples serialization boundary regression for the OntologyEX graph export."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import rdf_export as rdf
from policy_check import DomainError


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


if __name__ == "__main__":
    unittest.main()
