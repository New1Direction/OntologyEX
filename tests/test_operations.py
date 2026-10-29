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
                          "payouts.csv": "payouts", "subscriptions.csv": "subscriptions", "invoices.csv": "invoices",
                          "bank/business-account.csv": "transactions", "bills.csv": "bills",
                          "cloud/aws-cost-and-usage.csv": "cloud_costs", "support/zendesk-tickets.csv": "tickets",
                          "crm/hubspot-deals.csv": "deals"})
        self.assertNotIn("customer", " ".join(by_file["payments.csv"]["columns_used"]).lower())
        self.assertEqual(by_file["payments.csv"]["columns_not_read"], 6)
        self.assertEqual(by_file["bank/business-account.csv"]["recognized_by"], "operations.json")
        self.assertEqual(by_file["bank/business-account.csv"]["columns_matched_only"], ["description"])
        self.assertEqual(self.answer["unrecognized"], [])

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


class SpendDemoTests(Folder):
    def setUp(self):
        super().setUp()
        demo.write_operations(self.dir / "ops", NOW)
        self.answer = ops.report(self.dir / "ops", now=NOW)
        self.usd = self.answer["spend"]["currencies"]["usd"]

    def test_no_person_payee_description_or_account_leaves_the_records(self):
        text = json.dumps(self.answer)
        for needle in ("Jordan", "Okafor", "Riley", "WEWORK", "STAPLES", "arn:aws", "ST-", "Hartwell", "aws.amazon.com"):
            self.assertNotIn(needle, text, needle)
        self.assertIsNone(re.search(r"\b\d{12}\b", text))  # no AWS account or resource number

    def test_suppliers_categories_cloud_and_signals(self):
        self.assertEqual([p["name"] for p in self.usd["suppliers_paid"]],
                         ["Amazon Web Services", "Datadog", "Twilio", "SendGrid", "OpenAI"])
        categories = self.usd["totals"]["transactions"]["categories"]
        self.assertEqual(set(categories), {"payroll", "rent", "legal", "hosting", "software", "office", "refunds"})
        self.assertEqual(set(self.usd["totals"]["cloud_costs"]["services"]),
                         {"amazon elastic compute cloud", "amazon relational database service", "amazon simple storage service"})
        found = {s["signal"] for s in self.answer["signals"]}
        self.assertTrue({"SPEND_RISING", "CLOUD_COST_RISING", "BILLS_OVERDUE", "NEW_SUPPLIER_PAID",
                         "PAYOUTS_NOT_IN_BANK"} <= found, found)

    def test_the_missing_payout_is_exactly_the_gap(self):
        import csv
        rows = list(csv.reader((self.dir / "ops/payouts.csv").open()))[1:]
        self.assertEqual(self.usd["reconciliation"]["gap"], float(rows[3][1]))  # the one the bank never got

    def test_paid_against_used(self):
        code = [{"name": "Stripe", "category": "payments"}, {"name": "SendGrid", "category": "email and messaging"},
                {"name": "Sentry", "category": "analytics and monitoring"}]
        found = {(f["finding"], f["supplier"]) for f in
                 ops.with_comparison(self.answer, [], code)["spend"]["suppliers_vs_spend"]}
        self.assertEqual(found, {("PAID_AND_IN_CODE", "SendGrid"), ("PAID_NOT_IN_CODE", "Amazon Web Services"),
                                 ("PAID_NOT_IN_CODE", "Datadog"), ("PAID_NOT_IN_CODE", "Twilio"),
                                 ("PAID_NOT_IN_CODE", "OpenAI"), ("IN_CODE_NOT_PAID", "Sentry")})  # Stripe: a processor
        items = command_center.report({"state": {"nodes": [], "edges": []},
                                       "operations": ops.with_comparison(self.answer, [], code)})["items"]
        self.assertEqual({i["kind"] for i in items if i["category"] == "Spend"},
                         {"PAYOUTS_NOT_IN_BANK", "BILLS_OVERDUE", "SPEND_RISING", "CLOUD_COST_RISING", "NEW_SUPPLIER_PAID",
                          "PAID_NOT_IN_CODE", "IN_CODE_NOT_PAID"})


