"""Guardrails: an agent's tool call held to the business rules before it runs (Claude Code PreToolUse)."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import command_center
import guardrail as g

spec = importlib.util.spec_from_file_location("ontologyex_guard_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

TOOL = "mcp__payments__issue_refund"
FIELDS = {"amount_minor": "amount", "captured_minor": "payment.captured", "refunded_minor": "payment.refunded",
          "payment_status": "payment.status", "requester_authorized": "approved"}
CALL = {"amount": 500, "payment": {"captured": 1000, "refunded": 200, "status": "captured"}, "approved": True}


class Guarded(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.built = demo.build(self.tmp / "demo")
        self.repo = Path(self.built["repo"])
        self.skill = Path(self.built["skill"])
        self.configure()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def configure(self, guards=None, **extra):
        (self.repo / ".ontologyex").mkdir(exist_ok=True)
        (self.repo / g.CONFIG).write_text(json.dumps({
            "schema_version": 1, "skills": ["../payments-domain"], **extra,
            "guards": guards if guards is not None else [
                {"tool": TOOL, "skill": "payments-domain", "task": "IssueRefund", "fields": FIELDS}]}))

    def decide(self, call, tool=TOOL):
        return g.decide(self.repo, tool, call)


class DecisionTests(Guarded):
    def test_pass_is_no_decision_and_never_allow(self):
        found = self.decide(CALL)
        self.assertEqual(found["decision"], "none")
        self.assertEqual(found["checked"][0]["status"], "CHECKS_PASS")
        self.assertEqual(g.hook(self.repo, json.dumps({"tool_name": TOOL, "tool_input": CALL})), "")
        self.assertEqual(self.decide(CALL, tool="mcp__payments__list_payments"),
                         {"decision": "none", "tool": "mcp__payments__list_payments", "guarded": False})

    def test_a_broken_rule_denies_naming_it(self):
        found = self.decide({**CALL, "amount": 900})  # 1000 captured, 200 refunded: 800 left
        self.assertEqual((found["decision"], found["checked"][0]["failed"]), ("deny", ["remaining-balance"]))
        self.assertIn("must not exceed captured_minor minus refunded_minor", found["reason"])
        out = json.loads(g.hook(self.repo, json.dumps({"tool_name": TOOL, "tool_input": {**CALL, "approved": False}})))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("authorization-required", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_what_cannot_be_checked_goes_to_a_person(self):
        missing = self.decide({"amount": 500})
        self.assertEqual(missing["decision"], "ask")
        self.assertIn("captured_minor (payment.captured) is not in the call", missing["reason"])
        for bad in (500.0, "500", None, [500]):
            self.assertEqual(self.decide({**CALL, "amount": bad})["decision"], "ask", bad)
        self.configure([{"tool": "mcp__ops__approve_*", "skill": "payments-domain", "task": "ApproveException",
                         "fields": {}}])
        open_question = self.decide({}, tool="mcp__ops__approve_exception")
        self.assertEqual(open_question["decision"], "ask")
        self.assertIn("needs a person for exception-authority", open_question["reason"])
        self.configure([{"tool": TOOL, "skill": "billing", "task": "IssueRefund", "fields": FIELDS}])
        self.assertIn("is not among the guard's skills", self.decide(CALL)["reason"])

    def test_changed_code_under_the_rules_asks_first(self):
        code = self.repo / "domain/payments.py"
        signature = "def apply_refund(payment: Payment, amount_minor: int)"
        code.write_text(code.read_text().replace(signature, signature.replace(")", ", force=False)")))
        found = self.decide(CALL)
        self.assertEqual(found["decision"], "ask")
        self.assertIn("source drift", found["reason"])
        self.configure(drift=False)
        self.assertEqual(self.decide(CALL)["decision"], "none")

    def test_a_guard_that_cannot_be_read_fails_to_a_person(self):
        (self.repo / g.CONFIG).write_text('{"schema_version": 1, "guards": [{"tool": "x"}]}')
        out = json.loads(g.hook(self.repo, json.dumps({"tool_name": TOOL, "tool_input": CALL})))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.configure(on_error="deny")
        out = json.loads(g.hook(self.repo, "not json"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        for bad in ({"schema_version": 2}, {"schema_version": 1, "extra": 1}, {"schema_version": 1, "on_error": "allow"},
                    {"schema_version": 1, "guards": [{"tool": "a b", "skill": "s", "task": "t", "fields": {}}]},
                    {"schema_version": 1, "guards": [{"tool": "t", "skill": "s", "task": "t",
                                                      "fields": {"x": "a..b"}}]},
                    {"schema_version": 1, "guards": [{"tool": "t", "skill": "s", "task": "t",
                                                      "fields": {"x": {"value": 1.5}}}]}):
            (self.repo / g.CONFIG).write_text(json.dumps(bad))
            with self.assertRaises(g.GuardError, msg=bad):
                g.load(self.repo)

    def test_the_audit_keeps_decisions_not_values(self):
        g.hook(self.repo, json.dumps({"tool_name": TOOL, "tool_input": {**CALL, "amount": 777777}}))
        g.hook(self.repo, json.dumps({"tool_name": TOOL, "tool_input": CALL}))
        g.hook(self.repo, json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        text = (self.repo / g.AUDIT).read_text()
        self.assertNotIn("777777", text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([r["decision"] for r in rows], ["deny", "none"])  # unguarded calls are not logged
        found = g.summary(self.repo, datetime.now(timezone.utc))
        self.assertEqual(found["calls"], {"deny": 1, "ask": 0, "none": 1})
        self.assertEqual(found["denied_by_rule"][0]["rule"], "remaining-balance")
        items = command_center.report({"state": {"nodes": [], "edges": []}, "guardrails": found})["items"]
        blocked = next(i for i in items if i["kind"] == "AGENT_BLOCKED")
        self.assertIn("remaining-balance", blocked["title"])
        self.assertEqual(blocked["category"], "Decisions")


class HookTests(Guarded):
    def test_claude_code_runs_it_as_a_command(self):
        def run(event):
            return subprocess.run([sys.executable, str(SCRIPTS / "guardrail.py"), "hook", "--repo", str(self.repo)],
                                  input=json.dumps(event), capture_output=True, text=True, timeout=60)
        denied = run({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": TOOL,
                      "tool_input": {**CALL, "payment": {**CALL["payment"], "status": "refunded"}}, "cwd": "/"})
        self.assertEqual(denied.returncode, 0)
        self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        passed = run({"hook_event_name": "PreToolUse", "tool_name": TOOL, "tool_input": CALL})
        self.assertEqual((passed.returncode, passed.stdout), (0, ""))

    def test_install_keeps_other_settings_and_remove_takes_only_ours(self):
        settings = self.repo / g.SETTINGS
        settings.parent.mkdir(parents=True)
        theirs = {"type": "command", "command": "./lint.sh"}
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [theirs]}]}}))
        self.assertEqual(g.install(self.repo)["matcher"], TOOL)
        g.install(self.repo)  # again: refreshed, not duplicated
        written = json.loads(settings.read_text())
        self.assertEqual(written["permissions"], {"allow": ["Bash(ls)"]})
        groups = written["hooks"]["PreToolUse"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["hooks"], [theirs])
        self.assertIn("guardrail.py hook --repo", groups[1]["hooks"][0]["command"])
        self.assertEqual(g.remove(self.repo)["status"], "REMOVED")
        self.assertEqual(json.loads(settings.read_text())["hooks"]["PreToolUse"], [{"matcher": "Bash", "hooks": [theirs]}])
        self.assertEqual(g.remove(self.repo)["status"], "NOT_INSTALLED")

    def test_matchers_and_init(self):
        self.assertEqual(g.matcher([{"tool": "mcp__a__b"}, {"tool": "mcp__a__c"}]), "mcp__a__b|mcp__a__c")
        self.assertEqual(g.matcher([{"tool": "mcp__pay__refund_*"}, {"tool": "x.y"}]), r"^(?:mcp__pay__refund_.*|x\.y)$")
        (self.repo / g.CONFIG).unlink()
        found = g.init(self.repo, [self.skill])
        self.assertEqual(found["guards"], 1)  # ApproveException has no inputs to check
        guard = json.loads((self.repo / g.CONFIG).read_text())["guards"][0]
        self.assertEqual((guard["tool"], guard["task"]), ("mcp__SERVER__issue_refund", "IssueRefund"))
        self.assertEqual(guard["fields"]["amount_minor"], "amount_minor")
        g.load(self.repo)  # what init writes is valid
        with self.assertRaises(g.GuardError):
            g.init(self.repo, [self.skill])
        cli = subprocess.run([sys.executable, str(SCRIPTS / "guardrail.py"), "check", "--repo", str(self.repo),
                              "--tool", "mcp__SERVER__issue_refund", "--input", json.dumps({
                                  "amount_minor": 5, "captured_minor": 10, "refunded_minor": 0,
                                  "payment_status": "captured", "requester_authorized": True})],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(cli.stdout)["decision"], "none", cli.stderr)


if __name__ == "__main__":
    unittest.main()
