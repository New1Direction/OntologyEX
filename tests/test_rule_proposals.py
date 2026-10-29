"""Agent rule proposals: cite the lines, a person decides, and a change to those lines reopens it."""
import importlib.util
import json
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
import agent_tools as at
import entity_context as ec
import native_view_bridge as bridge
import rule_suggest as rs
import watermark
from policy_check import DomainError

spec = importlib.util.spec_from_file_location("ontologyex_rule_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

REFUND = "payments-domain:action:IssueRefund"
STATEMENT = "A refund never exceeds what is still captured and not yet refunded."
CITE = {"path": "payments.py", "start_line": 25, "end_line": 26}
AT = "2026-10-29T10:00:00Z"


def proposal(statement=STATEMENT, citation=CITE, by="agent:claude", target=REFUND):
    return {"kind": rs.PROPOSAL_KIND, "id": rs.rule_id(target, statement), "at": AT, "proposed_by": by,
            "on_behalf_of": "Dana", "target": target, "statement": statement, "citation": citation,
            "reason": "apply_refund raises when the amount is outside the refundable remainder"}


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ontologyex-rules-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "domain").mkdir()
        (self.tmp / "domain/payments.py").write_text("\n".join(f"line {i}" for i in range(1, 31)) + "\n")
        (self.tmp / ".env").write_text("STRIPE_SECRET=sk_live_x\n")
        self.files = watermark.RepoFiles(self.tmp, ttl=0)

    def test_valid_proposals(self):
        self.assertTrue(rs.valid_proposal(proposal()))
        for bad in (proposal(statement="short"), proposal(citation={**CITE, "end_line": 24}),
                    proposal(citation={**CITE, "path": "/etc/passwd"}), proposal(citation={**CITE, "path": "a/../b"}),
                    proposal(citation={**CITE, "end_line": 200}), proposal(by="Dana"), {**proposal(), "id": "forged"}):
            self.assertFalse(rs.valid_proposal(bad), bad)
        self.assertEqual(rs.rule_id(REFUND, "A  refund NEVER exceeds"), rs.rule_id(REFUND, "a refund never exceeds"))

    def test_citations_are_checked_and_secrets_never_quoted(self):
        found = rs.check_citation(self.files, CITE)
        self.assertEqual((found["status"], found["path"], found["quote"]), ("LINES_FOUND", "domain/payments.py",
                                                                            "line 25\nline 26"))
        self.assertEqual(rs.check_citation(self.files, {**CITE, "start_line": 40, "end_line": 41})["status"],
                         "LINES_OUT_OF_RANGE")
        self.assertEqual(rs.check_citation(self.files, {**CITE, "path": "nope.py"})["status"], "FILE_NOT_FOUND")
        self.assertEqual(rs.check_citation(self.files, {"path": ".env", "start_line": 1, "end_line": 1}),
                         {"status": "WITHHELD_SECRET_FILE"})

    def test_decide_then_the_cited_lines_change(self):
        rules = self.tmp / "rules.json"
        open_rules = rs.suggest([proposal(), proposal(by="agent:ci")], [], [], {REFUND}, self.files)
        row = open_rules["suggestions"][0]
        self.assertEqual(len(open_rules["suggestions"]), 1)  # two agents, one rule
        self.assertEqual(len(row["signals"]), 2)
        rs.apply_decisions(rules, "Dana", "read apply_refund", confirm=[row])
        confirmed, _ = rs.read_rule_document(rules)
        self.assertEqual(confirmed[0]["lines_sha256"], row["citation_check"]["lines_sha256"])
        self.assertEqual(rs.confirmed_view(confirmed, self.files)[0]["status"], "CONFIRMED_BY_PERSON")
        self.assertEqual(rs.suggest([proposal()], confirmed, [], {REFUND}, self.files)["suggestions"], [])
        text = (self.tmp / "domain/payments.py").read_text().replace("line 26", "line 26 changed")
        (self.tmp / "domain/payments.py").write_text(text)
        view = rs.confirmed_view(confirmed, self.files)[0]
        self.assertEqual((view["status"], view["evidence_check"]), ("CONFIRMED_BEFORE_EVIDENCE_CHANGED",
                                                                    "CHANGED_SINCE_DECISION"))
        rs.apply_decisions(rules, "Bo", "still holds", reconfirm=rs.reconfirm_rows(confirmed, [view["id"]], self.files))
        confirmed, _ = rs.read_rule_document(rules)
        self.assertEqual((rs.confirmed_view(confirmed, self.files)[0]["status"], confirmed[0]["declared_by"]),
                         ("CONFIRMED_BY_PERSON", "Bo"))

    def test_rejected_rules_stay_rejected_and_bad_documents_fail(self):
        rules = self.tmp / "rules.json"
        row = rs.suggest([proposal()], [], [], {REFUND}, self.files)["suggestions"][0]
        rs.apply_decisions(rules, "Dana", "not a rule", reject=[row])
        _, rejected = rs.read_rule_document(rules)
        self.assertNotIn("lines_sha256", rejected[0])
        self.assertEqual(rs.suggest([proposal()], [], rejected, {REFUND}, self.files)["not_shown"], 1)
        self.assertEqual(rs.suggest([proposal(target="x:action:Y")], [], [], {REFUND}, self.files)["suggestions"], [])
        rules.write_text(json.dumps({"schema_version": 1, "kind": rs.RULE_KIND, "confirmed": [{"target": REFUND}]}))
        with self.assertRaises(DomainError):
            rs.read_rule_document(rules)