class SpendFormatTests(Folder):
    def test_debit_credit_columns_and_a_missing_currency(self):
        write(self.dir, "statement.csv", "Posting Date,Narrative,Debit,Credit\n"
              "2026-09-02,GOOGLE *CLOUD 77,310.00,\n2026-09-03,SQ *CORNER COFFEE,4.50,\n2026-09-04,STRIPE PAYOUT,,1200.00\n")
        source = ops.report(self.dir, now=NOW)["sources"][0]
        self.assertEqual((source["stream"], source["rows_used"]), ("transactions", 0))
        self.assertIn("currency", source["hint"])
        write(self.dir, "operations.json", json.dumps({"files": [{"match": "statement.csv", "stream": "transactions",
                                                                  "currency": "eur", "columns": {
            "created": "Posting Date", "description": "Narrative", "debit": "Debit", "credit": "Credit"}}]}))
        eur = ops.report(self.dir, min_cell=1, now=NOW)["spend"]["currencies"]["eur"]
        month = eur["months"]["transactions"][0]
        self.assertEqual((month["in"], month["out"], month["suppliers"], month["processor_deposits"]),
                         (1200.0, 314.5, {"Google Cloud": 310.0}, {"Stripe": 1200.0}))  # Square is too common a word

    def test_card_charges_listed_as_positive(self):
        write(self.dir, "card.csv", "Date,Description,Amount\n2026-09-02,OPENAI *API,20.00\n2026-09-05,PAYMENT THANK YOU,-20.00\n")
        write(self.dir, "operations.json", json.dumps({"files": [{"match": "card.csv", "stream": "transactions",
                                                                  "currency": "usd", "amounts": "out_positive",
                                                                  "columns": {"created": "Date", "description": "Description",
                                                                              "amount": "Amount"}}]}))
        month = ops.report(self.dir, min_cell=1, now=NOW)["spend"]["currencies"]["usd"]["months"]["transactions"][0]
        self.assertEqual((month["out"], month["in"], month["suppliers"]), (20.0, 20.0, {"OpenAI": 20.0}))

    def test_google_cloud_and_azure_exports(self):
        write(self.dir, "gcp-billing.csv", "Billing account name,Service description,SKU description,Usage start date,Cost,Currency\n"
              "Acme,Compute Engine,N2 core,2026-09-01T00:00:00Z,12.50,USD\nAcme,Cloud Storage,Standard,2026-09-01T00:00:00Z,1.25,USD\n")
        write(self.dir, "azure-costs.csv", "Date,MeterCategory,CostInBillingCurrency,BillingCurrency,SubscriptionId\n"
              "2026-09-03,Virtual Machines,30.00,USD,0000-1111\n")
        usd = ops.report(self.dir, now=NOW)["spend"]["currencies"]["usd"]
        cloud = usd["totals"]["cloud_costs"]
        self.assertEqual(cloud["providers"], {"Microsoft Azure": 30.0, "Google Cloud": 13.75})
        self.assertEqual(cloud["services"], {"virtual machines": 30.0, "compute engine": 12.5, "cloud storage": 1.25})

    def test_bills_overdue(self):
        write(self.dir, "bills.csv", "Bill Date,Vendor,Amount,Currency,Due Date,Status\n"
              "2026-09-01,Twilio Inc,120.00,USD,2026-09-30,open\n2026-10-01,Twilio Inc,120.00,USD,2026-11-30,open\n"
              "2026-08-01,Twilio Inc,110.00,USD,2026-08-30,paid\n")
        answer = ops.report(self.dir, min_cell=1, now=NOW)
        months = {m["month"]: m for m in answer["spend"]["currencies"]["usd"]["months"]["bills"]}
        self.assertEqual((months["2026-09"]["overdue"], months["2026-10"]["overdue"], months["2026-10"]["open"]), (1, 0, 1))
        self.assertIn("BILLS_OVERDUE", {s["signal"] for s in answer["signals"]})

    def test_aws_pull_through_the_command_line(self):
        fake = self.dir / "aws"
        log = self.dir / "args.txt"
        answer = {"ResultsByTime": [
            {"TimePeriod": {"Start": "2026-08-01"}, "Groups": [
                {"Keys": ["Amazon Elastic Compute Cloud - Compute"], "Metrics": {"UnblendedCost": {"Amount": "800.5", "Unit": "USD"}}},
                {"Keys": ["Amazon Simple Storage Service"], "Metrics": {"UnblendedCost": {"Amount": "40", "Unit": "USD"}}}]},
            {"TimePeriod": {"Start": "2026-09-01"}, "Groups": [
                {"Keys": ["Amazon Elastic Compute Cloud - Compute"], "Metrics": {"UnblendedCost": {"Amount": "1400", "Unit": "USD"}}}]}]}
        fake.write_text(f"#!/bin/sh\necho \"$@\" > {log}\ncat <<'JSON'\n{json.dumps(answer)}\nJSON\n")
        fake.chmod(0o755)
        out = self.dir / "ops"
        result = ops.pull_aws(out, 3, str(fake), now=NOW)
        self.assertEqual(result["months"], ["2026-08", "2026-09"])
        self.assertTrue(log.read_text().startswith("ce get-cost-and-usage --time-period Start=2026-08-01,End=2026-10-30"))
        usd = ops.report(out, now=NOW)["spend"]["currencies"]["usd"]
        sep = usd["months"]["cloud_costs"][1]
        self.assertEqual((sep["month"], sep["cost"], sep["services"]), ("2026-09", 1400.0,
                                                                         {"amazon elastic compute cloud - compute": 1400.0}))
        self.assertIn("CLOUD_COST_RISING", {s["signal"] for s in ops.report(out, now=NOW)["signals"]})
        broken = self.dir / "aws-broken"
        broken.write_text("#!/bin/sh\necho 'AccessDenied: ce:GetCostAndUsage' >&2\nexit 254\n")
        broken.chmod(0o755)
        with self.assertRaises(ops.OperationsError) as caught:
            ops.pull_aws(out, 3, str(broken), now=NOW)
        self.assertIn("AccessDenied", str(caught.exception))


