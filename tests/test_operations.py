"""Operations: the business's own records, read as weekly aggregates only (operations.py)."""
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import command_center
import operations as ops

spec = importlib.util.spec_from_file_location("ontologyex_operations_demo", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)  # a Thursday in 2026-W44


def write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)


class DemoTests(Folder):
    def setUp(self):
        super().setUp()
        demo.write_operations(self.dir / "ops", NOW)
        self.answer = ops.report(self.dir / "ops", now=NOW)

    def test_no_identifier_or_free_text_leaves_the_records(self):
        text = json.dumps(self.answer)
        for needle in ("example.test", "Customer ", "Order #", "Payment complete", "bank declined", "ba_9"):
            self.assertNotIn(needle, text, needle)
        self.assertIsNone(re.search(r"\b(ch|re|dp|po|sub|in|cus)_[a-z0-9]{6,}", text))  # no record IDs
        self.assertFalse(self.answer["privacy"]["rows_kept"])

    def test_each_export_is_recognized_and_only_its_columns_read(self):
        by_file = {s["file"]: s for s in self.answer["sources"]}
        self.assertEqual({f: s.get("stream") for f, s in by_file.items() if s["status"] == "READ"},
                         {"payments.csv": "payments", "refunds.csv": "refunds", "disputes.csv": "disputes",
                          "payouts.csv": "payouts", "subscriptions.csv": "subscriptions", "invoices.csv": "invoices"})
        self.assertNotIn("customer", " ".join(by_file["payments.csv"]["columns_used"]).lower())
        self.assertEqual(by_file["payments.csv"]["columns_not_read"], 6)
        unknown = self.answer["unrecognized"][0]
        self.assertEqual((unknown["file"], unknown["headers"]), ("bank/statement-october.csv",
                                                                 ["Posting Date", "Narrative", "Debit", "Credit"]))

    def test_signals_from_complete_weeks(self):
        found = {s["signal"] for s in self.answer["signals"]}
        self.assertTrue({"DISPUTES_OPEN", "PAYOUT_FAILED", "REFUND_RATE_RISING"} <= found, found)
        self.assertNotIn("REVENUE_DOWN", found)  # the week in progress is never compared
        self.assertEqual(self.answer["week_in_progress"], "2026-W44")
        usd = self.answer["currencies"]["usd"]
        self.assertEqual(usd["last_week"], "2026-W44")
        self.assertGreater(usd["recent"]["payments"]["refund_rate"], usd["recent"]["payments"]["refund_rate_before"])

    def test_the_same_files_and_day_give_the_same_answer(self):
        self.assertEqual(ops.report(self.dir / "ops", now=NOW), self.answer)

    def test_code_and_operations(self):
        entities = [{"id": "s:pay:action:IssueRefund", "label": "IssueRefund", "layer": "business"},
                    {"id": "s:pay:action:CancelSubscription", "label": "CancelSubscription", "layer": "business"},
                    {"id": "svc:payments-api", "label": "payments-api", "layer": "system"}]
        found = {(f["finding"], f["stream"]) for f in ops.with_comparison(self.answer, entities)["code_vs_operations"]}
        self.assertIn(("SEEN_AND_MODELED", "refunds"), found)
        self.assertIn(("SEEN_AND_MODELED", "subscriptions"), found)
        self.assertIn(("HAPPENS_NOT_IN_CODE", "disputes"), found)
        self.assertIn(("HAPPENS_NOT_IN_CODE", "payouts"), found)
        quiet = {**self.answer, "streams_present": ["payments"]}
        found = {(f["finding"], f["stream"]) for f in ops.with_comparison(quiet, entities)["code_vs_operations"]}
        self.assertIn(("IN_CODE_NOT_SEEN", "refunds"), found)  # 16 weeks of records, and no refund in them

    def test_command_center_ranks_them(self):
        bundle = {"operations": ops.with_comparison(self.answer, [{"id": "a", "label": "IssueRefund", "layer": "business"}])}
        items = {i["kind"]: i for i in command_center.report({**bundle, "state": {"nodes": [], "edges": []}})["items"]}
        self.assertEqual(items["PAYOUT_FAILED"]["category"], "Operations")
        self.assertIn("money", [f["factor"] for f in items["REFUND_RATE_RISING"]["factors"]])
        self.assertEqual(items["REFUND_RATE_RISING"]["links"], ["a"])


