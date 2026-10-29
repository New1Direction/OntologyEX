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
    write("payouts.csv", ["id", "Amount", "Arrival Date (UTC)", "Created (UTC)", "Currency", "Status", "Destination"],
          [[ident("po_"), f"{2600 + rng.randrange(900)}.00", stamp(when(w, 3)), stamp(when(w, 1)), "usd",
            "failed" if w == 14 else "paid", "ba_" + "9" * 12] for w in range(16)])
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
    (folder / "bank").mkdir()
    write("bank/statement-october.csv", ["Posting Date", "Narrative", "Debit", "Credit"],
          [[stamp(when(w)), "CARD SETTLEMENT", "", "2900.00"] for w in range(3)])


def build(out: Path, now: datetime | None = None, unlinked: bool = False) -> dict:
    require(not out.exists(), "demo output already exists; choose a fresh directory")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    repo = out / "business"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / "compose.yml").write_text(COMPOSE, encoding="utf-8")
    (repo / "package.json").write_text('{"name":"shop-web","private":true}\n', encoding="utf-8")
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