class CustomersDemoTests(Folder):
    ENTITIES = [{"id": "s:refund", "label": "Refund", "layer": "business"},
                {"id": "s:issue", "label": "IssueRefund", "layer": "business"},
                {"id": "s:payment", "label": "Payment", "layer": "business"}]

    def setUp(self):
        super().setUp()
        demo.write_operations(self.dir / "ops", NOW)
        self.answer = ops.with_comparison(ops.report(self.dir / "ops", self.ENTITIES, now=NOW), self.ENTITIES, [])
        self.c = self.answer["customers"]

    def test_no_subject_requester_agent_deal_or_company_leaves_the_records(self):
        text = json.dumps(self.answer)
        for needle in ("example.test", "Customer ", "Acme", "Ana Ruiz", "Ben Cole", "Where is my", "CSV export is",
                       "Team plan", "100001", "900001"):
            self.assertNotIn(needle, text, needle)
        tickets = next(x for x in self.answer["sources"] if x.get("stream") == "tickets")
        self.assertEqual(tickets["columns_matched_only"], ["subject"])
        self.assertNotIn("subject", tickets["columns_used"])

    def test_signals_and_what_two_sources_say_together(self):
        self.assertEqual({s["signal"] for s in self.c["signals"]},
                         {"TICKETS_RISING", "BACKLOG_GROWING", "RESOLUTION_SLOWER", "SATISFACTION_DROPPING",
                          "TOPIC_SPIKE", "WIN_RATE_DOWN", "LOSS_REASON_DOMINANT"})
        joined = {(f["finding"], f.get("topic")) for f in self.c["joined"]}
        self.assertIn(("REFUNDS_CONFIRMED_BY_TICKETS", None), joined)
        self.assertIn(("TOPIC_NOT_MODELED", "export_csv"), joined)
        mentions = self.c["recent"]["tickets"]["mentions"]
        self.assertEqual(mentions["Refund"], mentions["IssueRefund"])  # the same tickets, counted once each
        items = command_center.report({"state": {"nodes": [], "edges": []}, "operations": self.answer})["items"]
        top = next(i for i in items if i["kind"] == "REFUNDS_CONFIRMED_BY_TICKETS")
        self.assertEqual(top["category"], "Customers")
        self.assertGreater(top["score"], next(i for i in items if i["kind"] == "TICKETS_RISING")["score"])

    def test_bucket_medians(self):
        import customers
        bins = [0] * (len(customers.HOUR_BINS) + 1)
        for hours in (3, 5, 6, 7, 9, 10, 11, 13, 20, 30):
            customers.into(bins, customers.HOUR_BINS, hours)
        self.assertEqual(customers.median_of(bins, customers.HOUR_BINS), 9.3)  # exact median 9.5
        self.assertIsNone(customers.median_of([0] * 14, customers.HOUR_BINS))


