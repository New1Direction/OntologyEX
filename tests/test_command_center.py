"""The command center: everything that needs attention, ranked by named factors, with coverage and the field."""
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
sys.path.insert(0, str(ROOT / "tests"))
import agent_tools as at
import command_center as cc
from test_entity_context import API, DB, PAY, bundle

spec = importlib.util.spec_from_file_location("ontologyex_command_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

STRIPE = "discovery:supplier:stripe"


def with_supplier() -> dict:
    """The small bundle plus Stripe, the only payments supplier, which the api service declares."""
    model = bundle()
    model["state"]["nodes"].append({"id": STRIPE, "kind": "external_supplier", "label": "Stripe", "evidence_id": "e1"})
    model["state"]["edges"].append({"source": API, "target": STRIPE, "relation": "DEPENDS_ON", "evidence_id": "e1"})
    model["suppliers"] = {"suppliers": [{"id": STRIPE, "name": "Stripe", "category": "payments",
                                         "criticality": "CRITICAL"}],
                          "categories": [{"category": "payments", "criticality": "CRITICAL",
                                          "suppliers": ["Stripe"], "single_supplier": True}]}
    return model


class RankingTests(unittest.TestCase):
    def test_every_kind_of_attention_is_ranked_with_its_factors(self):
        found = cc.report(with_supplier())
        kinds = {i["kind"]: i for i in found["items"]}
        self.assertEqual(set(kinds), {"SYSTEM_ERROR", "AT_RISK", "OPEN_QUESTION", "LINK_GAP",
                                      "SINGLE_CRITICAL_SUPPLIER", "DECISIONS_WAITING", "UNOBSERVED_SERVICES"})
        scores = [i["score"] for i in found["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for item in found["items"]:  # a score is exactly its factors, so the ranking can be argued with
            self.assertEqual(item["score"], min(100, sum(f["points"] for f in item["factors"])), item["title"])
            self.assertEqual(item["level"], cc._level(item["score"]))
            self.assertTrue(item["next_step"])
        db = kinds["SYSTEM_ERROR"]
        self.assertEqual(db["subject"]["id"], DB)
        self.assertEqual([f["factor"] for f in db["factors"]], ["severity", "reach", "money"])  # Pay relies on db
        self.assertEqual(kinds["OPEN_QUESTION"]["why"], "who approves refunds?")
        self.assertEqual(kinds["OPEN_QUESTION"]["standing"], "UNKNOWN")
        self.assertEqual(kinds["SINGLE_CRITICAL_SUPPLIER"]["subject"]["id"], STRIPE)
        self.assertIn("Pay", kinds["SINGLE_CRITICAL_SUPPLIER"]["factors"][1]["detail"])  # Pay runs on api, api on Stripe
        self.assertEqual(found["by_level"], {name: sum(i["level"] == name for i in found["items"])
                                             for _, name in cc.LEVELS})
        self.assertFalse(found["execution_authorized"])

    def test_what_nobody_can_see_is_an_item_too(self):
        model = with_supplier()
        model["business"]["actions"] = []
        kinds = {i["kind"]: i for i in cc.report(model)["items"]}
        self.assertEqual((kinds["NO_BUSINESS_MODEL"]["level"], kinds["NO_BUSINESS_MODEL"]["standing"]),
                         ("HIGH", "UNKNOWN"))
        self.assertEqual(kinds["UNOBSERVED_SERVICES"]["title"], "3 of 3 services report no health")

    def test_same_model_same_ranking(self):
        self.assertEqual(json.dumps(cc.report(with_supplier()), sort_keys=True),
                         json.dumps(cc.report(with_supplier()), sort_keys=True))

    def test_reach_and_money_raise_the_score(self):
        alone = cc._item("STALE", None, "a report", "", [])
        reached = cc._item("STALE", None, "a report", "", [{"layer": "business", "label": "IssueRefund"},
                                                           {"layer": "business", "label": "Ship"}])
        self.assertEqual(alone["score"], 20)
        self.assertEqual(reached["score"], 20 + 16 + 15)  # two business items, and a refund moves money

    def test_kpis_and_coverage(self):
        found = cc.report(with_supplier())
        kpis = {k["label"]: k["value"] for k in found["kpis"]}
        self.assertEqual((kpis["Business actions"], kpis["At risk or impacted"], kpis["Services in error"],
                          kpis["Suppliers"], kpis["Decisions waiting"]), (1, 1, 1, 1, 1))
        rows = {r["label"]: r for r in found["coverage"]["rows"]}
        self.assertEqual(rows["Business actions linked to a system"]["percent"], 100)
        self.assertEqual((rows["Services reporting health"]["part"], rows["Services reporting health"]["whole"]), (0, 3))
        self.assertIsNone(rows["Confirmations still current"]["percent"])  # none recorded: no fake 0% or 100%


class FieldTests(unittest.TestCase):
    def test_too_few_names_says_so(self):
        self.assertIsNone(cc.report(bundle())["field"]["field"])

    def test_words_not_code_conventions(self):
        self.assertEqual(cc._words("rpc createAdminOrderRoute"), "order")
        model = bundle()  # handlers named ...Route are not a logistics business
        model["state"]["nodes"] += [{"id": f"r:{n}", "kind": "api_route", "label": f"rpc {n}Route"}
                                    for n in ("createUser", "getTeam", "updateDocument", "findTemplates")]
        self.assertIsNone(cc.report(model)["field"]["field"])

    def test_a_shop_gets_the_e_commerce_checklist(self):
        model = with_supplier()
        model["state"]["nodes"] += [{"id": f"t:{name}", "kind": "declared_db_table", "label": name}
                                    for name in ("orders", "order_items", "products", "carts", "inventory")]
        field = cc.report(model)["field"]
        self.assertEqual(field["field"], "e-commerce")
        checks = {c["item"]: c for c in field["checklist"]}
        self.assertEqual((checks["Payments provider"]["status"], checks["Payments provider"]["suppliers"]),
                         ("FOUND", ["Stripe"]))
        self.assertEqual(checks["Inventory or stock"]["entities"][0]["id"], "t:inventory")
        self.assertEqual(checks["Shipping carriers"]["status"], "NOT_FOUND_IN_SOURCES")
        self.assertIn("not that the business lacks it", field["detail"])


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ontologyex-command-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "bundle.json").write_text(json.dumps(with_supplier()))

    def tools(self, policy=None):
        path = None
        if policy is not None:
            path = self.tmp / "policy.json"
            path.write_text(json.dumps({"schema_version": 1, **policy}))
        return at.Tools(at.Source(None, self.tmp / "bundle.json"), at.load_policy(path),
                        at.Audit(self.tmp / "audit.jsonl", "claude", "Dana"), None)

    def test_read_only_and_filtered_by_level(self):
        self.assertEqual(at.TOOLS["what_needs_attention"]["kind"], at.READ_ONLY)
        everything = self.tools().call("what_needs_attention", {})
        self.assertEqual(everything["items"][0]["kind"], "SYSTEM_ERROR")
        critical = self.tools().call("what_needs_attention", {"level": "CRITICAL"})
        self.assertTrue(critical["items"])
        self.assertEqual({i["level"] for i in critical["items"]}, {"CRITICAL"})
        with self.assertRaises(at.ToolError) as caught:
            self.tools().call("what_needs_attention", {"level": "URGENT"})
        self.assertEqual(caught.exception.status, "INVALID_ARGUMENTS")

    def test_a_withheld_entity_is_not_named(self):
        found = self.tools({"allow_tools": ["what_needs_attention"], "deny_entities": [DB]}).call(
            "what_needs_attention", {})
        self.assertNotIn(DB, json.dumps(found))
        self.assertNotIn("db reports an error", json.dumps(found))
        self.assertIn("SINGLE_CRITICAL_SUPPLIER", [i["kind"] for i in found["items"]])
        self.assertEqual(found["total"], len(found["items"]))


class ServedTests(unittest.TestCase):
    def test_route_and_page_from_the_running_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([*built["command"], "--port", str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            with urllib.request.urlopen(base + "/api/command", timeout=30) as resp:
                found = json.load(resp)
            self.assertEqual(found["kind"], "COMMAND_CENTER")
            first = found["items"][0]
            self.assertEqual((first["kind"], first["level"]), ("SYSTEM_ERROR", "CRITICAL"))  # payments is down
            self.assertIn("IssueRefund", json.dumps(first["factors"]))
            with urllib.request.urlopen(base + "/command", timeout=10) as resp:
                self.assertIn(b"Command center", resp.read())
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/command?x=1", timeout=10)
            self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
