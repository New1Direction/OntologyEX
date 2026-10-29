#!/usr/bin/env python3
"""One-command local demo of the business operating picture. Synthetic data only.

Builds a fictional business repository, compiles the pre-authored payments domain
skill, declares binding-to-service links, writes synthetic health samples and
synthetic grade events, then (with --serve) opens the read-only control room.
No LLM, network, real payment system, or production observation is involved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ontology-extraction" / "scripts"))
import domain_skill as ds
import evidence
import evidence_grade
from policy_check import DomainError, canonical, require

spec = importlib.util.spec_from_file_location(
    "ontologyex_operating_picture_payments", ROOT / "examples/payments-domain/demo.py")
payments = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payments)

COMPOSE = """services:
  web:
    depends_on: [api]
  api:
    depends_on: [payments, db]
  payments:
    depends_on: [ledger, db]
  ledger:
    depends_on: [db]
  worker:
    depends_on: [queue, db]
  queue: {}
  db: {}
"""
SERVICE = "discovery:service:compose.yml:"
# Synthetic samples: (service, status, metrics). Nothing here is a real observation.
HEALTH = (("db", "ok", {"db_pool_utilization_pct": 41}), ("ledger", "ok", {}),
          ("payments", "error", {"error_rate_pct": 12.5}), ("api", "ok", {}),
          ("queue", "warning", {"queue_lag_seconds": 95}))
LINKS = (("payments.apply_refund", "payments", "Refund code is deployed in the payments service."),
         ("payments.Payment", "ledger", "Payment records are stored by the ledger service."),
         ("payments.Payment", "api", "The public API returns Payment records."))


def write_operations(folder: Path, now: datetime) -> None:
    """Sixteen weeks of synthetic payment-processor exports for the fictional shop, shaped like Stripe's
    dashboard exports, with the customer columns such exports carry (all fictional) so the tests can show
    they are never read. Refunds rise in the last four weeks, two disputes are open, one payout failed
    and subscription cancellations climb: things the operations page should find."""
    import csv
    import random
    rng = random.Random(20261029)
    folder.mkdir(parents=True)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    first = monday - timedelta(weeks=15)
    when = lambda week, day=None: min(now, first + timedelta(  # noqa: E731
        weeks=week, days=rng.randrange(7) if day is None else day, seconds=rng.randrange(86400)))
    stamp = lambda t: t.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    ident = lambda prefix: prefix + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(14))  # noqa: E731

    def write(name, header, rows):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        with (folder / name).open("w", newline="", encoding="utf-8") as handle:
            out = csv.writer(handle)
            out.writerow(header)
            out.writerows(rows)

    payments, refunds = [], []
    for week in range(16):
        for _ in range(55 + rng.randrange(15)):
            t, price = when(week), rng.choice(["19.00", "49.00", "99.00", "149.00"])
            customer = rng.randrange(400)
            failed = rng.random() < 0.08
            refunded = not failed and rng.random() < (0.07 if week >= 12 else 0.02)
            charge = ident("ch_")
            payments.append([charge, stamp(t), price, price if refunded else "0.00", "usd", "true" if not failed else "false",
                             f"{float(price) * 0.029 + 0.30:.2f}", "Failed" if failed else "Refunded" if refunded else "Paid",
                             "Payment complete." if not failed else "The bank declined it.", f"cus_{customer:06d}x",
                             f"customer{customer}@example.test", f"Customer {customer}",
                             rng.choice(["card_declined", "insufficient_funds", "expired_card"]) if failed else "",
                             f"Order #{rng.randrange(10**6)}"])
            if refunded:
                refunds.append([ident("re_"), price, charge, stamp(min(now, t + timedelta(days=2))), "usd",
                                rng.choice(["requested_by_customer", "duplicate", "fraudulent"]), "succeeded",
                                f"customer{customer}@example.test"])
    write("payments.csv", ["id", "Created date (UTC)", "Amount", "Amount Refunded", "Currency", "Captured", "Fee", "Status",
                           "Seller Message", "Customer ID", "Customer Email", "Customer Description", "Decline Reason",
                           "Description"], payments)
    write("refunds.csv", ["id", "Amount", "Charge ID", "Created (UTC)", "Currency", "Reason", "Status", "Customer Email"],
          refunds)
    write("disputes.csv", ["id", "Amount", "Charge ID", "Created (UTC)", "Currency", "Reason", "Status",
                           "Evidence Due By (UTC)"],
          [[ident("dp_"), "149.00", ident("ch_"), stamp(when(13)), "usd", "fraudulent", "needs_response", stamp(when(15, 6))],
           [ident("dp_"), "49.00", ident("ch_"), stamp(when(14)), "usd", "product_not_received", "under_review", ""],
           [ident("dp_"), "99.00", ident("ch_"), stamp(when(6)), "usd", "fraudulent", "won", ""]])
    payouts = [[ident("po_"), f"{2600 + rng.randrange(900)}.00", stamp(when(w, 3)), stamp(when(w, 1)), "usd",
                "failed" if w == 14 else "paid", "ba_" + "9" * 12] for w in range(16)]
    write("payouts.csv", ["id", "Amount", "Arrival Date (UTC)", "Created (UTC)", "Currency", "Status", "Destination"], payouts)
    subscriptions = []
    for n in range(90):
        plan, amount_, interval = rng.choice([("Starter", "19.00", "month"), ("Team", "99.00", "month"),
                                              ("Annual", "990.00", "year")])
        started = when(rng.randrange(16))
        ends = rng.random() < (0.35 if n % 3 == 0 else 0.08)
        ended = started + timedelta(days=rng.randrange(14, 60)) if ends else None
        if ended is not None and ended > monday + timedelta(days=6):
            ended = None
        subscriptions.append([ident("sub_"), f"cus_{n:06d}x", f"subscriber{n}@example.test", plan, f"Shop {plan}", "1",
                              interval, amount_, "usd", "canceled" if ended else "active", stamp(started), stamp(started),
                              stamp(ended) if ended else "", stamp(ended) if ended else ""])
    write("subscriptions.csv", ["id", "Customer ID", "Customer Email", "Plan", "Product", "Quantity", "Interval", "Amount",
                                "Currency", "Status", "Created (UTC)", "Start (UTC)", "Canceled At (UTC)",
                                "Ended At (UTC)"], subscriptions)
    write("invoices.csv", ["id", "Amount Due", "Amount Paid", "Currency", "Customer Email", "Date (UTC)", "Status",
                           "Subscription"],
          [[ident("in_"), amount_, "0.00" if status != "paid" else amount_, "usd", f"subscriber{rng.randrange(90)}@example.test",
            stamp(when(rng.randrange(16))), status, ident("sub_")]
           for amount_, status in [(rng.choice(["19.00", "99.00"]), rng.choice(["paid"] * 12 + ["open", "uncollectible"]))
                                   for _ in range(120)]])
    write_spend(folder, now, payouts, rng, write)
    write_customers(folder, first, now, rng, write)
    write_written(folder)


def write_written(folder: Path) -> None:
    """What the shop says it does: a refund policy, terms of service (a Word file), a support page, a finance policy
    and a supplier's order form, all fictional. The terms give a shorter refund window than the policy, the support
    promise is not kept, a supplier bill is overdue against the finance policy, and nothing describes how disputes
    or payouts are handled."""
    import zipfile
    docs = folder / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "refund-policy.md").write_text(
        "# Refund policy\n\nCustomers can ask for a refund within 30 days of purchase.\n\n"
        "Refunds over $500 need a manager's approval before they are issued.\n\n"
        "Questions go to support@shop.example.test or +1 555 010 2233.\n", encoding="utf-8")
    (docs / "support.md").write_text(
        "# Support\n\nWe answer by email and chat. We resolve every support ticket within 8 hours.\n", encoding="utf-8")
    (docs / "finance-policy.md").write_text(
        "# Finance policy\n\nWe pay supplier invoices within 30 days of receiving them.\n", encoding="utf-8")
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in (
        "Terms of Service", "1. Payments are taken when you order.",
        "2. Refunds are available within 14 days of purchase.", "3. These terms are governed by local law."))
    with zipfile.ZipFile(docs / "terms-of-service.docx", "w") as z:  # fixed timestamps: the same bytes every build
        z.writestr(zipfile.ZipInfo("[Content_Types].xml", (1980, 1, 1, 0, 0, 0)),
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr(zipfile.ZipInfo("word/document.xml", (1980, 1, 1, 0, 0, 0)),
                   '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/'
                   f'main"><w:body>{body}</w:body></w:document>')
    (folder / "contracts").mkdir(exist_ok=True)
    (folder / "contracts/datadog-order-form.txt").write_text(
        "ORDER FORM\nCustomer: Fictional Shop Ltd\nTerm: 12 months. This order renews automatically for another 12 "
        "months unless either party gives 60 days' written notice.\nPayment terms: Net 30.\n", encoding="utf-8")


def write_customers(folder: Path, first: datetime, now: datetime, rng, write) -> None:
    """Sixteen weeks of a helpdesk export and a CRM deals export, shaped like Zendesk's and HubSpot's, with the
    requester, company and owner columns such exports carry (all fictional) so the tests can show they are never
    kept. In the last four weeks refund questions climb with the refunds, CSV export tickets spike (nothing in the
    model is named for exporting), ratings and solve times get worse, and deals are lost to price."""
    tickets = []
    subjects = {"refund_request": ["Where is my refund?", "Refund for order {n}", "Please refund the duplicate charge"],
                "billing": ["Charged twice for one payment", "Invoice copy for my payment", "Update the card on file"],
                "login": ["Cannot log in", "Password reset email never arrives", "Locked out of my account"],
                "export_csv": ["CSV export is empty", "Export to CSV times out", "Exported CSV has wrong dates"]}
    for week in range(16):
        late = week >= 12
        for _ in range((40 if late else 24) + rng.randrange(6)):
            tag = rng.choices(list(subjects), weights=[5 if late else 2, 3, 3, 4 if late else 0.2])[0]
            created = min(now, first + timedelta(weeks=week, days=rng.randrange(7), seconds=rng.randrange(86400)))
            hours = rng.uniform(2, 40 if late else 14)
            solved = created + timedelta(hours=hours)
            open_ = solved > now or rng.random() < (0.12 if late else 0.03)
            rating = rng.choices(["good", "bad", ""], weights=[3, 2 if late else 0.4, 5])[0]
            tickets.append([str(100000 + len(tickets)), rng.choice(subjects[tag]).format(n=f"#{rng.randrange(10**5)}"),
                            "open" if open_ else "solved", rng.choice(["low", "normal", "normal", "high"]),
                            f"Customer {rng.randrange(900)}", f"buyer{rng.randrange(900)}@example.test",
                            created.strftime("%Y-%m-%d %H:%M:%S +0000"),
                            "" if open_ else solved.strftime("%Y-%m-%d %H:%M:%S +0000"),
                            f"{tag} vip" if rng.random() < 0.1 else tag, rng.choice(["email", "web", "chat"]), rating,
                            rng.choice(["Ana Ruiz", "Ben Cole"])])
    write("support/zendesk-tickets.csv", ["Id", "Subject", "Status", "Priority", "Requester", "Requester email", "Created at",
                                          "Solved at", "Tags", "Via", "Satisfaction Score", "Assignee"], tickets)
    deals = []
    for week in range(16):
        late = week >= 10
        for _ in range(7 + rng.randrange(4)):
            created = min(now, first + timedelta(weeks=week, days=rng.randrange(7)))
            roll = rng.random()
            stage = ("closedwon" if roll < (0.12 if late else 0.35) else "closedlost" if roll < 0.75 else
                     rng.choice(["appointmentscheduled", "contractsent"]))
            closed = min(now, created + timedelta(days=rng.randrange(3, 25))) if stage.startswith("closed") else None
            reason = rng.choices(["Price", "Went with a competitor", "No budget", "Timing"],
                                 weights=[6 if late else 1, 2, 2, 1])[0] if stage == "closedlost" else ""
            deals.append([str(900000 + len(deals)), f"Acme Widgets {rng.randrange(400)} - Team plan", stage,
                          f"{rng.choice([1188, 2388, 5940])}.00", "USD", created.strftime("%Y-%m-%d %H:%M"),
                          closed.strftime("%Y-%m-%d %H:%M") if closed else "", rng.choice(["Ana Ruiz", "Ben Cole"]),
                          f"Acme Widgets {rng.randrange(400)}", rng.choice(["Organic search", "Referrals", "Paid search"]),
                          reason])
    write("crm/hubspot-deals.csv", ["Record ID", "Deal Name", "Deal Stage", "Amount", "Deal currency code", "Create Date",
                                    "Close Date", "Deal owner", "Associated Company", "Original Source",
                                    "Closed Lost Reason"], deals)


def write_spend(folder: Path, now: datetime, payouts: list, rng, write) -> None:
    """Six months of a business bank account, vendor bills and an AWS cost and usage report, all synthetic.
    Payroll lines name fictional people (never kept); one paid payout never reaches the bank; spending jumps in
    the last complete month, OpenAI is paid for the first time then, and EC2 costs climb."""
    month0 = (now.replace(day=1) - timedelta(days=150)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = []
    at = month0
    while at <= now:
        months.append(at)
        at = (at + timedelta(days=32)).replace(day=1)
    last_complete = months[-2]
    day = lambda m, d: min(now, m + timedelta(days=d - 1, hours=10))  # noqa: E731
    stamp = lambda t: t.strftime("%d/%m/%Y")  # noqa: E731
    lines = []
    for i, m in enumerate(months):
        lines += [[stamp(day(m, 1)), "WEWORK MEMBERSHIP", "Rent", "-2400.00"],
                  [stamp(day(m, 3)), f"AWS EMEA aws.amazon.com {rng.randrange(10**8)}", "Hosting",
                   f"-{1150 + 60 * i + rng.randrange(40)}.00"],
                  [stamp(day(m, 5)), "SENDGRID INC", "Software", "-89.95"],
                  [stamp(day(m, 7)), "TWILIO INC SAN FRANCISCO", "Software", f"-{110 + rng.randrange(30)}.40"],
                  [stamp(day(m, 15)), "PAYROLL Jordan Avery", "Payroll", "-4800.00"],
                  [stamp(day(m, 15)), "PAYROLL Sam Okafor", "Payroll", "-4650.00"],
                  [stamp(day(m, 20)), "STAPLES 0042", "Office", f"-{40 + rng.randrange(60)}.10"],
                  [stamp(day(m, 22)), "REFUND TO CUSTOMER Riley Chen", "Refunds", "-49.00"]]
        if m == last_complete:
            lines += [[stamp(day(m, 9)), "OPENAI *CHATGPT API", "Software", "-120.00"],
                      [stamp(day(m, 18)), "HARTWELL & CO LEGAL", "Legal", "-9800.00"]]
    for row in payouts:  # each paid payout lands as a deposit from Stripe, except one
        if row[5] == "paid" and row is not payouts[3]:
            landed = datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")
            lines.append([landed.strftime("%d/%m/%Y"), "STRIPE TRANSFER ST-" + str(rng.randrange(10**9)), "Sales", row[1]])
    lines = [line for line in lines if datetime.strptime(line[0], "%d/%m/%Y") <= now.replace(tzinfo=None)]
    (folder / "bank").mkdir()
    write("bank/business-account.csv", ["Date", "Description", "Category", "Amount"], sorted(
        lines, key=lambda r: datetime.strptime(r[0], "%d/%m/%Y")))
    (folder / "operations.json").write_text(json.dumps({"files": [{
        "match": "bank/*.csv", "stream": "transactions", "currency": "usd", "date_format": "%d/%m/%Y",
        "columns": {"created": "Date", "description": "Description", "category": "Category", "amount": "Amount"}}]},
        indent=2) + "\n", encoding="utf-8")
    bills = []
    for m in months:
        bills.append([m.strftime("%Y-%m-%d"), "Datadog Inc", "450.00", "USD", (m + timedelta(days=30)).strftime("%Y-%m-%d"),
                      "open" if m == months[-2] else "paid"])
        bills.append([m.strftime("%Y-%m-%d"), "Hartwell & Co", "1200.00", "USD", (m + timedelta(days=14)).strftime("%Y-%m-%d"),
                      "paid"])
    write("bills.csv", ["Bill Date", "Vendor", "Amount", "Currency", "Due Date", "Status"],
          [b for b in bills if b[0] <= now.strftime("%Y-%m-%d")])
    usage = []
    for m in months:
        growth = 2.0 if m == last_complete else 1.0
        for d in range(0, 31, 3):
            t = m + timedelta(days=d)
            if t.month != m.month or t > now:
                continue
            for code, name, base in (("AmazonEC2", "Amazon Elastic Compute Cloud", 90 * growth),
                                     ("AmazonRDS", "Amazon Relational Database Service", 45),
                                     ("AmazonS3", "Amazon Simple Storage Service", 9)):
                usage.append([t.strftime("%Y-%m-%dT00:00:00Z"), code, name, f"{base + rng.random() * 5:.4f}", "USD",
                              f"arn:aws:ec2:eu-west-1:{rng.randrange(10**12):012d}:instance/i-{rng.randrange(16**12):012x}",
                              f"{rng.randrange(10**12):012d}"])
    (folder / "cloud").mkdir()
    write("cloud/aws-cost-and-usage.csv", ["lineItem/UsageStartDate", "lineItem/ProductCode", "product/ProductName",
                                           "lineItem/UnblendedCost", "lineItem/CurrencyCode", "lineItem/ResourceId",
                                           "lineItem/UsageAccountId"], usage)


def build(out: Path, now: datetime | None = None, unlinked: bool = False) -> dict:
    require(not out.exists(), "demo output already exists; choose a fresh directory")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    repo = out / "business"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / "compose.yml").write_text(COMPOSE, encoding="utf-8")
    (repo / "package.json").write_text(json.dumps({"name": "shop-web", "private": True, "dependencies": {
        "@sendgrid/mail": "^8.1.0", "@sentry/node": "^8.0.0"}}) + "\n", encoding="utf-8")
    (repo / ".github/workflows/ci.yml").write_text(
        "name: ci\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo test\n",
        encoding="utf-8")
    domain = repo / "domain"
    shutil.copytree(ROOT / "examples/payments-domain/source", domain,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    workspace = out / "workspace"
    ds.prepare(domain, ["requirements.md", "payments.py"], workspace, payments.NAME, payments.GOAL)
    payments.populate(workspace)
    # Cite the implementation of the remaining-balance check too, so one rule is multi-kind.
    code = evidence.record(workspace, "payments.py", 25, 26, "E-remaining-code", "implementation")
    contract = json.loads((workspace / "60-contract.json").read_text(encoding="utf-8"))
    contract["evidence"].append(code)
    for rule in contract["rules"]:
        if rule["id"] == "remaining-balance":
            rule["evidence"].append("E-remaining-code")
    (workspace / "60-contract.json").write_bytes(canonical(contract))
    skill = out / payments.NAME
    ds.build(workspace, skill, domain)

    links = out / "links.json"
    links.write_bytes(canonical({"schema_version": 1, "kind": "ONTOLOGYEX_SYSTEM_LINKS", "links": [] if unlinked else [
        {"skill": payments.NAME, "binding": binding, "component": SERVICE + service,
         "declared_by": "demo (synthetic)", "note": note} for binding, service, note in LINKS]}))
    stamp = now.isoformat().replace("+00:00", "Z")
    health = out / "health.jsonl"
    health.write_text("".join(json.dumps({
        "schema_version": 1, "component_id": SERVICE + name, "observed_at": stamp,
        "status": status, "source": "synthetic", "expires_after_seconds": 86400,
        "metrics": metrics}, sort_keys=True) + "\n" for name, status, metrics in HEALTH), encoding="utf-8")
    events = out / "events.json"
    evidence_grade.record(skill, events, "remaining-balance", "REVIEW_SUPPORTS", "demo (synthetic)",
                          "requirements.md and payments.py state the same limit")
    evidence_grade.record(skill, events, "remaining-balance", "BEHAVIOR_CHECK_PASSED", "demo (synthetic)",
                          "examples/payments-domain fixture checks")
    evidence_grade.record(skill, events, "positive-amount", "REVIEW_SUPPORTS", "demo (synthetic)", "")
    write_operations(repo / ".ontologyex/operations", now)  # exports, read as aggregates (REPO/.ontologyex/operations)
    command = [sys.executable, str(ROOT / "ontology-extraction/scripts/control_room.py"),
               "--repo", str(repo), "--skill", str(skill), "--links", str(links),
               "--events", str(events), "--observations", str(health)]
    return {"status": "DEMO_BUILT", "data": "SYNTHETIC_NOT_PRODUCTION", "repo": str(repo),
            "skill": str(skill), "links": str(links), "events": str(events), "observations": str(health),
            "command": command}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "build/operating-picture")
    parser.add_argument("--serve", action="store_true", help="start the local control room")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--start-unlinked", action="store_true",
                        help="start with no links and confirm suggested ones in the page")
    args = parser.parse_args()
    try:
        result = build(args.out, unlinked=args.start_unlinked)
    except (DomainError, OSError, ValueError) as exc:
        print(canonical({"status": "ERROR", "error": str(exc)}).decode(), end="")
        return 1
    print(canonical(result).decode(), end="")
    if args.serve:
        import control_room
        print(f"Open http://127.0.0.1:{args.port}/business", flush=True)
        control_room.serve(Path(result["repo"]), Path(result["observations"]), None, args.port,
                           skills=[Path(result["skill"])], links=Path(result["links"]),
                           events=Path(result["events"]), allow_link_decisions=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