class AgentToolTests(Folder):
    def test_agents_get_aggregates_and_the_series_only_on_request(self):
        import agent_tools as at
        demo.write_operations(self.dir / "ops", NOW)
        bundle = {"state": {"nodes": [], "edges": [], "execution_authorized": False},
                  "operations": ops.report(self.dir / "ops", now=NOW)}
        (self.dir / "bundle.json").write_text(json.dumps(bundle))
        tools = at.Tools(at.Source(None, self.dir / "bundle.json"), at.load_policy(None),
                         at.Audit(self.dir / "audit.jsonl", "claude", "Dana"), None)
        brief = tools.call("operations_status", {})
        self.assertIn("PAYOUT_FAILED", {s["signal"] for s in brief["signals"]})
        self.assertNotIn("streams", brief["currencies"]["usd"])
        self.assertIn("streams", tools.call("operations_status", {"weeks": "yes"})["currencies"]["usd"])
        with self.assertRaises(at.ToolError):
            tools.call("operations_status", {"weeks": "all"})


class ArithmeticTests(Folder):
    def test_sums_weeks_currencies_and_formats(self):
        write(self.dir, "payments.csv", "id,Created date (UTC),Amount,Amount Refunded,Currency,Status,Customer Email\n"
              "ch_1,2026-10-04 23:30:00,10.00,0,usd,Paid,a@x.test\n"     # Sunday: ISO week 40
              "ch_2,2026-10-05T00:30:00Z,\"1,234.50\",0,usd,Paid,b@x.test\n"  # Monday: week 41
              "ch_3,2026-10-06 10:00,5,5,usd,Refunded,c@x.test\n"
              "ch_4,2026-10-06 11:00,7,0,usd,Failed,d@x.test\n"
              "ch_5,2026-10-06 11:00,\"1.000,50\",0,eur,Paid,e@x.test\n")
        answer = ops.report(self.dir, min_cell=1, now=NOW)
        usd = {w["week"]: w for w in answer["currencies"]["usd"]["streams"]["payments"]}
        self.assertEqual((usd["2026-W40"]["gross"], usd["2026-W41"]["gross"]), (10.0, 1239.5))
        self.assertEqual((usd["2026-W41"]["succeeded"], usd["2026-W41"]["failed"], usd["2026-W41"]["refunded"]), (2, 1, 5.0))
        self.assertEqual(answer["currencies"]["eur"]["totals"]["payments"]["gross"], 1000.5)  # never added to dollars

    def test_minor_units_from_api_objects(self):
        write(self.dir, "charges.json", json.dumps({"object": "list", "data": [
            {"object": "charge", "id": "ch_9", "amount": 1000, "currency": "jpy", "created": 1791100000, "status": "succeeded",
             "billing_details": {"email": "a@x.test"}},
            {"object": "charge", "id": "ch_8", "amount": 1250, "currency": "usd", "created": 1791100000, "status": "succeeded"},
            {"object": "charge", "id": "ch_7", "amount": 1000, "currency": "kwd", "created": 1791100000, "status": "succeeded"}]}))
        c = ops.report(self.dir, min_cell=1, now=NOW)["currencies"]
        self.assertEqual((c["jpy"]["totals"]["payments"]["gross"], c["usd"]["totals"]["payments"]["gross"],
                          c["kwd"]["totals"]["payments"]["gross"]), (1000.0, 12.5, 1.0))
        self.assertNotIn("a@x.test", json.dumps(ops.report(self.dir, min_cell=1, now=NOW)))

    def test_small_weeks_withhold_amounts_not_counts(self):
        write(self.dir, "refunds.csv", "id,Amount,Created (UTC),Currency,Reason,Status\n"
              "re_1,40.00,2026-10-06 10:00,usd,duplicate,succeeded\nre_2,15.00,2026-10-07 10:00,usd,fraudulent,succeeded\n")
        week = ops.report(self.dir, now=NOW)["currencies"]["usd"]["streams"]["refunds"][0]
        self.assertEqual((week["count"], week["amount"], week["reasons"], week["withheld"]), (2, None, None, True))
        week = ops.report(self.dir, min_cell=1, now=NOW)["currencies"]["usd"]["streams"]["refunds"][0]
        self.assertEqual((week["amount"], week["reasons"]), (55.0, {"duplicate": 1, "fraudulent": 1}))

    def test_subscriptions_become_weekly_active_and_monthly_revenue(self):
        write(self.dir, "subscriptions.csv", "id,Customer Email,Plan,Quantity,Interval,Amount,Currency,Status,Created (UTC),"
              "Canceled At (UTC)\n"
              "sub_1,a@x.test,Team,2,month,50.00,usd,active,2026-09-01 00:00,\n"
              "sub_2,b@x.test,Annual,1,year,1200.00,usd,active,2026-09-01 00:00,\n"
              "sub_3,c@x.test,Team,1,month,50.00,usd,canceled,2026-09-01 00:00,2026-09-20 00:00\n"
              "sub_4,d@x.test,Team,1,month,50.00,usd,past_due,2026-10-01 00:00,\n")
        answer = ops.report(self.dir, min_cell=1, now=NOW)
        weeks = {w["week"]: w for w in answer["currencies"]["usd"]["streams"]["subscriptions"]}
        self.assertEqual((weeks["2026-W36"]["active"], weeks["2026-W36"]["mrr"], weeks["2026-W36"]["new"]), (3, 250.0, 3))
        self.assertEqual((weeks["2026-W39"]["active"], weeks["2026-W39"]["mrr"]), (2, 200.0))   # sub_3 ended in W38
        self.assertEqual(weeks["2026-W38"]["canceled"], 1)
        self.assertEqual((weeks["2026-W40"]["active"], weeks["2026-W40"]["mrr"]), (3, 250.0))   # sub_4 started
        self.assertIn("SUBSCRIPTIONS_PAST_DUE", {s["signal"] for s in answer["signals"]})

    def test_a_mapping_reads_another_processor(self):
        write(self.dir, "paypal-activity.csv", "Date,Time,Name,Type,Status,Currency,Gross,Fee,From Email Address\n"
              "10/06/2026,10:00:00,Ann,Payment,Completed,USD,25.00,-1.03,ann@x.test\n"
              "10/07/2026,11:00:00,Bo,Payment,Denied,USD,30.00,0,bo@x.test\n")
        unread = ops.report(self.dir, now=NOW)  # recognized by its columns, but its dates are month-first
        source = unread["sources"][0]
        self.assertEqual((source["stream"], source["rows"], source["rows_used"]), ("payments", 2, 0))
        self.assertIn("date_format", source["hint"])
        write(self.dir, "operations.json", json.dumps({"files": [{
            "match": "paypal*.csv", "stream": "payments", "date_format": "%m/%d/%Y",
            "columns": {"amount": "Gross", "currency": "Currency", "created": "Date", "status": "Status", "fee": "Fee"},
            "status_map": {"Completed": "succeeded", "Denied": "failed"}}]}))
        week = ops.report(self.dir, min_cell=1, now=NOW)["currencies"]["usd"]["streams"]["payments"][0]
        self.assertEqual((week["week"], week["succeeded"], week["failed"], week["gross"]), ("2026-W41", 1, 1, 25.0))
        self.assertNotIn("ann@x.test", json.dumps(ops.report(self.dir, min_cell=1, now=NOW)))

    def test_labels_drop_identifiers(self):
        self.assertEqual([ops.label(x) for x in ("card_declined", "requested_by_customer", "cus_8Hj2kQ91", "a@b.test",
                                                 "Order 123456", "Team")],
                         ["card_declined", "requested_by_customer", "other", "other", "other", "team"])

    def test_empty_folder(self):
        self.assertEqual(ops.report(self.dir / "none", now=NOW)["status"], "NO_OPERATIONS_FOLDER")


