"""Change-impact evaluation preflight and citation checks without an API key."""
import os
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import impact_ab


class ImpactPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "source.txt").write_text("one\ntwo\nthree\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                        "commit", "-q", "-m", "fixture"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.dataset = {"kind": "ONTOLOGYEX_CHANGE_IMPACT_EVAL", "repositories": [{"id": "test", "commit": commit,
                        "tasks": [{"id": "q", "facts": [{"evidence": [{"path": "source.txt", "start": 2,
                                                                            "end": 2, "anchor": "two"}]}]}]}]}

    def test_preflight_requires_pinned_clean_source_and_matching_anchor(self):
        self.assertEqual(impact_ab.check_dataset(self.dataset, "test", self.repo), [])
        self.dataset["repositories"][0]["tasks"][0]["facts"][0]["evidence"][0]["anchor"] = "missing"
        self.assertTrue(impact_ab.check_dataset(self.dataset, "test", self.repo))
        (self.repo / "source.txt").write_text("one\ntwo\nchanged\n")
        self.assertTrue(any("not clean" in s for s in impact_ab.check_dataset(self.dataset, "test", self.repo)))

    def test_citation_check_rejects_missing_out_of_range_and_traversal(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret\n")
        cites = [{"path": "source.txt", "start_line": 1, "end_line": 3},
                 {"path": "source.txt", "start_line": 1, "end_line": 4},
                 {"path": "../outside.txt", "start_line": 1, "end_line": 1},
                 {"path": "missing.txt", "start_line": 1, "end_line": 1}]
        result = impact_ab.citation_check({"claims": [{"citations": cites}]}, self.repo)
        self.assertEqual((result["total"], result["valid"], len(result["invalid"])), (4, 1, 3))

    def test_docker_subprocess_environment_does_not_forward_keys(self):
        with patch.dict(os.environ, {"ONTOLOGYEX_EVAL_API_KEY": "not-a-real-key", "OTHER_SECRET": "x"}):
            env = impact_ab.clean_env()
        self.assertNotIn("ONTOLOGYEX_EVAL_API_KEY", env)
        self.assertNotIn("OTHER_SECRET", env)

    def test_invalid_model_submission_can_be_corrected(self):
        def response(args):
            return SimpleNamespace(stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                   content=[SimpleNamespace(type="tool_use", name="submit_answer", id="tool-1",
                                                            input=args)])
        invalid = {"claims": "not an array", "uncertainty": []}
        valid = {"claims": [{"claim": "one", "citations": [{"path": "source.txt", "start_line": 1,
                                                                "end_line": 1}]}], "uncertainty": []}
        client = MagicMock()
        client.messages.create.side_effect = [response(invalid), response(valid)]
        trace = io.StringIO()
        result = impact_ab.answer(client, "test-model", "medium", {"id": "q", "question": "Q"},
                                  "control", MagicMock(), MagicMock(), trace)
        self.assertEqual((result["stop"], result["submitted"]), ("submitted", valid))
        self.assertIn("claims must be an array", trace.getvalue())
        self.assertEqual(impact_ab.citation_check(invalid, self.repo)["valid"], 0)

    def test_directed_diagnostic_requires_impact_call_before_submission(self):
        def response(name, args):
            return SimpleNamespace(stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                   content=[SimpleNamespace(type="tool_use", name=name, id="tool-1", input=args)])
        submitted = {"claims": [], "uncertainty": []}
        client = MagicMock()
        client.messages.create.side_effect = [response("submit_answer", submitted),
                                              response("analyze_change", {"files": "source.txt"}),
                                              response("submit_answer", submitted)]
        ox = MagicMock()
        ox.schemas.return_value = []
        ox.call.return_value = '{"unmapped_files": ["source.txt"]}'
        trace = io.StringIO()
        result = impact_ab.answer(client, "test-model", "medium", {"id": "q", "question": "Q"},
                                  "treatment", MagicMock(), ox, trace, "source.txt")
        self.assertEqual(result["calls"], ["analyze_change"])
        self.assertIn("call analyze_change", trace.getvalue())


class ImpactShellTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("ONTOLOGYEX_IMPACT_SANDBOX_IMAGE"), "sandbox image not configured")
    def test_shell_is_offline_readonly_and_can_inspect_and_compute(self):
        image = os.environ["ONTOLOGYEX_IMPACT_SANDBOX_IMAGE"]
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            repo = Path(tmp)
            repo.chmod(0o755)
            (repo / "numbers.txt").write_text("1\n2\n")
            (repo / "numbers.txt").chmod(0o644)
            with impact_ab.Shell(repo, image) as shell:
                result = shell.call("rg -n 2 numbers.txt; python3 -c 'print(1 + 2)'")
                self.assertIn("2:2", result)
                self.assertIn("3", result)
                self.assertIn("readonly", shell.call("touch /repo/nope 2>/dev/null || echo readonly"))
                self.assertIn("isolated", shell.call("test ! -e /tmp/ontologyex-impact-eval-key/key && echo isolated"))
                network = subprocess.check_output(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}",
                                                   shell.container], text=True).strip()
                self.assertEqual(network, "none")


if __name__ == "__main__":
    unittest.main()
