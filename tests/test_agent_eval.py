"""check_answerable and the with/without-OntologyEX agent eval (evals/agent_ab.py).

The pre-flight is deterministic: what the sources cover for a question, what they do not, and which kinds
of source it needs. The eval's harness is tested with a scripted agent, so scoring, abstention, citation
checks and the fixed/broken report are pinned without a model or a key.
"""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "evals"))
import agent_ab
import agent_tools
import answerable

DATASET = ROOT / "evals/datasets/demo-shop.json"
R = "domain/requirements.md"


class AnswerableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.prepared = agent_ab.prepare(agent_ab.load_dataset(DATASET), Path(cls.tmp.name))
        cls.bundle = json.loads(cls.prepared["bundle"].read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ask(self, question):
        return answerable.check(self.bundle, question)

    def test_a_rule_the_model_holds_is_answerable_and_cites_it(self):
        found = self.ask("Can a refund exceed the captured amount?")
        self.assertEqual((found["verdict"], found["uncovered"], found["execution_authorized"]), ("ANSWERABLE", [], False))
        refund = next(c for c in found["covered"] if c["term"] == "refund")
        self.assertIn({"path": "requirements.md", "lines": [5, 5]}, [e for c in refund["covered_by"] for e in c["evidence"]])
        self.assertTrue(found["next"] and all(n["tool"] == "get_entity_context" for n in found["next"]))

    def test_an_unknown_the_model_records_is_an_open_question(self):
        found = self.ask("Who approves exceptions over the limit?")
        self.assertEqual(found["verdict"], "PARTLY")
        self.assertIn("The supplied requirements do not specify who can approve an exception.",
                      [q["statement"] for q in found["open_questions"]])

    def test_words_and_sources_it_does_not_have_are_named(self):
        found = self.ask("What was return on ad spend in June?")
        self.assertEqual(found["verdict"], "PARTLY")
        self.assertIn("advertising", found["uncovered"])  # "ad" is a subject, spelled out
        self.assertNotIn("june", found["terms"])  # dates and time words are not subjects
        owner = self.ask("Who owns IssueRefund?")
        self.assertEqual([n["present"] for n in owner["needs"] if n["source"] == "ownership"], [False])
        self.assertEqual(owner["terms"], ["owner", "issue", "refund"])  # names split as the model splits them

    def test_nothing_in_the_sources_is_said_so(self):
        found = self.ask("What is the kubernetes autoscaler quota?")
        self.assertEqual((found["verdict"], found["coverage"]), ("NOT_IN_SOURCES", 0.0))
        with self.assertRaises(ValueError):
            self.ask("   ")

    def test_live_health_counts_as_a_source(self):
        found = self.ask("What is the error rate of payments right now?")
        self.assertEqual((found["verdict"], found["uncovered"]), ("ANSWERABLE", []))
        self.assertIn({"present": True, "source": "health", "what": "live health (feeds or traces)"}, found["needs"])

    def test_the_agent_tool_and_its_arguments(self):
        tools = agent_tools.Tools(agent_tools.Source(None, self.prepared["bundle"]), agent_tools.load_policy(None),
                                  agent_tools.Audit(Path(self.tmp.name) / "audit.jsonl", "claude", "Dana"), None)
        self.assertEqual(agent_tools.TOOLS["check_answerable"]["kind"], agent_tools.READ_ONLY)
        self.assertEqual(tools.call("check_answerable", {"question": "Which payments can be refunded?"})["verdict"],
                         "ANSWERABLE")
        policy = Path(self.tmp.name) / "policy.json"
        policy.write_text(json.dumps({"schema_version": 1, "allow_tools": ["check_answerable"],
                                      "deny_paths": ["requirements.md", "docs/refund-policy.md"]}))
        guarded = agent_tools.Tools(agent_tools.Source(None, self.prepared["bundle"]), agent_tools.load_policy(policy),
                                    agent_tools.Audit(Path(self.tmp.name) / "audit.jsonl", "claude", "Dana"), None)
        text = json.dumps(guarded.call("check_answerable", {"question": "Can a captured payment be refunded within 30 days?"}))
        self.assertIn('"withheld": true', text)
        for needle in ("requirements.md", "refund-policy", "Only a Payment with status captured", "within 30 days of purchase"):
            self.assertNotIn(needle, text)  # withheld files are not named, and their rules are not quoted
        for bad in ({}, {"question": ""}, {"question": "x" * 501}, {"question": "a", "extra": 1}):
            with self.assertRaises(agent_tools.ToolError, msg=bad):
                tools.call("check_answerable", bad)


class RouteTests(unittest.TestCase):
    def test_the_control_room_answers_the_pre_flight(self):
        spec = importlib.util.spec_from_file_location("ontologyex_answerable_demo", ROOT / "examples/operating-picture/demo.py")
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
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
            query = urllib.parse.urlencode({"q": "Which payments can be refunded?"})
            with urllib.request.urlopen(f"{base}/api/answerable?{query}", timeout=60) as resp:
                self.assertEqual(json.loads(resp.read())["verdict"], "ANSWERABLE")
            for bad in ("", "?x=1", "?q=a&q=b"):
                with self.assertRaises(urllib.error.HTTPError, msg=bad) as caught:
                    urllib.request.urlopen(f"{base}/api/answerable{bad}", timeout=60)
                self.assertEqual(caught.exception.code, 400)


def submit(answer, answerable_, *cites):
    return {"answer": answer, "answerable": answerable_,
            "citations": [{"path": p, "start_line": a, "end_line": b} for p, a, b in cites]}


SCRIPT = {
    "control": {
        "refund-captured-only": {"calls": [["search_text", {"pattern": "captured", "glob": "*.md"}]],
                                 "submit": submit("Only payments with status captured.", True, (R, 15, 15))},
        "payments-error-rate": {"calls": [["list_files", {}]],
                                "submit": submit("The repository holds no health data.", False)},
        "approve-exception": {"calls": [["read_file", {"path": "docs/refund-policy.md"}]],
                              "submit": submit("A manager.", True, ("docs/refund-policy.md", 5, 5))},
        "email-supplier": {"calls": [], "submit": submit("SendGrid.", True, ("package.json", 1, 99))},
        "disputes-open": {"calls": [["search_text", {"pattern": "needs_response|under_review"}]],
                          "submit": submit("2 disputes.", True)},
        "refund-ceiling": {"calls": [], "submit": submit("Up to $5,000.", True, ("nowhere.md", 1, 1))},
    },
    "treatment": {
        "refund-captured-only": {"calls": [["check_answerable", {"question": "Which payments can be refunded?"}]],
                                 "submit": submit("Only a **captured** payment.", True, ("requirements.md", 15, 15))},
        "payments-error-rate": {"calls": [["what_needs_attention", {}]],
                                "submit": submit("payments reports an error rate of 12.5%.", True)},
        "approve-exception": {"calls": [["check_answerable", {"question": "Who may approve an exception?"}]],
                              "submit": submit("The requirements leave it unspecified.", False, (R, 21, 21))},
        "email-supplier": {"calls": [["list_suppliers", {}]], "submit": submit("SendGrid", True, ("package.json", 1, 1))},
        "disputes-open": {"calls": [["operations_status", {}]], "submit": submit("12 disputes.", True)},
        "refund-ceiling": {"calls": [], "submit": submit("The remaining captured amount (captured minus refunded).",
                                                         True, (R, 17, 17))},
    },
}


class HarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        script = Path(cls.tmp.name) / "script.json"
        script.write_text(json.dumps(SCRIPT))
        cls.out = Path(cls.tmp.name) / "run"
        cls.report = agent_ab.run(DATASET, cls.out, agent_ab.ScriptedAgent(script), "scripted", only=list(SCRIPT["control"]))
        cls.scores = {(r["arm"], r["id"]): r for r in map(json.loads, (cls.out / "scores.jsonl").read_text().splitlines())}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_datasets_hold_against_their_files(self):
        dataset = agent_ab.load_dataset(DATASET)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(agent_ab.check(dataset, agent_ab.prepare(dataset, Path(tmp))), [])
        fastapi = agent_ab.load_dataset(ROOT / "evals/datasets/fastapi-template.json")
        self.assertEqual(len(fastapi["questions"]), 13)
        for data in (dataset, fastapi):
            self.assertTrue(all(q["facts"] for q in data["questions"] if q["answerable"]), data["name"])
            self.assertFalse(any(q["facts"] for q in data["questions"] if not q["answerable"]), data["name"])

    def test_scoring(self):
        s = self.scores
        self.assertEqual((s["control", "refund-captured-only"]["strict"], s["control", "refund-captured-only"]["grounded"]),
                         (True, True))
        self.assertEqual((s["control", "payments-error-rate"]["correct"], s["control", "payments-error-rate"]["confidently_wrong"]),
                         (False, False))  # declined: wrong, but not confidently
        self.assertEqual((s["control", "approve-exception"]["correct"], s["control", "approve-exception"]["confidently_wrong"]),
                         (False, True))  # answered what the sources leave open
        email = s["control", "email-supplier"]
        self.assertEqual((email["correct"], email["citations_valid"], email["grounded"], email["strict"]),
                         (True, 0, False, False))  # a line that does not exist is not a citation
        self.assertEqual((s["treatment", "refund-captured-only"]["grounded"], s["treatment", "refund-captured-only"]["correct"]),
                         (True, True))  # a unique suffix path resolves; markdown is ignored
        self.assertTrue(s["treatment", "approve-exception"]["correct"])
        self.assertFalse(s["treatment", "disputes-open"]["correct"])  # '2' is not in '12': whole words only
        self.assertEqual(s["control", "refund-ceiling"]["citations_valid"], 0)
        self.assertEqual(s["treatment", "payments-error-rate"]["ontologyex_calls"], 1)
        self.assertEqual(s["control", "payments-error-rate"]["ontologyex_calls"], 0)

    def test_the_report(self):
        found = self.report
        self.assertEqual((found["fixed_by_ontologyex"], found["broken_by_ontologyex"]),
                         (["approve-exception", "payments-error-rate", "refund-ceiling"], ["disputes-open"]))
        control, treatment = found["arms"]["control"], found["arms"]["treatment"]
        self.assertEqual((control["correct"], treatment["correct"], control["confidently_wrong"]), (3, 5, 2))
        self.assertEqual((control["declined_correctly"], treatment["declined_correctly"]), ("0/1", "1/1"))
        self.assertEqual(found["semantics"], "SAME_MODEL_SAME_QUESTIONS_DETERMINISTIC_SCORING_NO_MODEL_JUDGE")
        self.assertIn("| Correct | 50% | 83% |", (self.out / "report.md").read_text())
        audit = [json.loads(line) for line in (self.out / "ontologyex-audit.jsonl").read_text().splitlines()]
        self.assertEqual(sorted({a["tool"] for a in audit}),
                         ["check_answerable", "list_suppliers", "operations_status", "what_needs_attention"])
        printed = subprocess.run([sys.executable, str(ROOT / "evals/agent_ab.py"), "report", str(self.out), "--markdown"],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(printed.stdout.rstrip("\n"), (self.out / "report.md").read_text().rstrip("\n"))

    def test_the_control_arm_has_no_ontologyex(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.json"
            script.write_text(json.dumps({"control": {"email-supplier": {"calls": [["list_suppliers", {}]],
                                                                         "submit": submit("x", True)}}}))
            with self.assertRaisesRegex(agent_ab.EvalError, "control arm does not have"):
                agent_ab.run(DATASET, Path(tmp) / "run", agent_ab.ScriptedAgent(script), "scripted", only=["email-supplier"])
            with self.assertRaisesRegex(agent_ab.EvalError, "not empty"):
                agent_ab.run(DATASET, self.out, agent_ab.ScriptedAgent(script), "scripted")

    def test_a_missing_key_variable_is_said_plainly(self):
        run = subprocess.run([sys.executable, str(ROOT / "evals/agent_ab.py"), "run", str(DATASET), "--out",
                              str(Path(self.tmp.name) / "keyless"), "--key-env", "ONTOLOGYEX_EVAL_KEY_FOR_TEST"],
                             capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(run.returncode, 2)
        self.assertTrue("ONTOLOGYEX_EVAL_KEY_FOR_TEST is not set" in run.stderr or "anthropic package" in run.stderr,
                        run.stderr)

    def test_whole_words(self):
        self.assertTrue(agent_ab.says(agent_ab.normalize("Refunds: **30 days**"), "30 days"))
        self.assertFalse(agent_ab.says("within 300 days", "30"))
        self.assertTrue(agent_ab.says(agent_ab.normalize("3,142.00 is missing"), "3142.00"))
        self.assertFalse(agent_ab.says("rapid", "api"))


if __name__ == "__main__":
    unittest.main()
