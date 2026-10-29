"""Entity context: standings, provenance, impact, explanations and the control-room routes."""
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
import entity_context as ec
import native_view_bridge as bridge

spec = importlib.util.spec_from_file_location("ontologyex_context_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

API, DB, WEB = "svc:api", "svc:db", "svc:web"
PAY, REFUND = "s:action:Pay", "s:object:Refund"


def bundle(**extra) -> dict:
    """A small hand-written bundle: web -> api -> db declared, Pay runs on api (confirmed),
    an open suggestion that Pay also runs on db, and a link whose component is gone."""
    return {
        "state": {"execution_authorized": False,
                  "nodes": [{"id": API, "kind": "declared_service", "label": "api", "status": "ok", "evidence_id": "e1"},
                            {"id": DB, "kind": "declared_service", "label": "db", "status": "error", "evidence_id": "e1"},
                            {"id": WEB, "kind": "declared_service", "label": "web", "evidence_id": "e2"}],
                  "edges": [{"source": WEB, "target": API, "relation": "DEPENDS_ON", "evidence_id": "e1"},
                            {"source": API, "target": DB, "relation": "DEPENDS_ON", "evidence_id": "e1"}],
                  "source_evidence": [{"id": "e1", "path": "compose.yml", "sha256": "aa", "source_kind": "MANIFEST",
                                       "authenticity": "UNVERIFIED"},
                                      {"id": "e2", "path": ".env", "sha256": "bb", "source_kind": "MANIFEST",
                                       "authenticity": "UNVERIFIED"}]},
        "business": {
            "actions": [{"key": PAY, "id": "Pay", "skill": "s", "health": "AT_RISK", "verb_phrase": "take a payment",
                         "bindings": [{"id": "b.pay", "components": [API]}],
                         "blockers": [{"type": "unknown", "id": "u1", "statement": "who approves refunds?"}],
                         "evidence": [{"path": "req.md", "quote": "Customers pay by card."}]}],
            "objects": [{"key": REFUND, "id": "Refund", "skill": "s", "health": "UNKNOWN", "definition": "money back",
                         "bindings": [], "systems": []}],
            "graph": {"edges": [{"source": PAY, "target": REFUND, "relation": "PRODUCES"}]},
            "link_decided": [{"skill": "s", "binding": "b.pay", "component": API, "decision": "confirm",
                              "declared_by": "Ana", "note": "handler"}],
            "link_suggestions": {"suggestions": [{"id": "sg1", "skill": "s", "binding": "b.pay", "component": DB,
                                                  "strength": "WEAK", "proposed_by": "agent:claude",
                                                  "signals": [{"signal": "AGENT_PROPOSAL", "detail": "reads db"}]}]},
            "link_gaps": [{"link": {"skill": "s", "binding": "b.old", "component": "svc:gone"},
                           "reason": "LINK_COMPONENT_NOT_DISCOVERED"}]},
        "link_history": [{"at": "2026-10-28T09:00:00Z", "by": "Ana", "action": "confirm", "note": "handler",
                          "row": {"skill": "s", "binding": "b.pay", "component": API}}],
        "dependency_history": [],
        **extra}


class StandingTests(unittest.TestCase):
    def test_path_standing_is_the_weakest_link(self):
        self.assertEqual(ec.path_standing([]), "CONFIRMED")
        self.assertEqual(ec.path_standing(["CONFIRMED", "SOURCE"]), "SOURCE")
        self.assertEqual(ec.path_standing(["SOURCE", "INFERRED", "CONFIRMED"]), "INFERRED")
        self.assertEqual(ec.path_standing(["INFERRED", "UNKNOWN"]), "UNKNOWN")

    def test_confirmed_link_names_who_when_and_why(self):
        found = ec.context(bundle(), PAY)
        runs = [r for r in found["relationships"] if r["relation"] == "RUNS_ON" and r["standing"] == "CONFIRMED"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["other"]["id"], API)
        self.assertEqual(runs[0]["decision"], {"by": "Ana", "at": "2026-10-28T09:00:00Z", "note": "handler"})

    def test_suggestions_are_inferred_never_confirmed(self):
        found = ec.context(bundle(), PAY)
        self.assertEqual([(s["other"]["id"], s["standing"], s["suggestion"]["proposed_by"])
                          for s in found["suggestions"]], [(DB, "INFERRED", "agent:claude")])
        self.assertEqual(found["standing_counts"]["CONFIRMED"], 1)

    def test_unknowns_include_open_questions(self):
        found = ec.context(bundle(), PAY)
        self.assertIn("who approves refunds?", [u["other"]["label"] for u in found["unknowns"]])
        refund = ec.context(bundle(), REFUND)
        self.assertIn("NO_CONFIRMED_LINK", [u["basis"] for u in refund["unknowns"]])

    def test_without_decision_history_the_decided_row_still_names_the_person(self):
        found = ec.context(bundle(link_history=[]), PAY)
        confirmed = next(r for r in found["relationships"] if r["standing"] == "CONFIRMED")
        self.assertEqual(confirmed["decision"], {"by": "Ana", "at": None, "note": "handler"})

    def test_agent_provenance_survives_confirmation(self):
        proposals = [{"skill": "s", "binding": "b.pay", "component": API, "proposed_by": "agent:claude"}]
        found = ec.context(bundle(agent_proposals=proposals), PAY)
        confirmed = next(r for r in found["relationships"] if r["standing"] == "CONFIRMED")
        self.assertEqual(confirmed["decision"]["proposed_by"], "agent:claude")
        self.assertNotIn("proposed_by", ec.context(bundle(), PAY)["relationships"][0]["decision"])


class ImpactAndExplainTests(unittest.TestCase):
    def test_what_relies_on_db_reaches_the_business(self):
        found = ec.context(bundle(), DB)
        rows = {r["id"]: (r["depth"], r["standing"]) for r in found["relied_on_by"]}
        # api depends on db (SOURCE); web reaches db through api. Pay reaches db first through
        # the open suggestion that it runs on db, so that row is only INFERRED, and says so.
        self.assertEqual(rows[API], (1, "SOURCE"))
        self.assertEqual(rows[WEB], (2, "SOURCE"))
        self.assertEqual(rows[PAY], (1, "INFERRED"))
        self.assertEqual(found["impact_semantics"], "DECLARED_REACHABILITY_NOT_MEASURED_BLAST_RADIUS")

    def test_without_the_suggestion_pay_relies_on_db_through_its_confirmed_link(self):
        quiet = bundle()
        quiet["business"]["link_suggestions"] = {"suggestions": []}
        rows = {r["id"]: (r["depth"], r["standing"], r["path"]) for r in ec.context(quiet, DB)["relied_on_by"]}
        self.assertEqual(rows[PAY], (2, "SOURCE", [f"{API} DEPENDS_ON {DB}", f"{PAY} RUNS_ON {API}"]))

    def test_explain_orders_paths_and_names_the_weakest_hop(self):
        found = ec.explain(bundle(), PAY, DB)
        self.assertEqual(found["verdict"], "INFERRED")  # the direct suggestion is shortest
        self.assertEqual(len(found["paths"][0]["hops"]), 1)
        two = [p for p in found["paths"] if len(p["hops"]) == 2]
        self.assertEqual(two[0]["standing"], "SOURCE")
        self.assertEqual([h["standing"] for h in two[0]["hops"]], ["CONFIRMED", "SOURCE"])
        self.assertEqual(ec.explain(bundle(), REFUND, WEB)["verdict"], "SOURCE")

    def test_no_path_is_reported_not_guessed(self):
        lonely = bundle()
        lonely["state"]["nodes"].append({"id": "svc:alone", "kind": "declared_service", "label": "alone"})
        self.assertEqual(ec.explain(lonely, "svc:alone", PAY)["verdict"], "NO_PATH_WITHIN_THREE_HOPS")


class RefusalTests(unittest.TestCase):
    def test_refusals(self):
        with self.assertRaises(ec.ContextError):
            ec.context(bundle(), "missing")
        authorizing = bundle()
        authorizing["state"]["execution_authorized"] = True
        for call in (lambda: ec.context(authorizing, PAY), lambda: ec.open_suggestions(authorizing),
                     lambda: ec.recent(authorizing)):
            with self.assertRaises(ec.ContextError):
                call()
        for query in ("", "x" * 201, "bad\x00query"):
            with self.assertRaises(ec.ContextError):
                ec.search(bundle(), query)

    def test_search_finds_both_layers_business_first(self):
        found = ec.search(bundle(), "pay")
        self.assertEqual(found["results"][0]["id"], PAY)
        self.assertEqual([r["id"] for r in ec.search(bundle(), "API")["results"]], [API])

    def test_open_suggestions_and_recent(self):
        rows = ec.open_suggestions(bundle())["suggestions"]
        self.assertEqual([(r["id"], r["kind"], r["proposed_by"]) for r in rows], [("sg1", "link", "agent:claude")])
        changes = ec.recent(bundle())["changes"]
        self.assertEqual(changes[0]["summary"], f"confirm link b.pay -> {API}")


class ControlRoomRouteTests(unittest.TestCase):
    """The routes the entity page and agents read, on the Python engine and (when built) Rust."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.built = demo.build(Path(cls.temp.name) / "demo", unlinked=True)

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
                with urllib.request.urlopen(base + "/api/state", timeout=2):
                    return base
            except OSError:
                time.sleep(0.1)
        self.fail("control room did not start")

    def get(self, base, path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def routes(self, base):
        status, raw = self.get(base, "/api/bundle")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        status, raw = self.get(base, "/api/search?q=refund")
        self.assertEqual((status, json.loads(raw)), (200, ec.search(data, "refund")))
        for entity in sorted(ec.entity_ids(data)):
            status, raw = self.get(base, "/api/context?id=" + urllib.request.quote(entity, safe=""))
            self.assertEqual((status, json.loads(raw)), (200, ec.context(data, entity)), entity)
        pay, db = "payments-domain:object:Payment", "discovery:service:compose.yml:db"
        status, raw = self.get(base, f"/api/explain?from={pay}&to={db}")
        self.assertEqual((status, json.loads(raw)), (200, ec.explain(data, pay, db)))
        self.assertEqual(self.get(base, "/api/context?id=missing")[0], 404)
        self.assertEqual(self.get(base, "/api/context?id=a&id=b")[0], 400)
        self.assertEqual(self.get(base, "/api/search?q=bad%01")[0], 400)
        self.assertEqual(self.get(base, f"/api/explain?from={pay}")[0], 400)
        self.assertEqual(self.get(base, "/api/bundle?x=1")[0], 400)
        status, raw = self.get(base, "/entity?id=" + pay)
        self.assertEqual(status, 200)
        self.assertIn(b'name="ontologyex-write-token" content=""', raw)

    def test_python_engine(self):
        self.routes(self.serve())

    @unittest.skipUnless(bridge.engine_binary("ontologyex-view").exists(), "Rust view engine not built")
    def test_rust_engine_gives_the_same_answers(self):
        self.routes(self.serve("--view-engine", "rust"))

    def test_entity_page_carries_the_token_only_when_decisions_are_on(self):
        base = self.serve("--allow-link-decisions")
        status, raw = self.get(base, "/entity")
        token = raw.decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]
        self.assertGreaterEqual(len(token), 40)


if __name__ == "__main__":
    unittest.main()
