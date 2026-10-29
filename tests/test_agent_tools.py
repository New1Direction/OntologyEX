"""Agent tools: kinds, policy, redaction, audit, MCP framing, and proposals a person decides."""
import importlib.util
import io
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
sys.path.insert(0, str(ROOT / "tests"))
import agent_tools as at
import business_view as bv
import entity_context as ec
import link_suggest as ls
from test_entity_context import API, DB, PAY, WEB, bundle

spec = importlib.util.spec_from_file_location("ontologyex_agent_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ontologyex-agent-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.bundle_path = self.tmp / "bundle.json"
        self.bundle_path.write_text(json.dumps(bundle()))
        self.audit_path = self.tmp / "audit.jsonl"

    def tools(self, policy=None, proposals=None, source=None):
        policy_path = None
        if policy is not None:
            policy_path = self.tmp / "policy.json"
            policy_path.write_text(json.dumps({"schema_version": 1, **policy}))
        return at.Tools(source or at.Source(None, self.bundle_path), at.load_policy(policy_path),
                        at.Audit(self.audit_path, "claude", "Dana"), proposals)

    def audit(self):
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]


class KindTests(unittest.TestCase):
    def test_every_tool_has_a_kind_and_none_destroys(self):
        self.assertEqual({t["kind"] for t in at.TOOLS.values()}, {at.READ_ONLY, at.ADDITIVE})
        for name, tool in at.TOOLS.items():
            hints = at.annotations(name)
            self.assertFalse(hints["destructiveHint"], name)
            self.assertEqual(hints["readOnlyHint"], tool["kind"] == at.READ_ONLY, name)
            self.assertFalse(tool["input"]["additionalProperties"], name)
        self.assertEqual([n for n, t in at.TOOLS.items() if t["kind"] == at.ADDITIVE], ["propose_link"])


class PolicyTests(Harness):
    def test_default_policy_reads_but_does_not_propose(self):
        tools = self.tools()
        self.assertEqual(tools.call("search_entities", {"query": "pay"})["results"][0]["id"], PAY)
        with self.assertRaises(at.ToolError) as caught:
            tools.call("propose_link", {"skill": "s", "binding": "b.pay", "component": DB, "reason": "reads db"})
        self.assertEqual(caught.exception.status, "NOT_ALLOWED")
        self.assertIn("allow_tools includes propose_link", str(caught.exception))
        self.assertEqual([(r["tool"], r["status"]) for r in self.audit()],
                         [("search_entities", "OK"), ("propose_link", "NOT_ALLOWED")])

    def test_bad_policies_fail_closed(self):
        path = self.tmp / "p.json"
        for body in ({"schema_version": 1, "allow_tools": ["delete_everything"]},
                     {"schema_version": 1, "allow": []}, {"allow_tools": []},
                     {"schema_version": 1, "deny_paths": "x"}):
            path.write_text(json.dumps(body))
            with self.assertRaises(at.ToolError):
                at.load_policy(path)

    def test_arguments_are_checked(self):
        tools = self.tools()
        for args in ({}, {"id": PAY, "extra": 1}, {"id": 5}, {"id": "a\x00b"}, {"id": "x" * 1025}):
            with self.assertRaises(at.ToolError) as caught:
                tools.call("get_entity_context", args)
            self.assertEqual(caught.exception.status, "INVALID_ARGUMENTS")
        with self.assertRaises(at.ToolError) as caught:
            tools.call("get_entity_context", {"id": "missing"})
        self.assertEqual(caught.exception.status, "NOT_FOUND")
        with self.assertRaises(at.ToolError) as caught:
            tools.call("no_such_tool", {})
        self.assertEqual(caught.exception.status, "UNKNOWN_TOOL")

    def test_secret_files_are_always_withheld(self):
        found = self.tools().call("get_entity_context", {"id": WEB})
        paths = [e.get("path") for e in found["source_evidence"]]
        self.assertNotIn(".env", json.dumps(found))
        self.assertIn(None, paths)
        self.assertTrue(any(e.get("withheld") for e in found["source_evidence"]))

    def test_denied_entities_are_invisible(self):
        tools = self.tools({"allow_tools": ["search_entities", "get_entity_context", "analyze_impact"],
                            "deny_entities": ["svc:db"]})
        self.assertEqual(tools.call("search_entities", {"query": "db"})["results"], [])
        with self.assertRaises(at.ToolError) as caught:
            tools.call("get_entity_context", {"id": DB})
        self.assertEqual(caught.exception.status, "NOT_ALLOWED")
        text = json.dumps(tools.call("get_entity_context", {"id": API}))
        self.assertNotIn(DB, text)
        self.assertIn(at.WITHHELD, text)
        with self.assertRaises(at.ToolError):
            tools.call("explain_relationship", {"from": PAY, "to": API})  # not in allow_tools