class EndToEndTests(unittest.TestCase):
    """An agent proposes a rule citing the demo's refund check; a person confirms it; the code changes."""

    def run_flow(self, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            rules = Path(tmp) / "rules.json"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port), "--allow-link-decisions",
                                        "--proposals", str(Path(tmp) / "proposals.jsonl"), "--rules", str(rules), *extra],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({"schema_version": 1, "allow_tools": sorted(at.TOOLS)}))
            tools = at.Tools(at.Source(base, None), at.load_policy(policy),
                             at.Audit(Path(tmp) / "audit.jsonl", "claude", "Dana"), Path(tmp) / "proposals.jsonl")
            made = tools.call("propose_rule", {"target": REFUND, "statement": STATEMENT, "path": "payments.py",
                                               "lines": "25-26", "reason": "apply_refund raises outside the remainder"})
            self.assertEqual((made["status"], made["applied"]), ("PROPOSED", False))
            self.assertEqual(made["citation_check"]["status"], "LINES_FOUND")
            self.assertIn("refundable remainder", made["citation_check"]["quote"])
            self.assertFalse(rules.exists())  # a proposal changes nothing
            for args, status in (({"lines": "40-200"}, "INVALID_ARGUMENTS"), ({"path": ".env"}, "NOT_ALLOWED"),
                                 ({"target": "discovery:service:compose.yml:api"}, "NOT_FOUND"),
                                 ({"statement": "short"}, "INVALID_ARGUMENTS")):
                with self.assertRaises(at.ToolError) as caught:
                    tools.call("propose_rule", {"target": REFUND, "statement": STATEMENT + " x", "path": "payments.py",
                                                "lines": "25", "reason": "r", **args})
                self.assertEqual(caught.exception.status, status, args)
            context = tools.call("get_entity_context", {"id": REFUND})
            self.assertEqual([r["statement"] for r in context["entity"]["proposed_rules"]], [STATEMENT])
            listed = tools.call("list_suggestions", {})["suggestions"]
            self.assertIn(("rule", REFUND), [(s["kind"], s["source"]["id"]) for s in listed])

            with urllib.request.urlopen(base + "/business", timeout=5) as resp:
                token = resp.read().decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]

            def decide(ids, decision):
                request = urllib.request.Request(base + "/api/rules/decision", method="POST", data=json.dumps(
                    {"ids": ids, "decision": decision, "by": "Dana", "note": "read apply_refund"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base, "X-OntologyEX-Token": token})
                try:
                    with urllib.request.urlopen(request, timeout=10) as resp:
                        return resp.status, json.load(resp)
                except urllib.error.HTTPError as exc:
                    return exc.code, json.load(exc)
            self.assertEqual(decide([made["id"]], "confirm")[0], 200)
            confirmed = tools.call("get_entity_context", {"id": REFUND})["entity"]["confirmed_rules"]
            self.assertEqual((confirmed[0]["status"], confirmed[0]["declared_by"], confirmed[0]["proposed_by"]),
                             ("CONFIRMED_BY_PERSON", "Dana", "agent:claude"))
            with self.assertRaises(at.ToolError) as caught:
                tools.call("propose_rule", {"target": REFUND, "statement": STATEMENT, "path": "payments.py",
                                            "lines": "25", "reason": "again"})
            self.assertEqual(caught.exception.status, "ALREADY_DECIDED")

            code = Path(built["repo"]) / "domain/payments.py"
            code.write_text(code.read_text().replace("amount outside refundable remainder", "amount too large"))
            stale = tools.call("get_entity_context", {"id": REFUND})["entity"]["confirmed_rules"][0]
            self.assertEqual(stale["status"], "CONFIRMED_BEFORE_EVIDENCE_CHANGED")
            self.assertEqual(tools.call("list_stale_decisions", {})["total"], 1)
            status, body = decide([stale["id"]], "reconfirm")
            self.assertEqual((status, body["status"]), (200, "RECONFIRMED"))
            self.assertEqual(tools.call("get_entity_context", {"id": REFUND})["entity"]["confirmed_rules"][0]["status"],
                             "CONFIRMED_BY_PERSON")
            self.assertEqual(decide(["no-such-rule"], "confirm")[0], 400)

    def test_python_engine(self):
        self.run_flow()

    @unittest.skipUnless(bridge.engine_binary("ontologyex-view").exists(), "Rust view engine not built")
    def test_rust_engine(self):
        self.run_flow("--view-engine", "rust")

    def test_rule_decisions_need_a_rules_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port), "--allow-link-decisions"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            request = urllib.request.Request(base + "/api/rules/decision", method="POST", data=b"{}",
                                             headers={"Content-Type": "application/json", "Origin": base})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(json.load(caught.exception)["status"], "RULE_DECISIONS_DISABLED")


if __name__ == "__main__":
    unittest.main()