class FakeStripe(BaseHTTPRequestHandler):
    pages = {}
    seen_keys = []

    def do_GET(self):
        self.seen_keys.append(self.headers.get("Authorization"))
        path, _, query = self.path.partition("?")
        after = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("starting_after")
        rows = self.pages.get(path, [])
        start = next((i + 1 for i, r in enumerate(rows) if r["id"] == after), 0) if after else 0
        body = json.dumps({"object": "list", "data": rows[start:start + 2], "has_more": start + 2 < len(rows)}).encode()
        self.send_response(401 if path == "/v1/payouts" and FakeStripe.deny_payouts else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PullTests(Folder):
    def setUp(self):
        super().setUp()
        t = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp())
        FakeStripe.deny_payouts = False
        FakeStripe.pages = {
            "/v1/charges": [{"object": "charge", "id": f"ch_{i}abc12", "amount": 2500, "currency": "usd", "created": t,
                             "status": "succeeded" if i % 4 else "failed", "receipt_email": f"c{i}@x.test",
                             "failure_code": None if i % 4 else "card_declined"} for i in range(5)],
            "/v1/refunds": [{"object": "refund", "id": "re_1abc12", "amount": 2500, "currency": "usd", "created": t,
                             "status": "succeeded", "reason": "duplicate"}],
            "/v1/subscriptions": [{"object": "subscription", "id": "sub_1abc12", "status": "active", "created": t,
                                   "customer": "cus_1abc12", "currency": "usd",
                                   "items": {"data": [{"quantity": 3, "price": {"unit_amount": 1000, "currency": "usd",
                                                                                "recurring": {"interval": "month"}}}]}}]}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeStripe)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def test_pull_writes_aggregates_only_and_the_report_reads_them(self):
        result = ops.pull_stripe("rk_test_abc", self.dir, 90, self.base, now=NOW)
        self.assertEqual(result["objects_read"], {"payments": 5, "refunds": 1, "subscriptions": 1})
        written = (self.dir / "stripe-api.ontologyex-aggregate.json").read_text()
        for needle in ("@x.test", "ch_", "cus_", "sub_1", "rk_test"):
            self.assertNotIn(needle, written)
        self.assertEqual(FakeStripe.seen_keys[0], "Bearer rk_test_abc")
        answer = ops.report(self.dir, min_cell=1, now=NOW)
        usd = answer["currencies"]["usd"]["totals"]
        self.assertEqual((usd["payments"]["succeeded"], usd["payments"]["failed"], usd["payments"]["gross"]), (3, 2, 75.0))
        self.assertEqual((usd["subscriptions"]["active"], usd["subscriptions"]["mrr"]), (1, 30.0))
        # A file covering the same weeks: the pull is kept, and the overlap is said.
        write(self.dir, "payments.csv", "id,Created date (UTC),Amount,Currency,Status,Captured\n"
              "ch_x,2026-10-06 10:00,99,usd,Paid,true\n")
        answer = ops.report(self.dir, min_cell=1, now=NOW)
        self.assertEqual(answer["currencies"]["usd"]["totals"]["payments"]["gross"], 75.0)
        self.assertEqual(answer["overlaps"][0]["stream"], "payments")

    def test_a_secret_key_is_refused_and_denied_reads_say_so(self):
        with self.assertRaises(ops.OperationsError):
            ops.pull_stripe("sk_live_abc", self.dir, 90, self.base, now=NOW)
        FakeStripe.deny_payouts = True
        with self.assertRaises(ops.OperationsError) as caught:
            ops.pull_stripe("rk_test_abc", self.dir, 90, self.base, now=NOW)
        self.assertIn("/v1/payouts", str(caught.exception))


class RouteTests(unittest.TestCase):
    def test_the_control_room_serves_operations(self):
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
            with urllib.request.urlopen(base + "/api/operations", timeout=60) as resp:
                answer = json.load(resp)
            self.assertEqual(answer["status"], "OK")
            self.assertIn("SEEN_AND_MODELED", {f["finding"] for f in answer["code_vs_operations"]})
            with urllib.request.urlopen(base + "/api/operations?format=markdown", timeout=30) as resp:
                self.assertTrue(resp.read().decode().startswith("# Operations"))
            with urllib.request.urlopen(base + "/api/command", timeout=60) as resp:
                kinds = {i["kind"] for i in json.load(resp)["items"]}
            self.assertIn("PAYOUT_FAILED", kinds)
            with urllib.request.urlopen(base + "/operations", timeout=10) as resp:
                self.assertIn(b"OntologyEX / Operations", resp.read())
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/operations?x=1", timeout=10)
            self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
