"""Change impact: what a change touches, what relies on it, and what to re-confirm."""
import importlib.util
import json
import random
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import agent_tools as at
import change_impact as ci
import entity_context as ec
import native_view_bridge as bridge
from test_entity_context import API, DB, PAY, REFUND, WEB, bundle

spec = importlib.util.spec_from_file_location("ontologyex_impact_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
A, B = "a" * 64, "b" * 64


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def model() -> dict:
    """The hand-written bundle, with line numbers and a watermarked link."""
    data = bundle()
    data["state"]["nodes"][1]["source_line"] = 7  # db is declared on compose.yml line 7
    data["business"]["actions"][0]["evidence"] = [{"path": "req.md", "quote": "Customers pay by card.",
                                                   "start_line": 3, "end_line": 4}]
    data["business"]["link_watermarks"] = [{"id": "d1", "skill": "s", "binding": "b.pay", "component": API,
                                            "recorded": {"src/pay.py": A, "compose.yml": A},
                                            "now": {"src/pay.py": A, "compose.yml": A}}]
    return data


class RuleTests(unittest.TestCase):
    def reasons(self, report, entity):
        row = next((t for t in report["touched"] if t["id"] == entity), None)
        return None if row is None else [(r["reason"], r["path"], r["line_hit"]) for r in row["reasons"]]

    def test_a_declaring_file_touches_its_systems_with_line_hits(self):
        report = ec.change_impact(model(), {"files": [{"path": "compose.yml", "status": "M", "ranges": [[6, 8]]}]})
        self.assertEqual(self.reasons(report, DB)[0], ("DECLARED_IN_CHANGED_FILE", "compose.yml", True))
        self.assertIn(("DECLARED_IN_CHANGED_FILE", "compose.yml", None), self.reasons(report, API))  # no line known
        far = ec.change_impact(model(), {"files": [{"path": "compose.yml", "ranges": [[1, 2]]}]})
        self.assertEqual(self.reasons(far, DB)[0][2], False)

    def test_skill_citations_match_by_path_suffix_and_lines(self):
        report = ec.change_impact(model(), {"files": [{"path": "docs/req.md", "ranges": [[4, 9]]}]})
        self.assertEqual(self.reasons(report, PAY), [("CITED_BY_SKILL", "docs/req.md", True)])
        self.assertIsNone(self.reasons(ec.change_impact(model(), {"files": [{"path": "xreq.md"}]}), PAY))

    def test_watermarked_evidence_predicts_reconfirmation(self):
        report = ec.change_impact(model(), {"files": [{"path": "src/pay.py", "status": "M"}]})
        self.assertEqual(self.reasons(report, PAY), [("CONFIRMED_LINK_EVIDENCE_CHANGES", "src/pay.py", None)])
        self.assertIn(("CONFIRMED_LINK_EVIDENCE_CHANGES", "src/pay.py", None), self.reasons(report, API))
        self.assertEqual(report["reconfirm_after_merge"], [{"decision_id": "d1", "skill": "s", "binding": "b.pay",
                                                            "component": API, "path": "src/pay.py"}])

    def test_a_binding_source_change_touches_the_actions_using_it(self):
        data = model()
        data["business"]["actions"][0]["bindings"][0]["evidence"] = [{"path": "pay.py", "start_line": 6, "end_line": 9}]
        report = ec.change_impact(data, {"files": [{"path": "domain/pay.py", "ranges": [[8, 8]]}]})
        self.assertEqual(self.reasons(report, PAY), [("BINDING_SOURCE_CHANGED", "domain/pay.py", True)])

    def test_skill_binding_changes_touch_the_actions_using_them(self):
        report = ec.change_impact(model(), {"bindings": [{"skill": "s", "binding": "b.pay"}]})
        self.assertEqual(self.reasons(report, PAY), [("SKILL_BINDING_CHANGED", "skill:s/b.pay", None)])
        self.assertEqual(report["changed_files"], 0)

    def test_what_relies_on_it_and_unmapped_files(self):
        report = ec.change_impact(model(), {"files": [{"path": "compose.yml", "ranges": [[7, 7]]},
                                                       {"path": "README.md", "status": "A"}]})
        touched = {t["id"] for t in report["touched"]}
        # web's evidence is .env, not compose.yml; Pay's confirmed link recorded compose.yml.
        self.assertEqual(touched, {API, DB, PAY})
        relied = {r["id"]: (r["standing"], r["via"]) for r in report["impacted"]}
        self.assertEqual(relied[WEB], ("SOURCE", API))
        quiet = model()
        quiet["business"]["link_watermarks"] = []
        later = ec.change_impact(quiet, {"files": [{"path": "compose.yml", "ranges": [[7, 7]]}]})
        self.assertEqual({r["id"]: r["standing"] for r in later["impacted"]}[PAY], "CONFIRMED")  # Pay runs on api
        self.assertTrue(touched.isdisjoint(relied))
        self.assertEqual(report["unmapped_files"], ["README.md"])
        self.assertEqual(report["semantics"], "DECLARED_REACHABILITY_FROM_CHANGED_FILES_NOT_RUNTIME_IMPACT")

    def test_invalid_change_sets_are_refused(self):
        for changes in ({"files": [{"path": "a"}, {"path": "a"}]}, {"files": [{"path": "a", "ranges": [[3, 1]]}]},
                        {"files": [{"path": "a", "status": "X"}]}, {"files": [{"path": "a\x00"}]},
                        {"files": [{"path": "a", "ranges": [[0, 1]]}]}, {"other": []}, [],
                        {"bindings": [{"skill": "s"}]}, {"files": [{"path": "a", "extra": 1}]}):
            with self.assertRaises(ec.ContextError):
                ec.change_impact(model(), changes)
        authorizing = model()
        authorizing["state"]["execution_authorized"] = True
        with self.assertRaises(ec.ContextError):
            ec.change_impact(authorizing, {"files": []})

    def test_laws_proved_in_lean_hold_on_random_change_sets(self):
        """touched_monotone and every_change_accounted, checked on the real function."""
        rng = random.Random(3)
        paths = ["compose.yml", ".env", "docs/req.md", "src/pay.py", "README.md", "x/y.go"]
        for _ in range(200):
            small = rng.sample(paths, rng.randrange(0, 4))
            large = sorted(set(small) | set(rng.sample(paths, rng.randrange(0, 4))))
            one = ec.change_impact(model(), {"files": [{"path": p} for p in small]})
            two = ec.change_impact(model(), {"files": [{"path": p} for p in large]})
            self.assertLessEqual({t["id"] for t in one["touched"]}, {t["id"] for t in two["touched"]})
            touched_paths = {r["path"] for t in two["touched"] for r in t["reasons"]}
            for path in large:
                self.assertTrue((path in touched_paths) != (path in two["unmapped_files"]), path)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="ontologyex-impact-git-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.repo, ignore_errors=True))
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "compose.yml").write_text("services:\n  api: {}\n  db: {}\n")
        (self.repo / "old.txt").write_text("gone soon\n")
        (self.repo / "keep.py").write_text("a = 1\nb = 2\nc = 3\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def test_working_tree_against_head(self):
        (self.repo / "keep.py").write_text("a = 1\nb = 20\nc = 3\nd = 4\n")
        (self.repo / "old.txt").unlink()
        (self.repo / "new file.md").write_text("hello\n")
        changes, omitted = ci.git_changes(self.repo)
        rows = {r["path"]: r for r in changes["files"]}
        self.assertEqual(omitted, 0)
        self.assertEqual(rows["keep.py"], {"path": "keep.py", "status": "M", "ranges": [[2, 2], [4, 4]]})
        self.assertEqual(rows["old.txt"], {"path": "old.txt", "status": "D"})
        self.assertEqual(rows["new file.md"], {"path": "new file.md", "status": "A"})  # untracked

    def test_branch_since_it_left_base(self):
        git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "compose.yml").write_text("services:\n  api: {}\n  db:\n    image: postgres\n")
        git(self.repo, "mv", "keep.py", "kept.py")
        git(self.repo, "commit", "-qam", "change")
        git(self.repo, "checkout", "-q", "main")
        (self.repo / "untracked.txt").write_text("not part of the branch\n")
        changes, _ = ci.git_changes(self.repo, "main", "feature")
        rows = {r["path"]: r for r in changes["files"]}
        self.assertEqual(rows["compose.yml"]["ranges"], [[3, 4]])
        self.assertEqual(rows["kept.py"]["status"], "R")
        self.assertNotIn("untracked.txt", rows)
        self.assertNotIn("keep.py", rows)

    def test_refs_are_checked_and_git_errors_are_reported(self):
        for base, head in (("--output=/tmp/x", None), ("main..x", None), (None, "feature"), ("-x", "y")):
            with self.assertRaises(ci.DiffError):
                ci.git_changes(self.repo, base, head)
        with self.assertRaises(ci.DiffError):
            ci.git_changes(self.repo, "no-such-ref")
        with self.assertRaises(ci.DiffError):
            ci.git_changes(Path(tempfile.mkdtemp(prefix="ontologyex-not-git-")))

    def test_markdown_summary(self):
        report = ec.change_impact(model(), {"files": [{"path": "src/pay.py"}, {"path": "README.md"}]})
        text = ci.markdown(report)
        self.assertIn("confirmed link rests on `src/pay.py`", text)
        self.assertIn("Confirmed links to re-confirm after merge", text)
        self.assertIn("Not mapped to any entity: `README.md`", text)


