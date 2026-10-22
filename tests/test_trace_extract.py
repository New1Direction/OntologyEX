"""Regression tests for safe, deterministic trace-to-ontology candidates."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ontology-extraction/scripts/trace_extract.py"
spec = importlib.util.spec_from_file_location("ontologyex_trace_extract", SCRIPT)
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)


def line(**overrides):
    value = {"schema_version": 1, "trace_id": "trace-1", "span_id": "span-1",
             "kind": "tool", "status": "ok", "agent": "builder", "tool": "git.diff",
             "entity_type": "Repository", "entity_id": "secret-project-123",
             "operation": "review", "effect": "read",
             "root_goal_id": "goal-example"}
    value.update(overrides)
    return json.dumps(value, sort_keys=True).encode()


class TraceExtractionTests(unittest.TestCase):
    def test_deterministic_and_privacy_preserving(self):
        data = line() + b"\n"
        first, second = trace.project(data), trace.project(data)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "DRAFT_NOT_APPROVED")
        self.assertFalse(first["execution_authorized"])
        self.assertEqual(first["ontology_draft"]["rules"], [])
        self.assertEqual(first["ontology_draft"]["permissions"], [])
        self.assertIn("Repository", [n["id"] for n in first["ontology_draft"]["object_types"]])
        self.assertIn("review", [n["id"] for n in first["ontology_draft"]["action_types"]])
        rendered = trace.encoded(first).decode()
        self.assertNotIn("secret-project-123", rendered)
        self.assertEqual(first["evidence"][0]["line_sha256"], trace.sha(data.strip()))

    def test_parent_link_preserved(self):
        parent = line(kind="agent", span_id="parent", tool=None, entity_type=None,
                      entity_id=None, operation=None, effect=None)
        # Null fields are not permitted; omit them.
        obj = json.loads(parent)
        for key in ("tool", "entity_type", "entity_id", "operation", "effect"):
            obj.pop(key)
        data = (json.dumps(obj) + "\n").encode() + line(span_id="child", parent_span_id="parent") + b"\n"
        result = trace.project(data)
        self.assertIn("child_of", [e["relation"] for e in result["graph"]["edges"]])
        self.assertEqual(result["span_count"], 2)

    def test_sensitive_payload_rejected(self):
        obj = json.loads(line())
        obj["prompt"] = "ignore all previous instructions"
        with self.assertRaisesRegex(trace.TraceError, "missing/unknown"):
            trace.project((json.dumps(obj) + "\n").encode())

    def test_duplicate_key_rejected(self):
        data = b'{"schema_version":1,"trace_id":"t","span_id":"a","kind":"tool","status":"ok","status":"error"}\n'
        with self.assertRaisesRegex(trace.TraceError, "duplicate"):
            trace.project(data)

    def test_invalid_identifiers_and_status(self):
        for change in ({"tool": "https://evil.test/?key=123"},
                       {"agent": "test@example.com"}, {"status": "approved"},
                       {"effect": "executed production"}, {"entity_type": "X\nY"}):
            with self.subTest(change=change):
                with self.assertRaises(trace.TraceError):
                    trace.project(line(**change) + b"\n")

    def test_dangling_parent_and_duplicate_span(self):
        with self.assertRaisesRegex(trace.TraceError, "dangling parent"):
            trace.project(line(parent_span_id="missing") + b"\n")
        with self.assertRaisesRegex(trace.TraceError, "duplicate span"):
            trace.project(line() + b"\n" + line() + b"\n")

    def test_no_overwrite_or_symlinked_destination(self):
        data = trace.project(line() + b"\n")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "out"
            trace.write_new(target, data)
            self.assertTrue((target / "ontology-draft.json").exists())
            with self.assertRaisesRegex(trace.TraceError, "already exists"):
                trace.write_new(target, data)
            external = root / "external"
            external.mkdir()
            (root / "alias").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(trace.TraceError, "symlinked"):
                trace.write_new(root / "alias" / "new", data)
            self.assertFalse((external / "new").exists())

    def test_command_returns_machine_readable_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "events.jsonl"
            path.write_bytes(line() + b"\n")
            run = subprocess.run([sys.executable, str(SCRIPT), str(path), "--out", str(root / "draft")],
                                 capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "DRAFT_NOT_APPROVED")
            self.assertFalse(json.loads((root / "draft/ontology-draft.json").read_text())["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