class AuditTests(Harness):
    def test_each_call_is_recorded_with_actor_and_principal(self):
        tools = self.tools()
        tools.call("get_entity_context", {"id": PAY})
        tools.call("analyze_impact", {"id": DB})
        rows = self.audit()
        self.assertEqual(len({r["id"] for r in rows}), 2)
        for row in rows:
            self.assertEqual((row["actor"], row["on_behalf_of"], row["on_behalf_of_verified"]),
                             ("agent:claude", "Dana", False))
            self.assertEqual(len(row["args_sha256"]), 64)
            self.assertNotIn("query", row)
        self.assertEqual(rows[1]["entity_ids"], [DB])

    def test_no_audit_no_answer(self):
        self.audit_path.mkdir()  # an audit path that cannot be appended to
        tools = self.tools()
        with self.assertRaises(OSError):
            tools.call("get_entity_context", {"id": PAY})
        reply = at.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "get_entity_context", "arguments": {"id": PAY}}})
        self.assertTrue(reply["result"]["isError"])
        self.assertNotIn("structuredContent", reply["result"])


class ProposalTests(Harness):
    def proposer(self):
        self.proposals = self.tmp / "proposals.jsonl"
        return self.tools({"allow_tools": ["propose_link", "list_suggestions"]}, self.proposals)

    def test_proposal_is_appended_once_and_only_proposed(self):
        tools = self.proposer()
        args = {"skill": "s", "binding": "b.pay", "component": WEB, "reason": "web.py calls take_payment"}
        first = tools.call("propose_link", args)
        self.assertEqual((first["status"], first["applied"]), ("PROPOSED", False))
        self.assertEqual(first["id"], ls.suggestion_id("s", "b.pay", WEB))
        self.assertEqual(tools.call("propose_link", args)["status"], "ALREADY_PROPOSED")
        rows = ls.read_proposals(self.proposals)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["proposed_by"], rows[0]["on_behalf_of"]), ("agent:claude", "Dana"))

    def test_proposals_are_checked_against_the_model(self):
        tools = self.proposer()
        cases = [({"binding": "b.none"}, "NOT_FOUND"), ({"component": "svc:gone"}, "NOT_FOUND"),
                 ({"component": API}, "ALREADY_DECIDED")]
        for change, status in cases:
            args = {"skill": "s", "binding": "b.pay", "component": WEB, "reason": "because", **change}
            with self.assertRaises(at.ToolError) as caught:
                tools.call("propose_link", args)
            self.assertEqual(caught.exception.status, status, change)
        rejected = bundle()
        rejected["business"]["link_decided"].append({"skill": "s", "binding": "b.pay", "component": WEB,
                                                     "decision": "reject", "declared_by": "Ana"})
        self.bundle_path.write_text(json.dumps(rejected))
        with self.assertRaises(at.ToolError) as caught:
            tools.call("propose_link", {"skill": "s", "binding": "b.pay", "component": WEB, "reason": "again"})
        self.assertEqual(caught.exception.status, "REJECTED_BY_PERSON")
        self.assertIn("Ana rejected", str(caught.exception))
        self.assertFalse(self.proposals.exists())

    def test_proposals_need_a_proposals_file(self):
        tools = self.tools({"allow_tools": ["propose_link"]})
        with self.assertRaises(at.ToolError) as caught:
            tools.call("propose_link", {"skill": "s", "binding": "b.pay", "component": WEB, "reason": "x"})
        self.assertIn("--proposals FILE", str(caught.exception))

    def test_merge_never_raises_strength_or_resurfaces_rejections(self):
        data = {"bindings": [{"skill": "s", "binding": "b"}],
                "components": [{"id": c, "label": c, "kind": "declared_service"} for c in ("x", "y", "z")],
                "linked": [["s", "b", "z"]], "rejected": [["s", "b", "y"]]}
        ranked = {"suggestions": [{"id": ls.suggestion_id("s", "b", "x"), "skill": "s", "binding": "b",
                                   "component": "x", "strength": "STRONG", "signals": [{"signal": "SAME_SOURCE_FILE",
                                                                                       "strength": "STRONG"}],
                                   "status": "PROPOSED"}]}

        def proposal(component, reason="r"):
            return {"kind": ls.PROPOSAL_KIND, "id": ls.suggestion_id("s", "b", component), "at": "t",
                    "proposed_by": "agent:a", "on_behalf_of": "Dana", "skill": "s", "binding": "b",
                    "component": component, "reason": reason}
        merged = ls.merge_proposals(ranked, data, [proposal("x"), proposal("y"), proposal("z")] + [proposal("x")] * 5)
        self.assertEqual([(r["component"], r["strength"]) for r in merged["suggestions"]], [("x", "STRONG")])
        self.assertEqual(sum(s["signal"] == "AGENT_PROPOSAL" for s in merged["suggestions"][0]["signals"]), 3)
        self.assertEqual(merged["agent_proposals"]["not_shown"], 2)
        self.assertEqual(len(ranked["suggestions"][0]["signals"]), 1)  # the input is not mutated

    def test_malformed_proposal_lines_are_skipped(self):
        path = self.tmp / "p.jsonl"
        good = {"kind": ls.PROPOSAL_KIND, "id": ls.suggestion_id("s", "b", "x"), "at": "t", "proposed_by": "agent:a",
                "on_behalf_of": "Dana", "skill": "s", "binding": "b", "component": "x", "reason": "r"}
        lines = [json.dumps(good), "{torn", json.dumps({**good, "proposed_by": "Ana"}),
                 json.dumps({**good, "id": "forged"}), json.dumps({**good, "extra": 1})]
        path.write_text("\n".join(lines) + "\n")
        self.assertEqual(ls.read_proposals(path), [good])

    def test_a_long_proposals_file_is_read_from_its_newest_end(self):
        path = self.tmp / "p.jsonl"
        row = {"kind": ls.PROPOSAL_KIND, "id": ls.suggestion_id("s", "b", "x"), "at": "t",
               "proposed_by": "agent:a", "on_behalf_of": "Dana", "skill": "s", "binding": "b", "component": "x",
               "reason": "r" * 400}
        path.write_text((json.dumps(row) + "\n") * 12000)  # about 6 MB
        rows = ls.read_proposals(path)
        self.assertEqual(len(rows), ls.MAX_PROPOSALS)
        self.assertTrue(all(r == row for r in rows))