class ServedTests(unittest.TestCase):
    """The control room answers /api/impact from git and the model; agents ask through analyze_change."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.built = demo.build(Path(cls.temp.name) / "demo", unlinked=True)
        repo = Path(cls.built["repo"])
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        git(repo, "checkout", "-qb", "change")
        code = repo / "domain/payments.py"
        code.write_text(code.read_text().replace("amount_minor", "amount_minor_cents"))
        git(repo, "commit", "-qam", "rename a field")
        git(repo, "checkout", "-q", "main")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def serve(self, *extra):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([*self.built["command"], "--port", str(port), *extra],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
        base = f"http://127.0.0.1:{port}"
        for _ in range(150):
            try:
                urllib.request.urlopen(base + "/api/state", timeout=2).close()
                return base
            except OSError:
                time.sleep(0.1)
        self.fail("control room did not start")

    def get(self, base, path):
        try:
            with urllib.request.urlopen(base + path, timeout=30) as resp:
                return resp.status, json.load(resp) if path.startswith("/api") else resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def routes(self, base):
        status, report = self.get(base, "/api/impact?base=main&head=change")
        self.assertEqual(status, 200)
        self.assertEqual(report["changed_files"], 1)
        touched = {t["label"]: [(r["reason"], r["line_hit"]) for r in t["reasons"]] for t in report["touched"]}
        # The demo's links predate watermarks, so the change reaches the business through the code each
        # binding cites (payments.py): IssueRefund binds apply_refund, whose lines the rename touches.
        self.assertIn(("BINDING_SOURCE_CHANGED", True), touched["IssueRefund"])
        self.assertEqual(report["unmapped_files"], [])
        status, data = self.get(base, "/api/bundle")
        changes, _ = ci.git_changes(Path(self.built["repo"]), "main", "change")
        self.assertEqual({k: v for k, v in report.items() if k != "files_omitted"}, ec.change_impact(data, changes))
        self.assertEqual(self.get(base, "/api/impact")[1]["changed_files"], 0)  # a clean working tree
        self.assertEqual(self.get(base, "/api/impact?base=--output=x")[0], 400)
        self.assertEqual(self.get(base, "/api/impact?head=change")[0], 400)
        self.assertEqual(self.get(base, "/api/impact?x=1")[0], 400)
        status, page = self.get(base, "/impact?base=main&head=change")
        self.assertEqual(status, 200)
        self.assertIn(b"Change impact", page)

    def test_python_engine(self):
        self.routes(self.serve())

    @unittest.skipUnless(bridge.engine_binary("ontologyex-view").exists(), "Rust view engine not built")
    def test_rust_engine_gives_the_same_answer(self):
        self.routes(self.serve("--view-engine", "rust"))

    def test_agent_tool(self):
        base = self.serve()
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            tools = at.Tools(at.Source(base, None), at.load_policy(None), at.Audit(audit, "claude", "Dana"), None)
            refs = tools.call("analyze_change", {"base": "main", "head": "change"})
            self.assertEqual((refs["kind"], refs["changed_files"]), ("CHANGE_IMPACT", 1))
            named = tools.call("analyze_change", {"files": "compose.yml\nREADME.md\n"})
            self.assertEqual(named["unmapped_files"], ["README.md"])
            self.assertTrue(any(t["label"] == "payments" for t in named["touched"]))
            with self.assertRaises(at.ToolError):
                tools.call("analyze_change", {"files": "a", "base": "main"})
            with self.assertRaises(at.ToolError) as caught:
                tools.call("analyze_change", {"base": "--output=x"})
            self.assertIn("refused", str(caught.exception))
            bundle_path = Path(tmp) / "bundle.json"
            bundle_path.write_text(json.dumps(model()))
            offline = at.Tools(at.Source(None, bundle_path), at.load_policy(None), at.Audit(audit, "c", "D"), None)
            with self.assertRaises(at.ToolError) as caught:
                offline.call("analyze_change", {"base": "main"})
            self.assertIn("pass files", str(caught.exception))
            self.assertEqual(offline.call("analyze_change", {"files": "src/pay.py"})["reconfirm_after_merge"][0]["path"],
                             "src/pay.py")
            self.assertNotIn(".env", json.dumps(offline.call("analyze_change", {"files": ".env"})))


if __name__ == "__main__":
    unittest.main()
