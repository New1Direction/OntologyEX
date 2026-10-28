"""Decision-model client and every integration, against a local System One endpoint.

The endpoint (tests/fake_system_one.py) is not a model: handlers give controlled
answers so each integration's proposal-only behavior can be checked exactly.
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import business_view as bv
import conflict_scan
import control_room as room
import decide
import discover_repo
import evidence_grade as eg
import link_suggest as ls
import propose_sources
import statement_lint as sl
from fake_system_one import FakeSystemOne, overlap_answers
from policy_check import canonical

spec = importlib.util.spec_from_file_location("ontologyex_decide_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
API = "discovery:service:compose.yml:api"
PAYMENTS = "discovery:service:compose.yml:payments"


def choose(option_text: str, probability: float = 0.9):
    """Handler: pick the choice whose description contains option_text."""
    def handler(state, questions):
        answers = overlap_answers(state, questions)
        for qid, q in questions.items():
            if q["type"] == "choice":
                match = next((k for k, v in q["criteria"].items() if option_text in v), "none")
                if match not in q["criteria"]:
                    match = next(iter(q["criteria"]))
                rest = (1 - probability) / max(1, len(q["criteria"]) - 1)
                probs = {k: (probability if k == match else rest) for k in q["criteria"]}
                answers[qid] = {"type": "choice", "choice": match, "confidence": probability,
                                "probabilities": probs}
        return answers
    return handler


def noul_by(predicate):
    """Handler: answer each yes/no question with predicate(state, instructions)."""
    def handler(state, questions):
        return {qid: {"type": "noul", "noul": predicate(state, q["instructions"])}
                for qid, q in questions.items()}
    return handler


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def client(self, handler=overlap_answers, **options):
        server = FakeSystemOne(handler, **options)
        self.addCleanup(server.close)
        self.server = server
        return decide.DecisionClient("system-one", "clef-flash", "", server.url, self.root / "cache")


class ClientTests(Base):
    def test_all_answer_types_validate(self):
        c = self.client()
        answers = c.decide("this is about a refund payment", {
            "a": {"type": "noul", "instructions": "Is this about a refund payment?"},
            "b": {"type": "choice", "instructions": "Which team?",
                  "criteria": {"billing": "refund and payment questions", "sales": "new deals"}},
            "c": {"type": "score", "instructions": "How much about refund payment?",
                  "criteria": ["none", "some", "a lot"]}})
        self.assertEqual(answers["a"]["noul"], 1.0)
        self.assertEqual(answers["b"]["choice"], "billing")
        self.assertEqual(answers["c"]["score"], 2.0)
        self.assertEqual(self.server.requests[0]["model"], "clef-flash")

    def test_cloudflare_envelope_and_cache(self):
        c = self.client(cloudflare_envelope=True)
        q = {"x": {"type": "noul", "instructions": "Is this a refund?"}}
        first = c.decide("refund", q)
        second = c.decide("refund", q)
        self.assertEqual(first, second)
        self.assertEqual((c.calls, c.cache_hits, len(self.server.requests)), (1, 1, 1))

    def test_batches_above_64_questions(self):
        c = self.client()
        questions = {f"q{i}": {"type": "noul", "instructions": f"Is item {i} present?"} for i in range(100)}
        self.assertEqual(len(c.decide("items", questions)), 100)
        self.assertEqual([len(r["questions"]) for r in self.server.requests], [64, 36])

    def test_bad_answers_and_errors_are_refused(self):
        for bad in ({"x": {"type": "choice", "choice": "nope", "confidence": 1, "probabilities": {}}},
                    {"x": {"type": "choice", "choice": "a", "confidence": 1.5, "probabilities": {"a": 1}}},
                    {}):
            with self.subTest(bad=bad), self.assertRaises(decide.DecisionError):
                self.client(lambda s, q, bad=bad: bad).decide(
                    "s", {"x": {"type": "choice", "instructions": "?", "criteria": {"a": "", "b": ""}}})
        with self.assertRaises(decide.DecisionError):  # the endpoint answers 401
            self.client(token="secret").decide("s", {"x": {"type": "noul", "instructions": "?"}})

    def test_configuration_is_explicit(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(decide.from_env())
            with self.assertRaises(decide.DecisionError):
                decide.DecisionClient()
            with self.assertRaises(decide.DecisionError):
                decide.DecisionClient("cloudflare", "clef", "token")  # no account id
            with self.assertRaises(decide.DecisionError):
                decide.DecisionClient("system-one", url="http://example.com/v1/systemone")
            with self.assertRaises(decide.DecisionError):
                decide.DecisionClient("typesafe")  # no token
        with mock.patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "a" * 32}):
            c = decide.DecisionClient("cloudflare", "clef", "t", cache=self.root)
            self.assertTrue(c.url.endswith("/ai/run/@cf/cloudflare/clef"))

    def test_invalid_questions_never_leave(self):
        c = self.client()
        for questions in ({}, {"bad id!": {"type": "noul", "instructions": "?"}},
                          {"x": {"type": "box", "instructions": "?"}},
                          {"x": {"type": "score", "instructions": "?", "criteria": [str(i) for i in range(11)]}}):
            with self.subTest(questions=questions), self.assertRaises(decide.DecisionError):
                c.decide("s", questions)
        self.assertEqual(self.server.requests, [])


class DemoBase(Base):
    def setUp(self):
        super().setUp()
        self.built = demo.build(self.root / "demo", NOW, unlinked=True)
        self.skill = Path(self.built["skill"])
        self.state = room.build_state(discover_repo.discover(Path(self.built["repo"])), [], {}, NOW)


class LinkRouteTests(DemoBase):
    def test_model_route_adds_a_proposal_name_matching_missed(self):
        c = self.client(choose("api ("))
        result = ls.suggest([self.skill], self.state, [], [], client=c)
        rows = {(r["binding"], r["component"]): r for r in result["suggestions"]}
        routed = rows[("payments.Payment", API)]
        self.assertEqual(routed["status"], "PROPOSED")
        self.assertEqual([s["signal"] for s in routed["signals"]], ["MODEL_ROUTED"])
        self.assertEqual(routed["strength"], "MODERATE")
        self.assertIn("clef-flash", routed["signals"][0]["detail"])
        self.assertEqual(result["model_assist"]["routes_proposed"], 2)
        self.assertEqual(result["auto_applied"], 0)
        # Each binding's state is its own source quote (requests run in parallel, any order).
        states = sorted(r["state"] for r in self.server.requests)
        self.assertTrue(any("class Payment:" in st and "payments.Payment" in st for st in states))
        self.assertTrue(any("def apply_refund(" in st and "payments.apply_refund" in st for st in states))

    def test_model_signal_joins_an_existing_suggestion(self):
        c = self.client(choose("payments (", 0.65))
        rows = ls.suggest([self.skill], self.state, [], [], client=c)["suggestions"]
        row = next(r for r in rows if r["binding"] == "payments.apply_refund" and r["component"] == PAYMENTS)
        self.assertEqual([s["signal"] for s in row["signals"]][-1], "MODEL_ROUTED")
        self.assertEqual(row["signals"][-1]["strength"], "WEAK")
        self.assertEqual(row["strength"], "MODERATE")  # the path signal still sets the strength

    def test_none_or_low_probability_adds_nothing(self):
        plain = ls.suggest([self.skill], self.state, [], [])["suggestions"]
        for handler in (choose("None of these", 0.9), choose("api (", 0.4)):
            with self.subTest(handler=handler):
                result = ls.suggest([self.skill], self.state, [], [], client=self.client(handler))
                self.assertEqual(result["suggestions"], plain)

    def test_confirming_a_model_route_still_needs_a_person(self):
        c = self.client(choose("api ("))
        row = next(r for r in ls.suggest([self.skill], self.state, [], [], client=c)["suggestions"]
                   if r["component"] == API)
        links = self.root / "links.json"
        self.assertEqual(ls.decide(links, row, "confirm", "ana")["status"], "CONFIRMED")
        after = ls.suggest([self.skill], self.state, *bv.read_link_document(links), client=c)
        self.assertNotIn((row["binding"], API), {(r["binding"], r["component"]) for r in after["suggestions"]})

    def test_model_failure_keeps_deterministic_suggestions(self):
        c = self.client(lambda s, q: {})
        result = ls.suggest([self.skill], self.state, [], [], client=c)
        self.assertEqual(result["model_assist"]["status"], "ERROR")
        self.assertEqual(len(result["suggestions"]), 2)


class TriageTests(DemoBase):
    def test_low_support_first_and_nothing_recorded(self):
        c = self.client(noul_by(lambda state, q: 0.1 if "E-remaining-code" in q else 0.9))
        before = eg.grade(self.skill, Path(self.built["events"]))
        result = eg.triage(self.skill, c)
        self.assertEqual(result["rows"][0]["evidence"], "E-remaining-code")
        self.assertEqual(result["rows"][0]["flag"], "LIKELY_MISCITATION")
        self.assertEqual(result["events_written"], 0)
        self.assertEqual(eg.grade(self.skill, Path(self.built["events"])), before)

    def test_attach_triage_flags_check_citation(self):
        c = self.client(noul_by(lambda state, q: 0.1 if "E-remaining-code" in q else 0.9))
        picture = bv.project([self.skill], self.state, [], None)
        picture = bv.attach_triage(picture, eg.triage(self.skill, c)["rows"])
        flagged = [a for a in picture["attention"] if a["severity"] == "CHECK_CITATION"]
        self.assertEqual([a["label"] for a in flagged], ["IssueRefund"])
        rule = next(r for a in picture["actions"] for r in a["rules"] if r["id"] == "remaining-balance")
        self.assertEqual({e["id"]: e["model_support"] for e in rule["evidence"]},
                         {"E-remaining-balance": 0.9, "E-remaining-code": 0.1})


class SourceProposalTests(Base):
    def repo(self):
        repo = self.root / "repo"
        (repo / "src/refunds").mkdir(parents=True)
        (repo / "docs").mkdir()
        (repo / "node_modules/x").mkdir(parents=True)
        (repo / "src/refunds/cancel.py").write_text("def cancel_reservation(r):\n    refund(r)\n")
        (repo / "docs/cancellation-policy.md").write_text("# Cancellation policy\nRefund within 24 hours.\n")
        (repo / "src/unrelated.py").write_text("def render_logo():\n    pass\n")
        (repo / "node_modules/x/index.js").write_text("refund cancellation")
        (repo / ".env").write_text("TOKEN=abc")
        (repo / "config.py").write_text("KEY = 'AKIAABCDEFGHIJKLMNOP'\n")
        return repo

    def test_offline_ranking_skips_secrets_and_dependencies(self):
        result = propose_sources.propose(self.repo(), "cancel a reservation and refund")
        paths = [f["path"] for f in result["ranked"]]
        self.assertEqual(result["mode"], "WORD_OVERLAP_ONLY")
        self.assertNotIn("config.py", paths)
        self.assertNotIn(".env", paths)
        self.assertFalse(any(p.startswith("node_modules") for p in paths))
        self.assertEqual(result["coverage"]["sensitive"], 2)
        self.assertEqual(result["coverage"]["previews_sent"], 0)
        self.assertIn("--include src/refunds/cancel.py", result["next"])

    def test_model_scores_the_shortlist(self):
        def handler(state, questions):
            essential = "Cancellation policy" in state
            return {"relevance": {"type": "score", "score": 3.0 if essential else 0.0, "confidence": 0.9}}
        result = propose_sources.propose(self.repo(), "cancel a reservation and refund", self.client(handler))
        self.assertTrue(result["mode"].startswith("MODEL_SCORED:system-one:clef-flash"))
        self.assertEqual([f["path"] for f in result["proposed"]], ["docs/cancellation-policy.md"])
        self.assertEqual(result["coverage"]["previews_sent"], len(self.server.requests))
        self.assertTrue(all("Goal: cancel a reservation" in r["state"] for r in self.server.requests))


class ConflictTests(DemoBase):
    def test_candidates_never_change_the_contract(self):
        workspace = self.root / "demo" / "workspace"
        before = (workspace / "60-contract.json").read_bytes()
        c = self.client(noul_by(lambda state, q: 0.92 if "E-remaining-balance and E-remaining-code" in q else 0.05))
        result = conflict_scan.scan(workspace, c)
        self.assertEqual([x["evidence"] for x in result["candidates"]], [["E-remaining-balance", "E-remaining-code"]])
        self.assertEqual(result["candidates"][0]["draft_conflict"]["task"], "IssueRefund")
        self.assertFalse(result["contract_changed"])
        self.assertEqual((workspace / "60-contract.json").read_bytes(), before)
        self.assertGreater(result["pairs_asked"], 1)


class WordingTests(DemoBase):
    def test_model_ambiguity_is_advisory(self):
        c = self.client(noul_by(lambda state, q: 0.8 if "S0" in q else 0.1))
        result = sl.lint(self.skill, client=c)
        model = [w for w in result["warnings"] if w["code"] == "MODEL_AMBIGUITY"]
        self.assertEqual(len(model), 1)
        self.assertFalse(result["blocking"])
        self.assertEqual(result["model_check"], "system-one:clef-flash")
        self.assertEqual(len(self.server.requests), 1)  # every statement in one batched request


class CommandRouteTests(DemoBase):
    def picture(self):
        return bv.project([self.skill], self.state, [], None)

    def test_keyword_routing_offline(self):
        route = bv.route_command(self.picture(), "what is broken in IssueRefund?")
        self.assertEqual((route["severity"], route["router"]), ("AT_RISK", "keywords"))
        self.assertEqual(route["focus"], "payments-domain:action:IssueRefund")
        self.assertEqual(bv.route_command(self.picture(), "show suggested links")["view"], "suggestions")

    def test_model_routing_picks_view_item_and_filter(self):
        def handler(state, questions):
            out = {}
            for qid, q in questions.items():
                pick = {"view": "attention", "severity": "REVIEW_BLOCKED"}.get(qid) or next(
                    k for k, v in q["criteria"].items() if "ApproveException" in v)
                out[qid] = {"type": "choice", "choice": pick, "confidence": 0.9,
                            "probabilities": {pick: 0.9}}
            return out
        route = bv.route_command(self.picture(), "who approves exceptions?", self.client(handler))
        self.assertEqual(route["focus"], "payments-domain:action:ApproveException")
        self.assertEqual((route["view"], route["severity"]), ("attention", "REVIEW_BLOCKED"))
        self.assertIn("who approves exceptions", self.server.requests[0]["state"])


class ControlRoomModelTests(Base):
    def test_ask_endpoint_and_model_suggestions_over_http(self):
        built = demo.build(self.root / "demo", unlinked=True)
        server = FakeSystemOne(choose("api ("))
        self.addCleanup(server.close)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "ONTOLOGYEX_DECIDE_PROVIDER": "system-one", "ONTOLOGYEX_DECIDE_URL": server.url,
               "ONTOLOGYEX_DECIDE_CACHE": str(self.root / "cache")}
        process = subprocess.Popen([*built["command"], "--port", str(port), "--decision-model"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/api/business", timeout=5) as resp:
                    data = json.load(resp)
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("control room did not start")
        self.assertEqual(data["decision_model"]["model"], "system-one:clef-flash")
        signals = {s["signal"] for r in data["link_suggestions"]["suggestions"] for s in r["signals"]}
        self.assertIn("MODEL_ROUTED", signals)
        self.assertIn("--model-assist", data["link_suggestions"]["suggestions"][0]["confirm_command"])
        with urllib.request.urlopen(base + "/api/ask?q=" + urllib.parse.quote("show me ledger"), timeout=5) as resp:
            route = json.load(resp)
        self.assertEqual(route["kind"], "COMMAND_ROUTE")
        self.assertEqual(route["router"], "system-one:clef-flash")
        for bad in ("", "?q=", "?q=a&x=1"):
            with self.subTest(bad=bad), self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/api/ask" + bad, timeout=5)


if __name__ == "__main__":
    unittest.main()
