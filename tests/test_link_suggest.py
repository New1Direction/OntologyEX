"""Link suggestions: explained, deterministic, and applied only by an explicit decision."""
import copy
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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import business_view as bv
import control_room as room
import discover_repo
import link_suggest as ls
from policy_check import DomainError

spec = importlib.util.spec_from_file_location(
    "ontologyex_link_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
PAYMENTS = "discovery:service:compose.yml:payments"


def match_input(bindings, components, linked=(), rejected=()):
    return {"schema_version": 1, "kind": "LINK_MATCH_INPUT", "bindings": bindings,
            "components": components, "linked": [list(x) for x in linked],
            "rejected": [list(x) for x in rejected]}


def binding(binding="billing.charge_card", path="billing/charge.py", system="", binds="Charge"):
    return {"skill": "s", "binding": binding, "binds": binds, "system": system, "path": path}


def component(cid, label, kind="declared_service", path="compose.yml"):
    return {"id": cid, "label": label, "kind": kind, "path": path}


class TokenTests(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(ls.tokens("applyRefundToPayment"), ["payment", "refund", "to"])
        self.assertEqual(ls.tokens("payments.apply_refund"), ["payments", "refund"])
        self.assertEqual(ls.tokens("Zahlungsdienst-é-x"), ["zahlungsdienst"])
        self.assertEqual(ls.tokens("src/main.py"), [])
        self.assertEqual(ls.segments("services/payments/refund.py"), ["services", "payments", "refund"])


class MatcherTests(unittest.TestCase):
    def test_same_source_file_is_strong_and_services_never_match_files(self):
        data = match_input([binding(path="charge.py")], [
            component("m", "charge", "source_module", "src/billing/charge.py"),
            component("svc", "unrelated", "declared_service", "charge.py")])
        rows = ls.suggest_from(data)["suggestions"]
        self.assertEqual([(r["component"], r["strength"]) for r in rows], [("m", "STRONG")])
        self.assertEqual(rows[0]["signals"][0]["signal"], "SAME_SOURCE_FILE")

    def test_path_and_system_are_moderate_name_only_is_weak(self):
        data = match_input([binding(system="acme-billing")], [
            component("a", "billing"), component("b", "card")])
        rows = {r["component"]: r for r in ls.suggest_from(data)["suggestions"]}
        self.assertEqual(rows["a"]["strength"], "MODERATE")
        self.assertEqual(rows["b"]["strength"], "WEAK")
        self.assertEqual([s["signal"] for s in rows["b"]["signals"]], ["SHARED_NAME"])

    def test_unrelated_components_are_not_suggested(self):
        data = match_input([binding()], [component("x", "inventory"), component("y", "db")])
        self.assertEqual(ls.suggest_from(data)["suggestions"], [])

    def test_linked_and_rejected_pairs_are_skipped(self):
        data = match_input([binding()], [component("a", "billing")],
                           linked=[("s", "billing.charge_card", "a")])
        self.assertEqual(ls.suggest_from(data)["suggestions"], [])
        data = match_input([binding()], [component("a", "billing")],
                           rejected=[("s", "billing.charge_card", "a")])
        result = ls.suggest_from(data)
        self.assertEqual((result["suggestions"], result["rejected_not_shown"]), ([], 1))

    def test_at_most_five_per_binding_strongest_first(self):
        comps = [component(f"c{i}", f"billing {i}" if i else "billing") for i in range(8)]
        rows = ls.suggest_from(match_input([binding()], comps))["suggestions"]
        self.assertEqual(len(rows), ls.MAX_PER_BINDING)
        self.assertEqual(rows[0]["component"], "c0")

    def test_deterministic_ids_and_never_applied(self):
        data = match_input([binding()], [component("a", "billing")])
        first, second = ls.suggest_from(data), ls.suggest_from(copy.deepcopy(data))
        self.assertEqual(first, second)
        self.assertEqual(first["suggestions"][0]["id"], ls.suggestion_id("s", "billing.charge_card", "a"))
        self.assertEqual(first["auto_applied"], 0)
        self.assertEqual(first["suggestions"][0]["status"], "PROPOSED")


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.built = demo.build(Path(cls.temp.name) / "demo", NOW, unlinked=True)
        cls.skill = Path(cls.built["skill"])
        cls.state = room.build_state(discover_repo.discover(Path(cls.built["repo"])), [], {}, NOW)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def links(self, name):
        path = Path(self.temp.name) / f"{name}.json"
        path.write_text(json.dumps({"schema_version": 1, "kind": bv.LINK_KIND, "links": []}))
        return path

    def test_demo_suggestions(self):
        rows = ls.suggest([self.skill], self.state, [], [])["suggestions"]
        self.assertEqual({(r["binding"], r["component"], r["strength"]) for r in rows}, {
            ("payments.apply_refund", PAYMENTS, "MODERATE"),
            ("payments.Payment", PAYMENTS, "MODERATE")})

    def test_confirm_then_reject_then_idempotent(self):
        path = self.links("decide")
        rows = {r["binding"]: r for r in ls.suggest([self.skill], self.state, [], [])["suggestions"]}
        self.assertEqual(ls.decide(path, rows["payments.apply_refund"], "confirm", "ana")["status"], "CONFIRMED")
        self.assertEqual(ls.decide(path, rows["payments.Payment"], "reject", "ana", "ledger")["status"], "REJECTED")
        self.assertEqual(ls.decide(path, rows["payments.Payment"], "confirm", "bo")["status"], "UNCHANGED")
        links, rejected = bv.read_link_document(path)
        self.assertEqual([(l["binding"], l["declared_by"]) for l in links], [("payments.apply_refund", "ana")])
        self.assertEqual([r["binding"] for r in rejected], ["payments.Payment"])
        after = ls.suggest([self.skill], self.state, links, rejected)
        self.assertEqual((after["suggestions"], after["rejected_not_shown"]), ([], 1))
        picture = bv.project([self.skill], self.state, links, None)
        actions = {a["id"]: a for a in picture["actions"]}
        self.assertEqual(actions["IssueRefund"]["systems"], [PAYMENTS])

    def test_decisions_need_a_person_and_a_proposal(self):
        path = self.links("refuse")
        row = ls.suggest([self.skill], self.state, [], [])["suggestions"][0]
        with self.assertRaises(DomainError):
            ls.decide(path, row, "confirm", "  ")
        with self.assertRaises(DomainError):
            ls.decide(path, row, "approve", "ana")
        with self.assertRaises(DomainError):
            ls.decide(path, {**row, "status": "CONFIRMED"}, "confirm", "ana")
        self.assertEqual(bv.read_link_document(path), ([], []))

    def test_document_rejects_a_pair_that_is_both_linked_and_rejected(self):
        path = Path(self.temp.name) / "both.json"
        row = {"skill": "s", "binding": "b", "component": "c", "declared_by": "x", "note": ""}
        path.write_text(json.dumps({"schema_version": 1, "kind": bv.LINK_KIND,
                                    "links": [row], "rejected": [row]}))
        with self.assertRaises(DomainError):
            bv.read_link_document(path)
        path.write_text(json.dumps({"schema_version": 1, "kind": bv.LINK_KIND, "links": [], "x": 1}))
        with self.assertRaises(DomainError):
            bv.read_link_document(path)


class DecisionHttpTests(unittest.TestCase):
    def serve(self, built, *extra):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([*built["command"], "--port", str(port), *extra],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/api/business", timeout=2) as resp:
                    return base, json.load(resp)
            except OSError:
                time.sleep(0.1)
        self.fail("control room did not start")

    def post(self, base, body, token="", origin=None):
        request = urllib.request.Request(base + "/api/links/decision", method="POST",
                                         data=json.dumps(body).encode(), headers={
                                             "Content-Type": "application/json",
                                             "Origin": origin or base, "X-OntologyEX-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_writes_are_off_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", unlinked=True)
            base, data = self.serve(built)
            self.assertEqual(data["link_decisions"], "READ_ONLY_USE_CLI")
            row = data["link_suggestions"]["suggestions"][0]
            self.assertIn("link_suggest.py confirm", row["confirm_command"])
            status, body = self.post(base, {"id": row["id"], "decision": "confirm", "by": "a", "note": ""})
            self.assertEqual((status, body["status"]), (403, "LINK_DECISIONS_DISABLED"))
            with urllib.request.urlopen(base + "/business", timeout=2) as resp:
                self.assertIn('name="ontologyex-write-token" content=""', resp.read().decode())

    def test_opt_in_decision_requires_token_and_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = demo.build(Path(tmp) / "demo", unlinked=True)
            base, data = self.serve(built, "--allow-link-decisions")
            with urllib.request.urlopen(base + "/business", timeout=2) as resp:
                page = resp.read().decode()
            token = page.split('name="ontologyex-write-token" content="')[1].split('"')[0]
            self.assertGreaterEqual(len(token), 40)
            row = next(r for r in data["link_suggestions"]["suggestions"]
                       if r["binding"] == "payments.apply_refund")
            body = {"id": row["id"], "decision": "confirm", "by": "ana", "note": ""}
            self.assertEqual(self.post(base, body)[0], 403)
            self.assertEqual(self.post(base, body, token, "http://evil.example")[0], 403)
            self.assertEqual(self.post(base, {**body, "extra": 1}, token)[0], 400)
            self.assertEqual(self.post(base, {**body, "id": "missing"}, token)[0], 400)
            status, result = self.post(base, body, token)
            self.assertEqual((status, result["status"]), (200, "CONFIRMED"))
            links, _ = bv.read_link_document(Path(built["links"]))
            self.assertEqual([(l["binding"], l["declared_by"]) for l in links], [("payments.apply_refund", "ana")])
            with urllib.request.urlopen(base + "/api/business", timeout=2) as resp:
                after = json.load(resp)
            health = {a["id"]: a["health"] for a in after["actions"]}
            self.assertEqual(health["IssueRefund"], "AT_RISK")


if __name__ == "__main__":
    unittest.main()
