"""Guardrails: an agent's tool call held to the business rules before it runs (Claude Code PreToolUse)."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


class OutcomeTests(Guarded):
    """An uncertain result is journaled as unknown, and an identical call waits for a person to check."""

    def event(self, kind, call_id, call=CALL, **extra):
        return json.dumps({"hook_event_name": kind, "tool_name": TOOL, "tool_input": call, "tool_use_id": call_id,
                           **extra})

    def pre(self, call_id, call=CALL, at=None):
        out = g.hook(self.repo, self.event("PreToolUse", call_id, call), at)
        return json.loads(out)["hookSpecificOutput"] if out else None

    def test_a_call_with_no_recorded_end_holds_the_identical_one(self):
        self.assertIsNone(self.pre("toolu_1"))  # passes the rules: goes ahead, journaled
        held = self.pre("toolu_2")  # the first never reported back (a timeout, a crash, still running)
        self.assertEqual(held["permissionDecision"], "ask")
        self.assertIn("no known outcome", held["permissionDecisionReason"])
        self.assertIsNone(self.pre("toolu_3", {**CALL, "amount": 400}))  # a different call is not held
        text = (self.repo / g.INTENTS).read_text()
        self.assertNotIn('"amount"', text)  # arguments are fingerprinted, never kept
        g.hook(self.repo, self.event("PostToolUse", "toolu_2", tool_response={"ok": True}))
        self.assertIsNone(self.pre("toolu_4"))  # an identical call completed: the earlier one is settled
        self.assertEqual({r["id"]: r["outcome"] for r in g.outcomes(self.repo, datetime.now(timezone.utc))},
                         {"toolu_1": "RECONCILED", "toolu_2": "DONE", "toolu_3": "UNKNOWN", "toolu_4": "UNKNOWN"})

    def test_how_a_call_ended_decides_whether_it_is_known(self):
        self.pre("a")
        g.hook(self.repo, self.event("PostToolUseFailure", "a", error="insufficient funds", is_interrupt=False))
        self.assertIsNone(self.pre("b"))  # the tool said it failed: known not to have taken effect
        g.hook(self.repo, self.event("PostToolUseFailure", "b", error="upstream request timed out"))
        self.assertEqual(self.pre("c")["permissionDecision"], "ask")  # a timeout: it may have
        g.hook(self.repo, self.event("PermissionDenied", "c"))
        g.hook(self.repo, self.event("PostToolUseFailure", "x-unknown", error="timeout"))  # not journaled: ignored
        found = {r["id"]: r["outcome"] for r in g.outcomes(self.repo, datetime.now(timezone.utc))}
        self.assertEqual(found, {"a": "FAILED", "b": "UNKNOWN", "c": "NOT_RUN"})
        self.assertEqual(g.reconcile(self.repo, TOOL, CALL, "Dana", "checked the ledger: no refund")["calls"], 1)
        self.assertIsNone(self.pre("d"))
        g.hook(self.repo, self.event("PostToolUseFailure", "d", error="boom", is_interrupt=True))
        self.assertEqual(self.pre("e")["permissionDecision"], "ask")  # interrupted: unknown

    def test_a_denied_call_is_not_journaled_and_old_unknowns_expire(self):
        self.assertEqual(self.pre("deny", {**CALL, "amount": 900})["permissionDecision"], "deny")
        self.assertFalse((self.repo / g.INTENTS).exists())
        then = datetime.now(timezone.utc) - g.UNKNOWN_HORIZON - timedelta(hours=1)
        self.pre("old", at=then)
        self.assertIsNone(self.pre("new"))  # beyond the horizon the guard stops holding identical calls

    def test_the_command_center_names_unknown_outcomes(self):
        self.pre("toolu_1")
        found = g.summary(self.repo, datetime.now(timezone.utc))
        self.assertEqual([r["tool"] for r in found["unknown_outcomes"]], [TOOL])
        items = command_center.report({"state": {"nodes": [], "edges": []}, "guardrails": found})["items"]
        item = next(i for i in items if i["kind"] == "OUTCOME_UNKNOWN")
        self.assertEqual((item["category"], item["standing"]), ("Decisions", "SOURCE"))
        self.assertEqual(item["evidence"][0]["source"], "guardrail intent journal")
        cli = subprocess.run([sys.executable, str(SCRIPTS / "guardrail.py"), "reconcile", "--repo", str(self.repo),
                              "--tool", TOOL, "--by", "Dana", "--note", "checked"], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(json.loads(cli.stdout)["calls"], 1, cli.stderr)
        self.assertEqual(g.summary(self.repo, datetime.now(timezone.utc))["unknown_outcomes"], [])


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
        self.assertEqual(sorted(written["hooks"]), sorted(g.EVENTS))  # before, and how each call ended
        self.assertEqual(g.remove(self.repo)["status"], "REMOVED")
        self.assertEqual(json.loads(settings.read_text())["hooks"], {"PreToolUse": [{"matcher": "Bash", "hooks": [theirs]}]})
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