class CustomersFormatTests(Folder):
    def test_a_freshdesk_export_and_a_salesforce_export_through_a_mapping(self):
        write(self.dir, "tickets.csv", "Ticket ID,Subject,Status,Priority,Source,Type,Created time,Resolved time,Tags,"
              "Contact\n1,Refund please,Resolved,Low,Email,Refund,2026-10-05 10:00:00,2026-10-05 16:00:00,,a@x.test\n"
              "2,Login,Open,High,Phone,Problem,2026-10-06 10:00:00,,,b@x.test\n"
              "3,Refund again,Closed,Low,Email,Refund,2026-10-07 10:00:00,2026-10-08 10:00:00,,c@x.test\n")
        write(self.dir, "opportunities.csv", "Opportunity Name,Account Name,Stage,Amount,Close Date,Created Date,Lead Source,"
              "Opportunity Owner\nBig deal,Initech,Closed Won,5000,10/08/2026,10/01/2026,Web,Pat\n"
              "Small deal,Globex,Closed Lost,900,10/09/2026,10/02/2026,Partner,Pat\n")
        write(self.dir, "operations.json", json.dumps({"files": [{"match": "opportunities.csv", "stream": "deals",
                                                                  "currency": "usd", "date_format": "%m/%d/%Y",
                                                                  "columns": {"stage": "Stage", "amount": "Amount",
                                                                              "close": "Close Date", "created": "Created Date",
                                                                              "source": "Lead Source"}}]}))
        answer = ops.report(self.dir, min_cell=1, now=NOW)
        c = answer["customers"]
        week = c["streams"]["tickets"][0]
        self.assertEqual((week["week"], week["opened"], week["solved"], week["topics"]),
                         ("2026-W41", 3, 2, {"refund": 2, "problem": 1}))
        self.assertEqual(c["totals"]["tickets"]["backlog"], 1)
        deals = c["totals"]["deals"]
        self.assertEqual((deals["won"], deals["lost"], deals["won_amount"], deals["sources"]),
                         (1, 1, {"usd": 5000.0}, {"web": 1, "partner": 1}))
        text = json.dumps(answer)
        for needle in ("Initech", "Globex", "Big deal", "Pat", "a@x.test", "Refund please"):
            self.assertNotIn(needle, text, needle)


