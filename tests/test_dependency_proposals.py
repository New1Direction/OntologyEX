"""Agent dependency proposals: the same queue, policy and audit as links; only a person confirms."""
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
import dependency_suggest as ds
import entity_context as ec
import native_view_bridge as bridge
from policy_check import DomainError

spec = importlib.util.spec_from_file_location("ontologyex_dependency_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

WORKER, LEDGER, QUEUE, WEB, API = (f"discovery:service:compose.yml:{n}" for n in ("worker", "ledger", "queue", "web", "api"))
AT = "2026-10-29T10:00:00Z"


def proposal(source, target, by="agent:claude", reason="worker.py posts refunds to the ledger"):
    return {"kind": ds.PROPOSAL_KIND, "id": ds.suggestion_id(source, target), "at": AT, "proposed_by": by,
            "on_behalf_of": "Dana", "source": source, "target": target, "reason": reason}


def state():
    nodes = [{"id": i, "label": i.rsplit(":", 1)[1], "kind": "declared_service"} for i in (WORKER, LEDGER, QUEUE, WEB, API)]
    return {"nodes": nodes, "edges": [{"source": WORKER, "target": QUEUE, "relation": "DEPENDS_ON"}]}


class ProposalTests(unittest.TestCase):
    def test_valid_proposals(self):
        self.assertTrue(ds.valid_proposal(proposal(WORKER, LEDGER)))
        for bad in ({**proposal(WORKER, LEDGER), "id": "forged"}, proposal(WORKER, WORKER),
                    proposal(WORKER, LEDGER, by="Dana"), {**proposal(WORKER, LEDGER), "at": "yesterday"},
                    proposal("not a component", LEDGER), {**proposal(WORKER, LEDGER), "extra": 1},
                    proposal(WORKER, LEDGER, reason="")):
            self.assertFalse(ds.valid_proposal(bad), bad)

    def test_merge_adds_weak_suggestions_and_never_resurfaces_decided_or_declared_pairs(self):
        traced = {"suggestions": [{"id": ds.suggestion_id(WEB, API), "source": WEB, "target": API,
                                   "evidence": {"caller_services": ["w -> a"], "calls": 40, "errors": 0,
                                                "first_seen": AT, "last_seen": AT},
                                   "strength": "STRONG", "status": "PROPOSED",
                                   "signals": [{"signal": "OBSERVED_CALLS", "strength": "STRONG", "detail": "40 calls"}]}]}
        rejected = [{"source": LEDGER, "target": QUEUE}]
        merged = ds.merge_proposals(traced, state(), [], rejected, [
            proposal(WORKER, LEDGER), proposal(WEB, API), proposal(LEDGER, QUEUE), proposal(WORKER, QUEUE),
            proposal(WORKER, "discovery:service:compose.yml:gone")] + [proposal(WORKER, LEDGER)] * 5)
        rows = {(s["source"], s["target"]): s for s in merged["suggestions"]}
        self.assertEqual(set(rows), {(WEB, API), (WORKER, LEDGER)})
        self.assertEqual(rows[(WEB, API)]["strength"], "STRONG")  # an agent's word never raises strength
        self.assertEqual([s["signal"] for s in rows[(WEB, API)]["signals"]], ["OBSERVED_CALLS", "AGENT_PROPOSAL"])
        mine = rows[(WORKER, LEDGER)]
        self.assertEqual((mine["strength"], mine["status"], mine["proposed_by"]), ("WEAK", "PROPOSED", "agent:claude"))
        self.assertEqual(set(mine["evidence"]), ds.AGENT_EVIDENCE_KEYS)  # the reason, never invented trace counts
        self.assertEqual(len(mine["signals"]), 3)
        self.assertEqual(merged["agent_proposals"]["not_shown"], 3)  # rejected, declared, unknown component
        self.assertEqual(len(traced["suggestions"][0]["signals"]), 1)  # the input is not mutated

    def test_a_confirmed_proposal_becomes_a_depends_on_edge_marked_as_proposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deps.json"
            merged = ds.merge_proposals({"suggestions": []}, state(), [], [], [proposal(WORKER, LEDGER)])
            ds.apply_decisions(path, "Dana", "checked worker.py", confirm=merged["suggestions"])
            confirmed, _ = ds.read_dependency_document(path)
            self.assertEqual(confirmed[0]["evidence"]["reason"], "worker.py posts refunds to the ledger")
            topology = {"nodes": state()["nodes"], "edges": [], "source_evidence": []}
            edge = ds.with_confirmed(topology, confirmed, "deps.json")["edges"][0]
            self.assertEqual((edge["relation"], edge["state"]), ("DEPENDS_ON", ds.AGENT_STATE))
            path.write_text(json.dumps({"schema_version": 1, "kind": ds.DEPENDENCY_KIND, "confirmed": [
                {**confirmed[0], "evidence": {**confirmed[0]["evidence"], "proposed_by": "Dana"}}]}))
            with self.assertRaises(DomainError):  # only an agent's proposal carries agent evidence
                ds.read_dependency_document(path)

    def test_the_proposals_file_holds_links_and_dependencies_side_by_side(self):
        import link_suggest as ls
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proposals.jsonl"
            ds.append_proposal(path, proposal(WORKER, LEDGER))
            link = {"kind": ls.PROPOSAL_KIND, "id": ls.suggestion_id("s", "b", "c"), "at": AT, "proposed_by": "agent:a",
                    "on_behalf_of": "Dana", "skill": "s", "binding": "b", "component": "c", "reason": "r"}
            ls.append_proposal(path, link)
            self.assertEqual([r["source"] for r in ds.read_proposals(path)], [WORKER])
            self.assertEqual([r["binding"] for r in ls.read_proposals(path)], ["b"])
            with self.assertRaises(DomainError):
                ds.append_proposal(path, proposal(WORKER, WORKER))


class ContextTests(unittest.TestCase):
    def bundle(self):
        return {"state": {"execution_authorized": False,
                          "nodes": [{"id": i, "kind": "declared_service", "label": i.rsplit(":", 1)[1]} for i in (WORKER, LEDGER)],
                          "edges": [], "source_evidence": []},
                "business": {"dependency_suggestions": {"suggestions": [
                    {"id": "d1", "source": WORKER, "target": LEDGER, "strength": "WEAK", "proposed_by": "agent:claude",
                     "evidence": {"proposed_by": "agent:claude", "on_behalf_of": "Dana", "reason": "r", "proposed_at": AT},
                     "signals": [{"signal": "AGENT_PROPOSAL", "detail": "agent:claude for Dana: r"}]}]}},
                "link_history": [], "dependency_history": []}

    def test_an_open_proposal_is_inferred_and_names_the_agent(self):
        rel = ec.context(self.bundle(), WORKER)["relationships"][0]
        self.assertEqual((rel["relation"], rel["standing"], rel["basis"]), ("DEPENDS_ON", "INFERRED", "AGENT_PROPOSAL_NOT_CONFIRMED"))
        self.assertEqual(rel["suggestion"]["proposed_by"], "agent:claude")
        self.assertEqual(rel["suggestion"]["signals"], ["agent:claude for Dana: r"])

    def test_trace_suggestions_show_their_own_signal_text(self):
        data = self.bundle()
        data["business"]["dependency_suggestions"]["suggestions"][0] = {
            "id": "d2", "source": WORKER, "target": LEDGER, "strength": "STRONG",
            "evidence": {"caller_services": [], "calls": 40, "errors": 1, "first_seen": AT, "last_seen": AT},
            "signals": [{"signal": "OBSERVED_CALLS", "detail": "worker called ledger 40 times in traces (1 failed)"}]}
        rel = ec.context(data, WORKER)["relationships"][0]
        self.assertEqual((rel["relation"], rel["basis"]), ("CALLS", "SEEN_IN_TRACES_NOT_CONFIRMED"))
        self.assertEqual(rel["suggestion"]["signals"], ["worker called ledger 40 times in traces (1 failed)"])
        self.assertEqual(rel["suggestion"]["proposed_by"], "trace observation")

    def test_a_confirmed_proposal_is_confirmed_by_the_person_and_keeps_the_agent(self):
        data = self.bundle()
        data["business"]["dependency_suggestions"] = {"suggestions": []}
        data["state"]["edges"] = [{"source": WORKER, "target": LEDGER, "relation": "DEPENDS_ON", "state": ds.AGENT_STATE}]
        data["dependency_history"] = [{"at": AT, "by": "Dana", "action": "confirm", "note": "checked",
                                       "row": {"source": WORKER, "target": LEDGER}}]
        data["agent_dependency_proposals"] = [{"source": WORKER, "target": LEDGER, "proposed_by": "agent:claude"}]
        rel = ec.context(data, WORKER)["relationships"][0]
        self.assertEqual(rel["standing"], "CONFIRMED")
        self.assertEqual(rel["decision"], {"by": "Dana", "at": AT, "note": "checked", "proposed_by": "agent:claude"})
        relied = ec.context(data, LEDGER)["relied_on_by"]
        self.assertEqual([(r["id"], r["standing"]) for r in relied], [(WORKER, "CONFIRMED")])


class EndToEndTests(unittest.TestCase):
    """An agent proposes over the control room; a person confirms one and rejects another."""

    def serve(self, built, tmp, *extra):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([*built["command"], "--port", str(port), "--allow-link-decisions",
                                    "--proposals", str(Path(tmp) / "proposals.jsonl"),
                                    "--dependencies", str(Path(tmp) / "dependencies.json"), *extra],
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

    def run_flow(self, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            base = self.serve(built, tmp, *extra)
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({"schema_version": 1, "allow_tools": sorted(at.TOOLS)}))
            audit = Path(tmp) / "audit.jsonl"
            tools = at.Tools(at.Source(base, None), at.load_policy(policy), at.Audit(audit, "claude", "Dana"),
                             Path(tmp) / "proposals.jsonl")
            deps_file = Path(tmp) / "dependencies.json"
            one = tools.call("propose_dependency", {"source": WORKER, "target": LEDGER,
                                                    "reason": "worker.py posts refunds to the ledger"})
            two = tools.call("propose_dependency", {"source": WEB, "target": QUEUE, "reason": "web enqueues jobs"})
            self.assertEqual((one["status"], one["applied"]), ("PROPOSED", False))
            self.assertEqual(tools.call("propose_dependency", {"source": WORKER, "target": LEDGER, "reason": "again"})["status"],
                             "ALREADY_PROPOSED")
            for args, status in (({"source": WORKER, "target": QUEUE}, "ALREADY_DECLARED"),
                                 ({"source": WORKER, "target": WORKER}, "NOT_FOUND"),
                                 ({"source": WORKER, "target": "discovery:service:compose.yml:gone"}, "NOT_FOUND")):
                with self.assertRaises(at.ToolError) as caught:
                    tools.call("propose_dependency", {**args, "reason": "x"})
                self.assertEqual(caught.exception.status, status, args)
            self.assertFalse(deps_file.exists())  # proposals change nothing
            with urllib.request.urlopen(base + "/api/business", timeout=10) as resp:
                business = json.load(resp)
            rows = {s["id"]: s for s in business["dependency_suggestions"]["suggestions"]}
            self.assertEqual(rows[one["id"]]["proposed_by"], "agent:claude")
            with urllib.request.urlopen(base + "/business", timeout=5) as resp:
                token = resp.read().decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]

            def decide(ids, decision):
                request = urllib.request.Request(base + "/api/dependencies/decision", method="POST", data=json.dumps(
                    {"ids": ids, "decision": decision, "by": "Dana", "note": "read worker.py"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base, "X-OntologyEX-Token": token})
                with urllib.request.urlopen(request, timeout=10) as resp:
                    return json.load(resp)
            self.assertEqual(len(decide([one["id"]], "confirm")["applied"]), 1)
            self.assertEqual(len(decide([two["id"]], "reject")["applied"]), 1)
            confirmed, rejected = ds.read_dependency_document(deps_file)
            self.assertEqual((confirmed[0]["evidence"]["proposed_by"], confirmed[0]["declared_by"]), ("agent:claude", "Dana"))
            self.assertEqual(len(rejected), 1)
            found = tools.call("get_entity_context", {"id": WORKER})
            edge = next(r for r in found["relationships"] if r["other"]["id"] == LEDGER and r["relation"] == "DEPENDS_ON")
            self.assertEqual((edge["standing"], edge["basis"]), ("CONFIRMED", ds.AGENT_STATE))
            self.assertEqual((edge["decision"]["by"], edge["decision"]["proposed_by"]), ("Dana", "agent:claude"))
            impact = tools.call("analyze_impact", {"id": LEDGER})["relied_on_by"]
            self.assertIn((WORKER, "CONFIRMED"), [(r["id"], r["standing"]) for r in impact])
            with self.assertRaises(at.ToolError) as caught:
                tools.call("propose_dependency", {"source": WEB, "target": QUEUE, "reason": "really"})
            self.assertEqual(caught.exception.status, "REJECTED_BY_PERSON")
            with self.assertRaises(at.ToolError) as caught:
                tools.call("propose_dependency", {"source": WORKER, "target": LEDGER, "reason": "again"})
            self.assertEqual(caught.exception.status, "ALREADY_DECIDED")
            audit_rows = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertTrue(all(r["kind"] in ("ADDITIVE", "READ_ONLY") for r in audit_rows))
            self.assertIn([WORKER, LEDGER], [r["entity_ids"] for r in audit_rows if r["tool"] == "propose_dependency"])

    def test_python_engine(self):
        self.run_flow()

    @unittest.skipUnless(bridge.engine_binary("ontologyex-view").exists(), "Rust view engine not built")
    def test_rust_engine(self):
        self.run_flow("--view-engine", "rust")


if __name__ == "__main__":
    unittest.main()
