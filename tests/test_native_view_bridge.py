"""Native view bridge safety tests; no compiled Rust required in this suite."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import native_view_bridge as bridge


class NativeViewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "ontologyex-view"
        self.path.write_text("#!/bin/sh\nexit 0\n", encoding="utf8")
        self.path.chmod(0o700)
        self.native = bridge.NativeView(self.path)
        self.graph = {"kind": "LOCAL_CONTROL_ROOM_STATE",
                      "execution_authorized": False,
                      "nodes": [], "edges": []}

    def valid(self, kind="LOCAL_CONTROL_ROOM_SUMMARY"):
        return {"kind": kind, "execution_authorized": False,
                **({"nodes": [], "edges": [],
                    "summary": {"execution_authorized": False}}
                   if kind in ("BOUNDED_GRAPH_PAGE", "BOUNDED_GRAPH_NEIGHBORHOOD")
                   else {})}

    def test_native_execution_does_not_inherit_secrets_or_use_shell(self):
        with patch.dict(os.environ, {"ONTOLOGYEX_GITHUB_TOKEN": "NO_LEAK_TO_CHILD",
                                     "DANGEROUS_SECRET": "another_secret"}):
            with patch.object(bridge.subprocess, "run") as run:
                run.return_value = Mock(
                    returncode=0, stdout=json.dumps(self.valid()).encode(),
                    stderr=b"")
                actual = self.native.execute(self.graph, "summary")
        self.assertEqual(actual["kind"], "LOCAL_CONTROL_ROOM_SUMMARY")
        args = run.call_args
        self.assertEqual(args.args[0], [str(self.path), "summary"])
        self.assertNotIn("ONTOLOGYEX_GITHUB_TOKEN", args.kwargs["env"])
        self.assertNotIn("DANGEROUS_SECRET", args.kwargs["env"])
        self.assertEqual(args.kwargs["timeout"], bridge.TIMEOUT_SECONDS)
        self.assertNotIn("shell", args.kwargs)
        self.assertLessEqual(len(args.kwargs["input"]), bridge.MAX_INPUT_BYTES)

    def test_empty_search_allowed_in_native_graph_page(self):
        with patch.object(bridge.subprocess, "run") as run:
            run.return_value = Mock(returncode=0,
                 stdout=json.dumps(self.valid("BOUNDED_GRAPH_PAGE")).encode(),
                 stderr=b"")
            data = self.native.execute(self.graph, "page", "all", "", "0")
        self.assertEqual(data["kind"], "BOUNDED_GRAPH_PAGE")

    def test_incident_requires_bounded_rows_and_never_authorizes_causality(self):
        good = {
            "kind": "UNATTESTED_INCIDENT_INVESTIGATION",
            "execution_authorized": False,
            "root_cause": "UNDETERMINED",
            "observation_semantics": "SOURCE_REPORTED_NOT_AUTHENTICATED_OR_VERIFIED",
            "timeline": [], "possible_dependents": [],
            "direct_relationships": [], "source_history": [],
        }
        with patch.object(bridge.subprocess, "run") as run:
            run.return_value = Mock(returncode=0,
                stdout=json.dumps(good).encode(), stderr=b"")
            report = self.native.execute(
                self.graph, "incident", "discovery:service:a",
                observation_rows=[])
        self.assertEqual(report["root_cause"], "UNDETERMINED")
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(sent["full"], self.graph)
        self.assertEqual(sent["rows"], [])
        with self.assertRaises(bridge.NativeViewError):
            self.native.execute(self.graph, "incident", "id")
        with self.assertRaises(bridge.NativeViewError):
            self.native.execute(self.graph, "summary", observation_rows=[])
        with self.assertRaises(bridge.NativeViewError):
            self.native.execute(self.graph, "incident", "id",
                                observation_rows=[{}]*4097)
        bad = dict(good, root_cause="CONFIRMED_DATABASE")
        with patch.object(bridge.subprocess, "run") as run:
            run.return_value = Mock(returncode=0,
                stdout=json.dumps(bad).encode(), stderr=b"")
            with self.assertRaisesRegex(bridge.NativeViewError, "safety contract"):
                self.native.execute(self.graph, "incident", "id",
                                    observation_rows=[])

    def test_invalid_queries_and_authorization_refused(self):
        for args in (("not-a-command",), ("node",), ("page","all",""),
                     ("node", "x"*1025), ("summary", "other")):
            with self.subTest(args=args), self.assertRaises(bridge.NativeViewError):
                self.native.execute(self.graph, *args)
        bad = {**self.graph, "execution_authorized": True}
        with self.assertRaises(bridge.NativeViewError):
            self.native.execute(bad, "summary")

    def test_unavailable_and_symlinked_binary_fail_fast(self):
        with self.assertRaisesRegex(bridge.NativeViewError, "missing"):
            bridge.NativeView(self.path.parent / "missing")
        link = self.path.parent / "alias"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(bridge.NativeViewError, "symlink"):
            bridge.NativeView(link)
        self.path.chmod(0o600)
        with self.assertRaisesRegex(bridge.NativeViewError, "non-executable"):
            bridge.NativeView(self.path)

    def test_native_failure_does_not_fall_back_to_python(self):
        with patch.object(bridge.subprocess, "run") as run:
            run.return_value = Mock(returncode=2, stdout=b"", stderr=b"SECRET_LEAK")
            with self.assertRaisesRegex(bridge.NativeViewError, "rejected"):
                self.native.execute(self.graph, "summary")
        with patch.object(bridge.subprocess, "run", side_effect=bridge.subprocess.TimeoutExpired("view", 5)):
            with self.assertRaisesRegex(bridge.NativeViewError, "timed out"):
                self.native.execute(self.graph, "summary")

    def test_native_contract_never_accepts_authorized_or_oversized_results(self):
        invalid = [
            self.valid("INCORRECT_KIND"),
            {"kind":"LOCAL_CONTROL_ROOM_SUMMARY","execution_authorized":True},
            {**self.valid(), "nodes":[{"id":"full-graph-must-not-leak"}]},
            {**self.valid("BOUNDED_GRAPH_PAGE"), "nodes":[{} for _ in range(91)]},
            {**self.valid("BOUNDED_NODE_DETAIL"), "edges":[{} for _ in range(41)]},
        ]
        for value in invalid:
            with self.subTest(value=value.get("kind")):
                with patch.object(bridge.subprocess, "run") as run:
                    run.return_value = Mock(returncode=0,
                        stdout=json.dumps(value).encode(), stderr=b"")
                    with self.assertRaises(bridge.NativeViewError):
                        self.native.execute(self.graph, "summary" if
                            value.get("kind") in ("LOCAL_CONTROL_ROOM_SUMMARY", "INCORRECT_KIND")
                            else "page" if value.get("kind") == "BOUNDED_GRAPH_PAGE"
                            else "node", *(("all","","0") if value.get("kind")=="BOUNDED_GRAPH_PAGE"
                                            else ("id",) if value.get("kind")=="BOUNDED_NODE_DETAIL"
                                            else ()))

    def test_malformed_duplicate_and_nonfinite_json_fail_closed(self):
        for response in (b'{"kind":"x","kind":"y"}', b'{"v":NaN}', b'\xff'):
            with self.subTest(response=response):
                with patch.object(bridge.subprocess, "run") as run:
                    run.return_value = Mock(returncode=0, stdout=response, stderr=b"")
                    with self.assertRaises(bridge.NativeViewError):
                        self.native.execute(self.graph, "summary")

    def test_concurrent_requests_rejected_not_queued_unbounded(self):
        self.assertTrue(self.native._slots.acquire(blocking=False))
        self.assertTrue(self.native._slots.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(bridge.NativeViewError, "concurrency limit"):
                self.native.execute(self.graph, "summary")
        finally:
            self.native._slots.release()
            self.native._slots.release()


if __name__ == "__main__":
    unittest.main()