class Fake(BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):
        Fake.seen.append((self.path, self.headers.get("Authorization")))
        body = Fake.routes.get(self.path.split("?")[0] + ("?" + self.path.split("?")[1] if "?" in self.path and
                                                           self.path.split("?")[0] + "?" + self.path.split("?")[1]
                                                           in Fake.routes else ""))
        if body is None:
            body = Fake.routes.get(self.path.split("?")[0])
        payload = json.dumps(body(self.path) if callable(body) else body).encode()
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class CustomerPullTests(Folder):
    def setUp(self):
        super().setUp()
        Fake.routes, Fake.seen = {}, []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def test_zendesk_pull(self):
        base = self.base

        def tickets(path):
            if "cursor=next" in path:
                return {"tickets": [{"id": 3, "created_at": "2026-10-07T10:00:00Z", "status": "open", "tags": ["login"],
                                     "subject": "cannot log in", "requester_id": 77}], "end_of_stream": True}
            return {"tickets": [{"id": 1, "created_at": "2026-10-05T10:00:00Z", "status": "solved", "tags": ["refund_request"],
                                 "subject": "Where is my refund", "via": {"channel": "email"},
                                 "satisfaction_rating": {"score": "bad"}},
                                {"id": 2, "created_at": "2026-10-06T10:00:00Z", "status": "solved", "tags": [],
                                 "type": "question", "subject": "Payment failed"}],
                    "metric_sets": [{"ticket_id": 1, "solved_at": "2026-10-05T14:00:00Z"},
                                    {"ticket_id": 2, "solved_at": "2026-10-06T12:00:00Z"}],
                    "after_url": f"{base}/api/v2/incremental/tickets/cursor.json?cursor=next", "end_of_stream": False}
        Fake.routes["/api/v2/incremental/tickets/cursor.json"] = tickets
        result = ops.pull_zendesk(base, {"Authorization": "Bearer zd"}, self.dir, 30, now=NOW,
                                  entities=CustomersDemoTests.ENTITIES)
        self.assertEqual(result["objects_read"], {"tickets": 3})
        written = (self.dir / "zendesk-api.ontologyex-aggregate.json").read_text()
        for needle in ("Where is my", "cannot log in", "requester", "Bearer"):
            self.assertNotIn(needle, written)
        c = ops.report(self.dir, min_cell=1, now=NOW)["customers"]
        week = c["streams"]["tickets"][0]
        self.assertEqual((week["opened"], week["solved"], week["bad"], week["mentions"]),
                         (3, 2, 1, {"Refund": 1, "IssueRefund": 1, "Payment": 1}))
        self.assertEqual(c["totals"]["tickets"]["backlog"], 1)
        self.assertTrue(all(auth == "Bearer zd" for _, auth in Fake.seen))
        Fake.routes["/api/v2/incremental/tickets/cursor.json"] = lambda path: {
            "tickets": [], "after_url": "https://elsewhere.example/api/next", "end_of_stream": False}
        with self.assertRaises(ops.OperationsError) as caught:
            ops.pull_zendesk(base, {"Authorization": "Bearer zd"}, self.dir, 30, now=NOW)
        self.assertIn("another host", str(caught.exception))

    def test_hubspot_pull(self):
        Fake.routes["/crm/v3/pipelines/deals"] = {"results": [{"stages": [
            {"id": "123", "metadata": {"isClosed": "true", "probability": "1.0"}},
            {"id": "124", "metadata": {"isClosed": "true", "probability": "0.0"}},
            {"id": "125", "metadata": {"isClosed": "false", "probability": "0.4"}}]}]}

        def deals(path):
            if "after=2" in path:
                return {"results": [{"properties": {"dealstage": "125", "amount": "300", "createdate": "2026-10-07T00:00:00Z",
                                                    "dealname": "Secret Co"}}]}
            return {"results": [{"properties": {"dealstage": "123", "amount": "1000", "deal_currency_code": "EUR",
                                                 "createdate": "2026-10-01T00:00:00Z", "closedate": "2026-10-06T00:00:00Z",
                                                 "hs_analytics_source": "ORGANIC_SEARCH", "dealname": "Secret Co"}},
                                 {"properties": {"dealstage": "124", "amount": "500", "deal_currency_code": "EUR",
                                                 "createdate": "2026-10-02T00:00:00Z", "closedate": "2026-10-07T00:00:00Z",
                                                 "closed_lost_reason": "Price"}}],
                    "paging": {"next": {"after": "2"}}}
        Fake.routes["/crm/v3/objects/deals"] = deals
        result = ops.pull_hubspot("pat-1", self.dir, now=NOW, base=self.base)
        self.assertEqual(result["objects_read"], {"deals": 3})
        self.assertNotIn("Secret Co", (self.dir / "hubspot-api.ontologyex-aggregate.json").read_text())
        deals = ops.report(self.dir, min_cell=1, now=NOW)["customers"]["totals"]["deals"]
        self.assertEqual((deals["won"], deals["lost"], deals["won_amount"], deals["lost_reasons"], deals["open"]),
                         (1, 1, {"eur": 1000.0}, {"price": 1}, 1))


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
