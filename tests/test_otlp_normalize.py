"""OpenTelemetry semantic-convention adapter: privacy and shape regression."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "ontology-extraction/scripts"
sys.path.insert(0, str(TOOLS))
import otlp_normalize as otlp
import trace_extract as extract


def attr(key, value):
    return {"key": key, "value": {"stringValue": value}}


def example():
    return {"resourceSpans": [{"resource": {"attributes": [attr("service.name", "example")]},
        "scopeSpans": [{"spans": [
            {"traceId": "a" * 32, "spanId": "b" * 16, "name": "secret confidential data",
             "attributes": [
                 attr("gen_ai.operation.name", "invoke_agent"),
                 attr("gen_ai.agent.name", "builder"),
                 attr("gen_ai.prompt", "PRIVATE CUSTOMER PROMPT"),
                 attr("gen_ai.completion", "PRIVATE MODEL COMPLETION"),
                 attr("ontologyex.root_goal_id", "goal-test")],
             "status": {"code": "STATUS_CODE_OK"}, "startTimeUnixNano": "1792674000000000000"},
            {"traceId": "a" * 32, "spanId": "c" * 16, "parentSpanId": "b" * 16,
             "attributes": [
                 attr("gen_ai.operation.name", "execute_tool"),
                 attr("gen_ai.agent.name", "builder"),
                 attr("gen_ai.tool.name", "git.status"),
                 attr("gen_ai.tool.call.arguments", "API_KEY_123456789"),
                 attr("ontologyex.entity_type", "Repository")],
             "status": {"code": "STATUS_CODE_ERROR"}}
        ]}]}]}


class OTLPTests(unittest.TestCase):
    def test_maps_genai_spans_to_draft_graph(self):
        source = json.dumps(example()).encode()
        normalized = otlp.normalize(source)
        self.assertEqual(normalized, otlp.normalize(source))
        self.assertNotIn(b"PRIVATE CUSTOMER", normalized)
        self.assertNotIn(b"PRIVATE MODEL", normalized)
        self.assertNotIn(b"API_KEY_", normalized)
        self.assertNotIn(b"secret confidential", normalized)
        proposal = extract.project(normalized)
        self.assertEqual(proposal["span_count"], 2)
        self.assertIn("goal-test", proposal["root_goal_ids"])
        self.assertIn("child_of", [v["relation"] for v in proposal["graph"]["edges"]])
        self.assertTrue(proposal["ontology_draft"]["action_types"])
        self.assertEqual(proposal["ontology_draft"]["rules"], [])
        self.assertFalse(proposal["execution_authorized"])

    def test_unsafe_allowlisted_attribute_is_rejected(self):
        obj = example()
        obj["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["attributes"][2] = attr(
            "gen_ai.tool.name", "https://example/?key=secret")
        with self.assertRaises(extract.TraceError):
            otlp.normalize(json.dumps(obj).encode())

    def test_no_import_of_arbitrary_tool_payload(self):
        obj = example()
        obj["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["attributes"].append(
            {"key": "tool.payload", "value": {"stringValue": "INTERNAL SECRET DATA"}})
        result = otlp.normalize(json.dumps(obj).encode())
        self.assertNotIn(b"INTERNAL SECRET DATA", result)

    def test_dangling_parent_not_invented(self):
        obj = example()
        obj["resourceSpans"][0]["scopeSpans"][0]["spans"] = obj["resourceSpans"][0]["scopeSpans"][0]["spans"][1:]
        normalized = otlp.normalize(json.dumps(obj).encode())
        self.assertNotIn(b"parent_span_id", normalized)

    def test_empty_output_rejected(self):
        with self.assertRaises(extract.TraceError):
            otlp.normalize(json.dumps({"resourceSpans": []}).encode())


if __name__ == "__main__":
    unittest.main()