class McpTests(Harness):
    def test_protocol(self):
        tools = self.tools()
        init = at.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2024-11-05"}})["result"]
        self.assertEqual(init["protocolVersion"], "2024-11-05")
        self.assertEqual(at.handle(tools, {"jsonrpc": "2.0", "id": 9, "method": "initialize",
                                           "params": {"protocolVersion": "1999"}})["result"]["protocolVersion"],
                         at.PROTOCOL_VERSIONS[0])
        self.assertIsNone(at.handle(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listed = at.handle(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertNotIn("propose_link", [t["name"] for t in listed])  # not offered when not allowed
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in listed))
        ok = at.handle(tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "get_entity_context", "arguments": {"id": PAY}}})["result"]
        self.assertFalse(ok["isError"])
        self.assertEqual(ok["structuredContent"]["entity"]["id"], PAY)
        self.assertEqual(json.loads(ok["content"][0]["text"]), ok["structuredContent"])
        bad = at.handle(tools, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                "params": {"name": "get_entity_context", "arguments": {"id": "nope"}}})["result"]
        self.assertTrue(bad["isError"])
        self.assertEqual(bad["structuredContent"]["status"], "NOT_FOUND")
        self.assertEqual(at.handle(tools, {"jsonrpc": "2.0", "id": 5, "method": "x"})["error"]["code"], -32601)
        self.assertEqual(at.handle(tools, ["not", "a", "request"])["error"]["code"], -32600)

    def test_stdio_framing(self):
        out = io.StringIO()
        at.serve(self.tools(), io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n'), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(replies[1]["error"]["code"], -32700)

    def test_sources_are_loopback_only(self):
        for url in ("http://example.com:8791", "https://127.0.0.1:8791", "http://127.0.0.1:8791/x",
                    "http://user@127.0.0.1:1"):
            with self.assertRaises(at.ToolError):
                at.Source(url, None)
        with self.assertRaises(at.ToolError):
            at.Source(None, None)


class EndToEndTests(unittest.TestCase):
    """An agent proposes over the control room; a person confirms with the page's token."""

    def test_agent_proposes_person_decides(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", unlinked=True)
            proposals, audit = Path(tmp) / "proposals.jsonl", Path(tmp) / "audit.jsonl"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port), "--allow-link-decisions",
                                        "--proposals", str(proposals)],
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
            tools = at.Tools(at.Source(base, None), at.load_policy(policy), at.Audit(audit, "claude", "Dana"),
                             proposals)
            worker, queue = "discovery:service:compose.yml:worker", "discovery:service:compose.yml:queue"
            links_before = Path(built["links"]).read_text()
            proposed = tools.call("propose_link", {"skill": "payments-domain", "binding": "payments.apply_refund",
                                                   "component": worker, "reason": "worker.py runs apply_refund"})
            tools.call("propose_link", {"skill": "payments-domain", "binding": "payments.Payment",
                                        "component": queue, "reason": "payments are queued"})
            self.assertEqual(Path(built["links"]).read_text(), links_before)  # proposals change no link
            with urllib.request.urlopen(base + "/api/business", timeout=5) as resp:
                business = json.load(resp)
            row = next(s for s in business["link_suggestions"]["suggestions"] if s["id"] == proposed["id"])
            self.assertEqual((row["proposed_by"], row["strength"], row["status"]), ("agent:claude", "WEAK", "PROPOSED"))
            listed = tools.call("list_suggestions", {"id": worker})["suggestions"]
            self.assertEqual([s["proposed_by"] for s in listed], ["agent:claude"])

            with urllib.request.urlopen(base + "/business", timeout=5) as resp:
                token = resp.read().decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]

            def decide(ids, decision):
                request = urllib.request.Request(base + "/api/links/decision", method="POST", data=json.dumps(
                    {"ids": ids, "decision": decision, "by": "Dana", "note": "checked worker.py"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base, "X-OntologyEX-Token": token})
                with urllib.request.urlopen(request, timeout=5) as resp:
                    return json.load(resp)
            queued = ls.suggestion_id("payments-domain", "payments.Payment", queue)
            self.assertEqual(len(decide([proposed["id"]], "confirm")["applied"]), 1)
            self.assertEqual(len(decide([queued], "reject")["applied"]), 1)

            found = tools.call("get_entity_context", {"id": "payments-domain:action:IssueRefund"})
            confirmed = next(r for r in found["relationships"] if r["other"]["id"] == worker)
            self.assertEqual((confirmed["standing"], confirmed["decision"]["by"], confirmed["decision"]["proposed_by"]),
                             ("CONFIRMED", "Dana", "agent:claude"))
            self.assertEqual(found["recent_changes"][0]["by"], "Dana")
            links, rejected = bv.read_link_document(Path(built["links"]))
            self.assertIn(worker, [l["component"] for l in links])
            with self.assertRaises(at.ToolError) as caught:
                tools.call("propose_link", {"skill": "payments-domain", "binding": "payments.Payment",
                                            "component": queue, "reason": "really"})
            self.assertEqual(caught.exception.status, "REJECTED_BY_PERSON")
            statuses = [json.loads(line)["status"] for line in audit.read_text().splitlines()]
            self.assertEqual(statuses, ["OK", "OK", "OK", "OK", "REJECTED_BY_PERSON"])


if __name__ == "__main__":
    unittest.main()
