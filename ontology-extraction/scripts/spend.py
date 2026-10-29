"""Spend: where the money goes, from bank and card statements, vendor bills and cloud cost exports.

Read by operations.py from the same folder, as monthly aggregates per currency:

- transactions (bank, card or ledger lines): money in and out per month, out by category
  (the business's own chart of accounts) and by known supplier, and deposits from payment
  processors;
- bills (accounts payable): billed, open and overdue per month, by known supplier;
- cloud costs (AWS cost and usage reports, Google Cloud billing exports, Azure cost
  exports): cost per month by provider and service.

What is kept from a line: its date, amount, currency and category, and a supplier's name
only when the line names a supplier OntologyEX knows (the same catalog the supplier map
uses, matched in the payee and description, which are read for that and nothing else).
Any other payee (a person paid, a shop, a customer refunded) is never kept: it counts
under its category only. Amounts of a month with fewer than `min_cell` lines are withheld
when shown, like operations.

With the supplier map and the payment records beside them, spend answers what the code
alone cannot: which suppliers are paid that the code does not use (or are used outside it),
which suppliers the code uses that nobody pays (a free tier, another account, or unused),
whether processor payouts reached the bank, and what the cloud costs against what the
business takes in.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re

import suppliers

STREAMS = ("transactions", "bills", "cloud_costs")
TITLES = {"transactions": "Bank and card", "bills": "Bills", "cloud_costs": "Cloud costs"}
MAX_LABELS = 40

COLUMNS = {
    "created": ("date", "transaction_date", "posting_date", "posted_date", "booking_date", "value_date", "posted",
                "bill_date", "issue_date", "invoice_date", "date_issued", "created"),
    "amount": ("amount", "transaction_amount", "value", "total", "bill_amount", "amount_due", "net_amount"),
    "debit": ("debit", "debits", "withdrawal", "withdrawals", "money_out", "paid_out", "outflow", "debit_amount"),
    "credit": ("credit", "credits", "deposit", "deposits", "money_in", "paid_in", "inflow", "credit_amount"),
    "payee": ("payee", "vendor", "supplier", "merchant", "merchant_name", "counterparty", "vendor_name", "contact",
              "name", "payee_name"),
    "description": ("description", "narrative", "memo", "details", "transaction_description", "particulars",
                    "reference", "transaction_details"),
    "category": ("category", "account", "account_name", "gl_account", "expense_category", "expense_account", "class"),
    "currency": ("currency", "currency_code", "billing_currency", "billingcurrency", "lineitem_currencycode"),
    "due": ("due_date", "due", "payment_due", "date_due"),
    "status": ("status", "payment_status", "bill_status"),
    "cost": ("lineitem_unblendedcost", "cost", "costinbillingcurrency", "pretaxcost", "unblended_cost"),
    "service": ("product_productname", "lineitem_productcode", "service_description", "service", "servicename",
                "metercategory", "consumedservice", "service_name"),
    "usage_start": ("lineitem_usagestartdate", "usage_start_date", "usage_start_time", "date", "usagedate",
                    "invoice_month", "billing_period_start"),
}
NAME_HINTS = (("bill", "bills"), ("payable", "bills"), ("statement", "transactions"), ("bank", "transactions"),
              ("transactions", "transactions"), ("card", "transactions"), ("ledger", "transactions"),
              ("expense", "transactions"), ("cost", "cloud_costs"), ("billing", "cloud_costs"), ("cur", "cloud_costs"))
# The payee or description of a bank line, as it usually reads, for suppliers whose names do not.
ALIASES = {"Amazon Web Services": r"\baws\b|amazon web serv", "Google Cloud": r"google\W{0,3}cloud|\bgcp\b",
           "Microsoft Azure": r"\bazure\b|msft\W{0,3}azure", "Stripe": r"\bstripe\b", "PayPal": r"paypal|braintree",
           "Google Maps": r"google\W{0,3}maps", "Google AI": r"google\W{0,3}(ai|gemini)", "OpenAI": r"openai|chatgpt",
           "Microsoft 365": r"microsoft\W{0,3}365|\bo365\b", "Trigger.dev": r"trigger\.dev",
           "New Relic": r"new\W?relic", "Better Stack": r"better\W?stack|logtail"}
# Names too common to find in a bank line without mistaking a shop or a word for the supplier.
AMBIGUOUS = {"Square", "Persona", "Neon", "Resend", "Clerk", "Polar", "Axiom", "MX", "Teller", "Segment", "Svix",
             "Novu", "Groq", "Replicate", "Inngest", "Kinde", "Mistral", "Cohere", "Mollie", "Alpaca", "Plaid"}
PAID_STATUSES = {"paid", "settled", "complete", "completed", "closed"}


def _pattern(name: str) -> re.Pattern | None:
    if name in AMBIGUOUS:
        return None
    return re.compile(ALIASES.get(name, r"\b" + re.escape(name.lower()).replace(r"\ ", r"\W{0,3}") + r"\b"), re.I)


KNOWN = [(name, category, _pattern(name)) for name, category, *_ in suppliers.VENDORS if _pattern(name) is not None]
PROCESSOR_CATEGORIES = {"payments", "banking and finance data"}


def known_supplier(*texts: str) -> tuple[str, str] | None:
    """The catalog supplier a payee or description names, if any: (name, category)."""
    text = " ".join(t for t in texts if t)
    for name, category, pattern in KNOWN:
        if pattern.search(text):
            return name, category
    return None


def month_of(when: datetime) -> str:
    return when.strftime("%Y-%m")


def cell(stream: str) -> dict:
    zero = Decimal(0)
    return {"records": 0, **{
        "transactions": {"in": zero, "out": zero, "count_in": 0, "count_out": 0, "categories": {}, "suppliers": {},
                         "processor_deposits": {}},
        "bills": {"count": 0, "amount": zero, "open": 0, "open_amount": zero, "overdue": 0, "overdue_amount": zero,
                  "suppliers": {}},
        "cloud_costs": {"cost": zero, "services": {}, "providers": {}},
    }[stream]}


def add_to(counter: dict, key: str, value: Decimal) -> None:
    key = key if key in counter or len(counter) < MAX_LABELS else "other"
    counter[key] = counter.get(key, Decimal(0)) + value


def find_columns(headers: list[str], norm) -> dict[str, str]:
    """Canonical column -> header, for the spend columns a file has."""
    by_norm = {norm(h): h for h in headers}
    found = {}
    for canonical, names in COLUMNS.items():
        for name in names:
            if name in by_norm and by_norm[name] not in found.values():
                found[canonical] = by_norm[name]
                break
    return found


def recognize(rel: str, found: dict[str, str]) -> tuple[str | None, str]:
    """(stream, how) from the canonical columns a file has."""
    have = set(found)
    lower = rel.lower()
    if {"cost", "service"} <= have and ("usage_start" in have or "created" in have):
        return "cloud_costs", "columns"
    if {"payee", "created"} <= have and ("amount" in have) and ("due" in have or ("status" in have and "bill" in lower)):
        return "bills", "columns"
    if "created" in have and ({"amount", "debit", "credit"} & have) and ({"payee", "description", "category"} & have):
        hinted = next((stream for hint, stream in NAME_HINTS if hint in lower), None)
        return "transactions", "file name and columns" if hinted == "transactions" else "columns"
    return None, ""


def provider_of(headers: list[str]) -> str:
    joined = " ".join(h.lower() for h in headers)
    if "lineitem/" in joined or "lineitem_" in joined:
        return "Amazon Web Services"
    if "service description" in joined or "sku description" in joined:
        return "Google Cloud"
    if "metercategory" in joined or "costinbillingcurrency" in joined or "consumedservice" in joined:
        return "Microsoft Azure"
    return "cloud"


class Book:
    """Monthly aggregates of where the money goes; no line is kept."""

    def __init__(self, now: datetime):
        self.cells: dict[tuple[str, str, str], dict] = {}
        self.now = now
        self.first_paid: dict[tuple[str, str], str] = {}  # (currency, supplier) -> first month paid

    def at(self, stream: str, currency: str, month: str) -> dict:
        key = (stream, currency, month)
        if key not in self.cells:
            self.cells[key] = cell(stream)
        return self.cells[key]

    def add(self, stream: str, r: dict, label) -> bool:
        currency = (r.get("currency") or "").strip().lower()[:3]
        when = r.get("created")
        if not re.fullmatch(r"[a-z]{3}", currency) or when is None:
            return False
        month = month_of(when)
        c = self.at(stream, currency, month)
        c["records"] += 1
        if stream == "cloud_costs":
            cost = r.get("cost") or Decimal(0)
            c["cost"] += cost
            add_to(c["services"], label(r.get("service") or "unspecified"), cost)
            add_to(c["providers"], r.get("provider") or "cloud", cost)
            return True
        supplier = known_supplier(r.get("payee") or "", r.get("description") or "")
        if stream == "bills":
            value = abs(r.get("amount") or Decimal(0))
            c["count"] += 1
            c["amount"] += value
            if supplier:
                add_to(c["suppliers"], supplier[0], value)
                self.first_paid.setdefault((currency, supplier[0]), month)
            status = (r.get("status") or "").strip().lower()
            if status not in PAID_STATUSES:
                c["open"] += 1
                c["open_amount"] += value
                if r.get("due") is not None and r["due"] < self.now:
                    c["overdue"] += 1
                    c["overdue_amount"] += value
            return True
        value = r.get("amount")
        if value is None:
            value = (r.get("credit") or Decimal(0)) - (r.get("debit") or Decimal(0))
        if value >= 0:
            c["in"] += value
            c["count_in"] += 1
            if supplier and supplier[1] in PROCESSOR_CATEGORIES:
                add_to(c["processor_deposits"], supplier[0], value)
        else:
            out = -value
            c["out"] += out
            c["count_out"] += 1
            add_to(c["categories"], label(r.get("category") or "uncategorized"), out)
            if supplier:
                add_to(c["suppliers"], supplier[0], out)
                self.first_paid.setdefault((currency, supplier[0]), month)
        return True


def months_between(first: str, last: str) -> list[str]:
    out, (y, m) = [], (int(first[:4]), int(first[5:]))
    while f"{y:04d}-{m:02d}" <= last and len(out) < 240:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def plain(value):
    if isinstance(value, Decimal):
        return float(round(value, 2))
    if isinstance(value, dict):
        return {k: plain(v) for k, v in sorted(value.items(), key=lambda kv: (-kv[1], kv[0]) if isinstance(kv[1], Decimal)
                                                 else (0, kv[0]))}
    return value


def shown(c: dict, min_cell: int, stream: str = "") -> dict:
    """A month as shown. Cloud costs describe services, not people, so they are never withheld."""
    out = plain(c)
    if c["records"] < min_cell and stream != "cloud_costs":
        for k, v in c.items():
            if isinstance(v, (Decimal, dict)):
                out[k] = None
        out["withheld"] = True
    return out


def section(book: Book, min_cell: int, payouts_by_month: dict, payments_by_month: dict) -> dict:
    """The spend part of the operations ledger: months, totals, findings and signals per currency."""
    current = month_of(book.now)
    currencies, signals = {}, []
    for currency in sorted({c for (_, c, _) in book.cells}):
        series = {s: sorted((m, c) for (st, cur, m), c in book.cells.items() if st == s and cur == currency)
                  for s in STREAMS}
        months = sorted({m for rows in series.values() for m, _ in rows})
        span = months_between(months[0], months[-1])
        series = {s: [(m, dict(rows).get(m) or cell(s)) for m in span] if rows else [] for s, rows in series.items()}
        complete = lambda rows: [(m, c) for m, c in rows if m < current]  # noqa: E731
        totals = {}
        for s, rows in series.items():
            if not rows:
                continue
            total = cell(s)
            for _, c in rows:
                for k, v in c.items():
                    if isinstance(v, dict):
                        for key, amount in v.items():
                            total[k][key] = total[k].get(key, Decimal(0)) + amount
                    else:
                        total[k] += v
            totals[s] = shown(total, min_cell, s)
        paid = {}  # supplier -> money out and billed, all months
        for s in ("transactions", "bills"):
            for _, c in series.get(s, []):
                for name, amount in c["suppliers"].items():
                    paid[name] = paid.get(name, Decimal(0)) + amount
        tx = complete(series.get("transactions", []))
        if len(tx) >= 4:
            last, before = tx[-1][1]["out"], [c["out"] for _, c in tx[-4:-1]]
            mean = sum(before, Decimal(0)) / 3
            if mean and last > Decimal("1.3") * mean:
                signals.append({"signal": "SPEND_RISING", "level": "HIGH", "currency": currency, "stream": "transactions",
                                "detail": f"money out in {tx[-1][0]} was {float(last / mean - 1):.0%} above the average "
                                          f"of the three months before"})
        cloud = complete(series.get("cloud_costs", []))
        if len(cloud) >= 2:
            last, prev = cloud[-1][1]["cost"], cloud[-2][1]["cost"]
            if prev and last > Decimal("1.25") * prev and last - prev >= 100:
                signals.append({"signal": "CLOUD_COST_RISING", "level": "MEDIUM", "currency": currency,
                                "stream": "cloud_costs",
                                "detail": f"cloud costs in {cloud[-1][0]} were {float(last / prev - 1):.0%} above "
                                          f"{cloud[-2][0]}"})
        overdue = [(m, c) for m, c in series.get("bills", []) if c["overdue"]]
        if overdue:
            count = sum(c["overdue"] for _, c in overdue)
            signals.append({"signal": "BILLS_OVERDUE", "level": "HIGH", "currency": currency, "stream": "bills",
                            "detail": f"{count} bill(s) are past their due date and not marked paid"})
        if len(tx) >= 4:
            for (cur, name), month in sorted(book.first_paid.items()):
                if cur == currency and month == tx[-1][0]:
                    signals.append({"signal": "NEW_SUPPLIER_PAID", "level": "MEDIUM", "currency": currency,
                                    "stream": "transactions",
                                    "detail": f"{name} was paid for the first time in {month}"})
        # Did processor payouts reach the bank? Months both cover, the last one left out (payouts land late).
        deposits = {m: sum(c["processor_deposits"].values(), Decimal(0)) for m, c in tx}
        payouts = {m: Decimal(str(v)) for m, v in (payouts_by_month.get(currency) or {}).items() if m < current}
        both = sorted(set(deposits) & set(payouts))[:-1]
        reconciliation = None
        if both:
            sent, landed = sum((payouts[m] for m in both), Decimal(0)), sum((deposits[m] for m in both), Decimal(0))
            gap = sent - landed
            reconciliation = {"months": both, "payouts": float(round(sent, 2)), "deposits": float(round(landed, 2)),
                              "gap": float(round(gap, 2)),
                              "note": "processor payouts against deposits from payment processors in the bank lines, "
                                      "over the months both cover; rough, since a payout can land a few days later"}
            if sent and abs(gap) > max(Decimal(50), Decimal("0.02") * sent):
                signals.append({"signal": "PAYOUTS_NOT_IN_BANK" if gap > 0 else "DEPOSITS_WITHOUT_PAYOUTS",
                                "level": "HIGH", "currency": currency, "stream": "transactions",
                                "detail": (f"processor payouts of {float(sent):,.2f} over {len(both)} month(s) and "
                                           f"{float(landed):,.2f} deposited from processors: {float(abs(gap)):,.2f} "
                                           + ("is not in the bank lines" if gap > 0 else "more arrived than was paid out"))})
        # What the cloud costs against what the business takes in, last complete month.
        economics = None
        if cloud and payments_by_month.get(currency):
            month = cloud[-1][0]
            taken = Decimal(str(payments_by_month[currency].get(month, 0)))
            if taken:
                economics = {"month": month, "cloud_cost": float(round(cloud[-1][1]["cost"], 2)),
                             "payments_taken": float(round(taken, 2)),
                             "cloud_share": float(round(cloud[-1][1]["cost"] / taken, 4))}
        currencies[currency] = {
            "first_month": span[0], "last_month": span[-1],
            "months": {s: [{"month": m, **shown(c, min_cell, s)} for m, c in rows] for s, rows in series.items() if rows},
            "totals": totals,
            "suppliers_paid": [{"name": n, "amount": float(round(a, 2)), "category": category_of(n)}
                               for n, a in sorted(paid.items(), key=lambda kv: -kv[1])],
            "reconciliation": reconciliation, "unit_economics": economics}
    return {"currencies": currencies, "signals": signals, "suppliers_vs_spend": []}


def category_of(name: str) -> str | None:
    return next((category for n, category, *_ in suppliers.VENDORS if n == name), None)


def compare(section_: dict, code_suppliers: list[dict]) -> list[dict]:
    """Suppliers paid against suppliers the code uses, once there are two months of bank lines."""
    if not section_ or not section_.get("currencies"):
        return []
    months = max(len(c["months"].get("transactions", [])) for c in section_["currencies"].values())
    if months < 2:
        return []
    code = {s["name"]: s.get("category") for s in code_suppliers}
    paid = {}
    for currency, c in section_["currencies"].items():
        for row in c["suppliers_paid"]:
            paid.setdefault(row["name"], []).append((currency, row["amount"]))
    out = []
    for name, amounts in sorted(paid.items()):
        spent = ", ".join(f"{a:,.2f} {cur.upper()}" for cur, a in amounts)
        if name in code:
            out.append({"finding": "PAID_AND_IN_CODE", "supplier": name,
                        "detail": f"the code uses {name}, and it is paid ({spent} in these records)"})
        elif category_of(name) not in PROCESSOR_CATEGORIES:
            out.append({"finding": "PAID_NOT_IN_CODE", "supplier": name,
                        "detail": f"{name} is paid ({spent} in these records) and the code does not use it: a tool "
                                  "people use, a service set up outside this repository, or one nobody needs any more"})
    for name in sorted(set(code) - set(paid)):
        if code[name] in PROCESSOR_CATEGORIES:
            continue  # processors take their fees out of payouts; they are rarely paid from the bank
        out.append({"finding": "IN_CODE_NOT_PAID", "supplier": name,
                    "detail": f"the code uses {name}, and these records show no payment to it: a free plan, paid "
                              "from another account or card, or not set up for production"})
    return out
