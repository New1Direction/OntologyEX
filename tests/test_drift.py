"""Source drift: each cited span found again (unchanged, reformatted, moved, changed, gone), and what rests on it."""
import importlib.util
import json
import os
import shutil
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
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import command_center
import drift
import rule_suggest
import watermark
from policy_check import DomainError

spec = importlib.util.spec_from_file_location("ontologyex_drift_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

CODE = ["import x", "", "def refund(p, amount):", "    if amount > p.captured:", "        raise ValueError('too much')",
        "    return p.refund(amount)", "", "def other():", "    pass"]
QUOTE = "\n".join(CODE[2:6])
SIGNATURE = "def apply_refund(payment: Payment, amount_minor: int)"


class LocateTests(unittest.TestCase):
    def test_unchanged_moved_reformatted(self):
        self.assertEqual(drift.locate(CODE, 3, 6, quote=QUOTE), {"status": "UNCHANGED", "start_line": 3, "end_line": 6})
        moved = ["# header", "# more", *CODE]
        self.assertEqual(drift.locate(moved, 3, 6, quote=QUOTE),
                         {"status": "MOVED", "start_line": 5, "end_line": 8, "from": [3, 6]})
        twice = [*CODE, "", *CODE[2:6]]  # the same lines twice: the copy nearest the old place, and how many
        self.assertEqual(drift.locate(twice, 3, 6, quote=QUOTE)["status"], "UNCHANGED")
        self.assertEqual(drift.locate(["x", *twice], 3, 6, quote=QUOTE),
                         {"status": "MOVED", "start_line": 4, "end_line": 7, "from": [3, 6], "matches": 2})
        reindented = ["import x", "", "def refund(p,   amount):", "", "  if amount > p.captured:",
                      "      raise ValueError('too much')", "  return p.refund(amount)"]
        self.assertEqual(drift.locate(reindented, 3, 6, quote=QUOTE),
                         {"status": "REFORMATTED", "start_line": 3, "end_line": 7, "from": [3, 6]})
        far = ["a"] * 30 + reindented
        self.assertEqual(drift.locate(far, 3, 6, quote=QUOTE),
                         {"status": "MOVED", "start_line": 33, "end_line": 37, "from": [3, 6], "reformatted": True})

    def test_changed_gone_and_unrecorded(self):
        changed = [*CODE[:3], "    if amount > p.captured + p.fees:", *CODE[4:]]
        found = drift.locate(changed, 3, 6, quote=QUOTE)
        self.assertEqual(found["status"], "CHANGED")
        self.assertEqual(found["nearest"]["start_line"], 3)
        self.assertGreater(found["nearest"]["similarity"], 0.9)
        self.assertNotIn("nearest", drift.locate(["completely", "different"], 1, 1, quote="def refund(p):"))
        self.assertEqual(drift.locate(None, 3, 6, quote=QUOTE), {"status": "FILE_GONE"})
        self.assertEqual(drift.locate(CODE, 3, 6), {"status": "NOT_RECORDED"})
        self.assertEqual(drift.locate(CODE, 3, 6 + drift.MAX_SPAN_LINES, recorded={"sha256": "a"})["status"],
                         "NOT_RECORDED")

    def test_a_decision_records_a_hash_only(self):
        # A comment is text: changing one is a change. Whitespace alone is not.
        recorded = drift.fingerprint(QUOTE)
        self.assertEqual(set(recorded), {"sha256", "norm_sha256", "norm_lines"})
        self.assertEqual(drift.locate(CODE, 3, 6, recorded=recorded)["status"], "UNCHANGED")
        commented = [*CODE[:4], "        raise ValueError('too much')  # never more than captured", *CODE[5:]]
        self.assertEqual(drift.locate(commented, 3, 6, recorded=recorded)["status"], "CHANGED")
        exact_only = {"sha256": recorded["sha256"]}  # rules confirmed before normalized hashes
        self.assertEqual(drift.locate(["# top", *CODE], 3, 6, recorded=exact_only)["status"], "MOVED")
        spaced = [*CODE[:3], "    if  amount > p.captured:", *CODE[4:]]
        self.assertEqual(drift.locate(spaced, 3, 6, recorded=exact_only)["status"], "CHANGED")
        self.assertEqual(drift.locate(spaced, 3, 6, recorded=recorded)["status"], "REFORMATTED")


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.built = demo.build(self.tmp / "demo")
        self.repo = Path(self.built["repo"])
        self.code = self.repo / "domain/payments.py"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def scan(self, rules=None):
        return drift.scan(self.repo, [Path(self.built["skill"])], rules)

    def states(self, report):
        return {i["label"]: i["state"] for i in report["items"]}

    def test_skill_evidence_and_the_items_resting_on_it(self):
        report = self.scan()
        self.assertEqual(set(report["counts"]), {"UNCHANGED"})
        self.assertEqual(set(self.states(report).values()), {"CURRENT"})
        self.code.write_text("# a new header\n\n" + self.code.read_text())  # every cited line moves down two
        report = self.scan()
        self.assertEqual(self.states(report)["Refund"], "REPAIRED")
        moved = [x for x in report["spans"] if x["status"] == "MOVED"]
        self.assertTrue(moved and all(x["start_line"] == x["from"][0] + 2 for x in moved))
        self.code.write_text(self.code.read_text().replace(SIGNATURE, SIGNATURE.replace(")", ", force=False)")))
        report = self.scan()
        states = self.states(report)
        self.assertEqual((states["Refund"], states["IssueRefund"]), ("STALE_REPAIR", "STALE_REPAIR"))
        self.assertEqual(states["Payment"], "REPAIRED")  # rests only on lines that moved
        changed = next(x for x in report["spans"] if x["status"] == "CHANGED")
        self.assertEqual((changed["path"], changed["nearest"]["start_line"]), ("domain/payments.py", changed["from"][0] + 2))
        text = drift.markdown(report)
        self.assertIn("## Needs repair", text)
        self.assertIn("domain/payments.py", text)
        items = command_center.report({"state": {"nodes": [], "edges": []}, "drift": report})["items"]
        stale = [i for i in items if i["kind"] == "STALE_REPAIR"]
        self.assertEqual({i["title"] for i in stale}, {"Refund was modeled from code that changed",
                                                       "IssueRefund was modeled from code that changed"})
        self.assertEqual(stale[0]["category"], "Decisions")
        (self.repo / "domain/requirements.md").unlink()
        self.assertIn("FILE_GONE", self.scan()["counts"])

    def test_confirmed_rules_follow_their_lines(self):
        rules = self.tmp / "rules.json"
        files = watermark.RepoFiles(self.repo, ttl=0)
        lines = self.code.read_text().split("\n")
        at = next(i for i, line in enumerate(lines) if "amount outside refundable remainder" in line)
        citation = {"path": "domain/payments.py", "start_line": at, "end_line": at + 1}
        suggestion = {"target": "payments-domain:action:IssueRefund", "statement": "a refund never exceeds the remainder",
                      "citation": citation, "citation_check": rule_suggest.check_citation(files, citation),
                      "evidence": {"proposed_by": "agent:claude", "on_behalf_of": "Dana", "reason": "the guard",
                                   "proposed_at": "2026-10-30T00:00:00Z"}, "status": "PROPOSED"}
        rule_suggest.apply_decisions(rules, "Dana", confirm=[suggestion])
        row = rule_suggest.read_rule_document(rules)[0][0]
        self.assertEqual(set(row["lines_norm"]), {"norm_sha256", "norm_lines"})

        def view():
            return rule_suggest.confirmed_view(rule_suggest.read_rule_document(rules)[0], files)[0]
        self.assertEqual((view()["status"], view()["drift"]["status"]), ("CONFIRMED_BY_PERSON", "UNCHANGED"))
        self.code.write_text("# header\n" + self.code.read_text())
        moved = view()
        self.assertEqual((moved["status"], moved["evidence_check"], moved["drift"]),
                         ("CONFIRMED_BY_PERSON", "CURRENT",
                          {"status": "MOVED", "start_line": at + 1, "end_line": at + 2, "from": [at, at + 1]}))
        self.assertIn("amount outside refundable remainder", moved["quote"])  # read where the lines are now
        self.code.write_text(self.code.read_text().replace("    if not 0 < amount_minor", "        if not 0 < amount_minor"))
        self.assertEqual(view()["drift"], {"status": "REFORMATTED", "start_line": at + 1, "end_line": at + 2,
                                           "from": [at, at + 1]})  # shifted a line and re-indented
        self.assertTrue(self.scan(rules)["rules"][0]["status"] in ("MOVED", "REFORMATTED"))
        self.code.write_text(self.code.read_text().replace("refundable remainder", "remaining balance"))
        changed = view()
        self.assertEqual((changed["status"], changed["evidence_check"]),
                         ("CONFIRMED_BEFORE_EVIDENCE_CHANGED", "CHANGED_SINCE_DECISION"))
        self.assertEqual(self.scan(rules)["summary"]["rules_changed"], 1)

    def test_the_agent_tool(self):
        import agent_tools as at
        self.code.write_text(self.code.read_text().replace(SIGNATURE, SIGNATURE.replace(")", ", force=False)")))
        bundle = {"state": {"nodes": [], "edges": [], "execution_authorized": False}, "drift": self.scan()}
        (self.tmp / "bundle.json").write_text(json.dumps(bundle))
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"schema_version": 1, "allow_tools": ["source_drift"],
                                      "deny_paths": ["domain/requirements.md"]}))
        tools = at.Tools(at.Source(None, self.tmp / "bundle.json"), at.load_policy(policy),
                         at.Audit(self.tmp / "audit.jsonl", "claude", "Dana"), None)
        found = tools.call("source_drift", {"id": "payments-domain:object:Refund"})
        self.assertEqual([i["state"] for i in found["items"]], ["STALE_REPAIR"])
        self.assertFalse(found["execution_authorized"])
        everything = json.dumps(tools.call("source_drift", {}))
        self.assertNotIn("requirements.md", everything)  # a denied file stays withheld

    def test_link_watermarks_compare_cited_lines_and_keep_old_records(self):
        marks = watermark.Marks(watermark.RepoFiles(self.repo, ttl=0), [Path(self.built["skill"])],
                                {"nodes": [], "source_evidence": []})
        key = ("payments-domain", "payments.apply_refund", "none")
        span = marks.now(*key)
        self.assertIn("span:domain/payments.py", span)
        whole = {"domain/payments.py": watermark.RepoFiles(self.repo, ttl=0).sha256("domain/payments.py")}
        self.assertIn("domain/payments.py", marks.now(*key, recorded=whole))  # decided before span hashes
        self.code.write_text(self.code.read_text() + "\n# unrelated\n")
        self.assertEqual(marks.now(*key), span)
        self.assertNotEqual(marks.now(*key, recorded=whole), whole)

    def test_the_command_line_and_the_hook(self):
        run = subprocess.run([sys.executable, str(SCRIPTS / "drift.py"), "check", "--repo", str(self.repo),
                              "--skill", self.built["skill"], "--quiet-when-current", "--fail-on-stale"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((run.returncode, run.stdout), (0, ""))
        self.code.write_text(self.code.read_text().replace(SIGNATURE, SIGNATURE.replace(")", ", force=False)")))
        run = subprocess.run([sys.executable, str(SCRIPTS / "drift.py"), "check", "--repo", str(self.repo),
                              "--skill", self.built["skill"], "--fail-on-stale", "--format", "json"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)["summary"]["stale_items"], 2)
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x.test"}
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], capture_output=True, text=True, env=env,
                                         timeout=60)
        git("init", "-q")
        hook = self.repo / ".git/hooks/post-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho theirs\n")
        with self.assertRaisesRegex(DomainError, "not ours"):
            drift.install_hook(self.repo, [Path(self.built["skill"])], None)
        hook.unlink()
        self.assertEqual(drift.install_hook(self.repo, [Path(self.built["skill"])], None)["status"], "INSTALLED")
        git("add", "-A")
        commit = git("commit", "-q", "-m", "change the refund signature")
        self.assertEqual(commit.returncode, 0, commit.stderr)  # the hook never blocks
        self.assertIn("Needs repair", commit.stdout + commit.stderr)
        self.assertEqual(drift.remove_hook(self.repo)["status"], "REMOVED")
        self.assertEqual(drift.remove_hook(self.repo)["status"], "NOT_INSTALLED")


class RouteTests(unittest.TestCase):
    def test_the_control_room_serves_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            code = Path(built["repo"]) / "domain/payments.py"
            code.write_text(code.read_text().replace(SIGNATURE, SIGNATURE.replace(")", ", force=False)")))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port)], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def get(path):
                with urllib.request.urlopen(base + path, timeout=60) as resp:
                    return resp.read().decode()
            report = json.loads(get("/api/drift"))
            self.assertEqual(report["summary"]["stale_items"], 2)
            one = json.loads(get("/api/drift?id=payments-domain:object:Refund"))
            self.assertEqual([i["state"] for i in one["items"]], ["STALE_REPAIR"])
            self.assertTrue(one["spans"] and all(x["skill"] == "payments-domain" for x in one["spans"]))
            self.assertTrue(get("/api/drift?format=markdown").startswith("# Source drift"))
            kinds = {i["kind"] for i in json.loads(get("/api/command"))["items"]}
            self.assertIn("STALE_REPAIR", kinds)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                get("/api/drift?x=1")
            self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
