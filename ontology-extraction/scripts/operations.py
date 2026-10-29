#!/usr/bin/env python3
"""Operations: what the business actually does, read from its own records, kept as aggregates.

The code says what a business can do. Its records say what it does: the payments it
takes, the refunds it gives, the disputes it fights, the payouts it receives, the
subscriptions that start and end, the invoices that go unpaid. This module reads those
records (exports dropped in a folder, or a read-only pull) and keeps only weekly
aggregates per currency: counts, totals and rates, never a row.

What is read, and what is not:
- From each file only the columns a stream needs are read (amounts, dates, statuses,
  reasons, plans and intervals). Customer names, emails, IDs, card details,
  descriptions and any other free text are never read, kept or shown.
- Nothing row-level is written anywhere. A pull from an API aggregates each page as it
  arrives and writes only the aggregates.
- Shown amounts and reason breakdowns are withheld for any week with fewer than
  `min_cell` records (default 3), so a total never stands for one person's payment.

Recognized files, in the operations folder (default REPO/.ontologyex/operations):
- Stripe dashboard exports (CSV): payments, refunds, disputes, payouts, subscriptions,
  invoices; recognized by file name and columns;
- Stripe API objects saved as JSON (a list object, or a list of objects);
- other processors' CSVs through a mapping in `operations.json` in the same folder:
  {"files": [{"match": "paypal*.csv", "stream": "payments", "columns": {"amount": "Gross",
  "currency": "Currency", "created": "Date", "status": "Status"}, "date_format": "%m/%d/%Y",
  "status_map": {"Completed": "succeeded"}}]};
- aggregates written by `operations.py pull stripe`.

Signals compare the last four complete weeks with the four before; the week in progress
is shown but never compared, since it is not over. Given the same files and the same day,
the answer is the same.

    python operations.py report --dir REPO/.ontologyex/operations [--repo REPO] [--format markdown]
    STRIPE_API_KEY=rk_... python operations.py pull stripe --out REPO/.ontologyex/operations [--days 180]
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import customers
import spend
import written

STREAMS = ("payments", "refunds", "disputes", "payouts", "subscriptions", "invoices")
TITLES = {"payments": "Payments", "refunds": "Refunds", "disputes": "Disputes", "payouts": "Payouts",
          "subscriptions": "Subscriptions", "invoices": "Invoices"}
AGGREGATE_KIND = "ONTOLOGYEX_OPERATIONS_AGGREGATE"
MIN_CELL = 3
WINDOW_WEEKS = 4
MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ROWS = 5_000_000
MAX_WEEKS = 520
MAX_LABELS = 30  # distinct reasons or plans kept per stream; the rest count as "other"
MAX_PAGES = 1000

# Columns a stream may read, by canonical name, and the header names (normalized) that supply each.
COLUMNS = {
    "amount": ("amount", "gross", "total", "amount_gross"),
    "currency": ("currency", "currency_code", "presentment_currency"),
    "created": ("created", "created_date", "created_at", "date", "created_on", "transaction_date", "date_time"),
    "status": ("status", "state", "dispute_status", "payout_status"),
    "fee": ("fee", "fees", "processing_fee"),
    "amount_refunded": ("amount_refunded", "refunded_amount"),
    "decline_reason": ("decline_reason", "failure_code", "failure_reason", "outcome_reason", "decline_code"),
    "reason": ("reason", "dispute_reason", "refund_reason"),
    "arrival": ("arrival_date", "arrival", "expected_arrival_date"),
    "interval": ("interval", "plan_interval", "billing_interval", "price_interval", "recurring_interval"),
    "interval_count": ("interval_count", "plan_interval_count", "price_interval_count"),
    "quantity": ("quantity", "seats"),
    "plan": ("plan", "plan_name", "product", "product_name", "price_nickname", "plan_nickname"),
    "start": ("start", "start_date", "started_at", "start_utc"),
    "canceled": ("canceled_at", "cancelled_at", "canceled_date", "ended_at", "cancellation_date"),
    "amount_due": ("amount_due", "total_due"),
    "amount_paid": ("amount_paid", "paid_amount"),
    "captured": ("captured",),
    "evidence_due": ("evidence_due_by", "due_by", "evidence_due"),
}
# What a stream needs, and the columns that point at it when the file name does not.
NEEDS = {
    "payments": ({"amount", "status", "created"}, {"captured", "amount_refunded", "decline_reason", "fee"}),
    "refunds": ({"amount", "created"}, set()),
    "disputes": ({"amount", "created", "status"}, {"evidence_due"}),
    "payouts": ({"amount", "created", "status"}, {"arrival"}),
    "subscriptions": ({"status"}, {"interval", "canceled"}),
    "invoices": ({"created", "status"}, {"amount_due", "amount_paid"}),
}
NAME_HINTS = (("refund", "refunds"), ("dispute", "disputes"), ("chargeback", "disputes"), ("payout", "payouts"),
              ("subscription", "subscriptions"), ("invoice", "invoices"), ("payment", "payments"),
              ("charge", "payments"), ("transaction", "payments"))
API_OBJECTS = {"charge": "payments", "payment_intent": "payments", "refund": "refunds", "dispute": "disputes",
               "payout": "payouts", "subscription": "subscriptions", "invoice": "invoices"}
SUCCEEDED = {"succeeded", "paid", "captured", "complete", "completed", "refunded", "partially_refunded",
             "partially refunded", "settled", "success", "successful"}
FAILED = {"failed", "declined", "denied", "blocked", "error", "canceled", "cancelled", "voided", "reversed"}
OPEN_DISPUTE = {"needs_response", "warning_needs_response", "under_review", "warning_under_review", "open",
                "needs response", "under review"}
LIVE_SUBSCRIPTION = {"active", "trialing", "past_due", "unpaid"}
# Currencies whose minor unit is not a hundredth (for amounts given in minor units).
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
THREE_DECIMAL = {"bhd", "jod", "kwd", "omr", "tnd"}
MONTHS_PER = {"day": Decimal("30.4375"), "week": Decimal("4.348214"), "month": Decimal(1), "year": Decimal(1) / 12}
# Words in the business model and the code that say a stream is handled there.
STREAM_WORDS = {"payments": r"pay|charge|checkout|purchase|order", "refunds": r"refund",
                "disputes": r"dispute|chargeback", "payouts": r"payout|transfer|settle",
                "subscriptions": r"subscri|renew|plan\b|billing", "invoices": r"invoice|billing|dunning"}
SECRET_KEY = re.compile(r"^sk_(live|test)_")


class OperationsError(ValueError):
    pass


# ---------------------------------------------------------------- reading values

def norm(name: str) -> str:
    """A header as a canonical key: 'Created date (UTC)' -> 'created_date'."""
    name = re.sub(r"\((utc|gmt)\)", "", name.strip().lower())
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")


def amount(text, minor: bool = False, currency: str = "") -> Decimal | None:
    """A money amount from '1,234.56', '$12.00', '-5', '12,50' or an integer in minor units."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = Decimal(str(text))
    else:
        t = re.sub(r"[^0-9,.\-]", "", str(text))
        if not re.search(r"\d", t):
            return None
        if "," in t and "." in t:
            t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
        elif "," in t:
            t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) else t.replace(",", "")
        try:
            value = Decimal(t)
        except InvalidOperation:
            return None
    if minor:
        c = currency.lower()
        value = value if c in ZERO_DECIMAL else value / (1000 if c in THREE_DECIMAL else 100)
    return value


def moment(text, date_format: str | None = None) -> datetime | None:
    """A UTC time from ISO-like text, 'YYYY-MM-DD HH:MM[:SS]', a date, or unix seconds."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return datetime.fromtimestamp(int(text), timezone.utc) if 0 < text < 10**11 else None
    t = str(text).strip()
    if re.fullmatch(r"\d{9,10}", t):
        return datetime.fromtimestamp(int(t), timezone.utc)
    if date_format:
        try:
            parsed = datetime.strptime(t, date_format)
        except ValueError:
            return None
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    t = t.replace("Z", "+00:00")
    t = re.sub(r"\s+(UTC|GMT)$", "", t)
    t = re.sub(r"(?<=\d)\s*([+-]\d{2}):?(\d{2})$", r"\1:\2", t) if re.search(r"[T ]\d{2}:\d{2}", t) else t  # "+0000"
    try:
        parsed = datetime.fromisoformat(t.replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2} ", t) else t)
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def week_of(when: datetime) -> str:
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(week: str) -> datetime:
    return datetime.fromisocalendar(int(week[:4]), int(week[6:]), 1).replace(tzinfo=timezone.utc)


def weeks_between(first: str, last: str) -> list[str]:
    out, at = [], week_start(first)
    end = week_start(last)
    while at <= end and len(out) < MAX_WEEKS:
        out.append(week_of(at))
        at += timedelta(days=7)
    return out


def label(text, limit: int = 60) -> str:
    """A reason or plan as a short label; anything that looks like an identifier or an email is not one."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()[:limit]
    if not t or "@" in t or re.search(r"\d{6,}|\b(?:ch|cus|pi|py|re|dp|du|po|sub|si|in|ii|txn|card|pm|src|acct|ba|"
                                     r"price|prod|evt|tok|seti)_(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}", t):
        return "other"
    return t.lower()


# ---------------------------------------------------------------- the ledger

def cell(stream: str) -> dict:
    """An empty week of one stream (a fresh dict every call)."""
    zero = Decimal(0)
    extra = {
        "payments": {"attempted": 0, "succeeded": 0, "failed": 0, "pending": 0, "gross": zero,
                     "refunded": zero, "fees": None, "decline_reasons": {}},
        "refunds": {"count": 0, "amount": zero, "failed": 0, "reasons": {}},
        "disputes": {"count": 0, "amount": zero, "open": 0, "won": 0, "lost": 0, "reasons": {}},
        "payouts": {"count": 0, "amount": zero, "failed": 0, "failed_amount": zero},
        "subscriptions": {"new": 0, "canceled": 0, "active": 0, "mrr": zero, "plans": {}},
        "invoices": {"count": 0, "due": zero, "paid": zero, "open": 0, "open_amount": zero,
                     "uncollectible": 0, "uncollectible_amount": zero},
    }[stream]
    return {"records": 0, **extra}


def bump(counter: dict, key: str) -> None:
    key = key if key in counter or len(counter) < MAX_LABELS else "other"
    counter[key] = counter.get(key, 0) + 1


class Ledger:
    """Weekly aggregates per stream and currency, built one record at a time; no record is kept."""

    def __init__(self):
        self.cells: dict[tuple[str, str, str], dict] = {}
        self.snapshot: dict[str, dict] = {}  # subscriptions by status as exported, per currency
        self.spans: list[tuple] = []  # (currency, first week, last week or None while live, monthly, plan)
        self.payout_months: dict[tuple[str, str], Decimal] = {}  # (currency, month landed) -> paid out

    def at(self, stream: str, currency: str, week: str) -> dict:
        key = (stream, currency, week)
        if key not in self.cells:
            self.cells[key] = cell(stream)
        return self.cells[key]

    def add(self, stream: str, r: dict) -> bool:
        """Add one record's whitelisted fields. False when it cannot be placed (no date or currency)."""
        currency = (r.get("currency") or "").strip().lower()[:3]
        if not re.fullmatch(r"[a-z]{3}", currency):
            return False
        if stream == "subscriptions":
            return self._subscription(currency, r)
        when = r.get("created")
        if when is None:
            return False
        c = self.at(stream, currency, week_of(when))
        c["records"] += 1
        status = (r.get("status") or "").strip().lower()
        value = r.get("amount") or Decimal(0)
        if stream == "payments":
            c["attempted"] += 1
            if status in SUCCEEDED or (not status and r.get("captured")):
                c["succeeded"] += 1
                c["gross"] += value
                c["refunded"] += r.get("amount_refunded") or Decimal(0)
            elif status in FAILED:
                c["failed"] += 1
                if r.get("decline_reason"):
                    bump(c["decline_reasons"], label(r["decline_reason"]))
            else:
                c["pending"] += 1
            if r.get("fee") is not None:
                c["fees"] = (c["fees"] or Decimal(0)) + r["fee"]
        elif stream == "refunds":
            if status in FAILED:
                c["failed"] += 1
            else:
                c["count"] += 1
                c["amount"] += value
            bump(c["reasons"], label(r.get("reason") or "unspecified"))
        elif stream == "disputes":
            c["count"] += 1
            c["amount"] += value
            bump(c["reasons"], label(r.get("reason") or "unspecified"))
            c["open"] += status in OPEN_DISPUTE
            c["won"] += status == "won"
            c["lost"] += status == "lost"
        elif stream == "payouts":
            c["count"] += 1
            c["amount"] += value
            if status in FAILED:
                c["failed"] += 1
                c["failed_amount"] += value
            else:
                landed = (r.get("arrival") or when).strftime("%Y-%m")
                self.payout_months[(currency, landed)] = self.payout_months.get((currency, landed), Decimal(0)) + value
        elif stream == "invoices":
            c["count"] += 1
            due = r.get("amount_due") if r.get("amount_due") is not None else value
            c["due"] += due or Decimal(0)
            c["paid"] += r.get("amount_paid") or (due if status == "paid" else Decimal(0)) or Decimal(0)
            remaining = (due or Decimal(0)) - (r.get("amount_paid") or Decimal(0))
            if status == "open":
                c["open"] += 1
                c["open_amount"] += remaining
            elif status == "uncollectible":
                c["uncollectible"] += 1
                c["uncollectible_amount"] += remaining
        return True

    def _subscription(self, currency: str, r: dict) -> bool:
        status = (r.get("status") or "").strip().lower()
        snap = self.snapshot.setdefault(currency, {})
        bump(snap, status or "unknown")
        start = r.get("start") or r.get("created")
        if start is None:
            return True  # counted in the snapshot; no history without a start
        interval = (r.get("interval") or "month").strip().lower().rstrip("s").replace("monthly", "month") \
            .replace("yearly", "year").replace("annual", "year").replace("weekly", "week").replace("daily", "day")
        count = int(r.get("interval_count") or 1) or 1
        monthly = (r.get("amount") or Decimal(0)) * (r.get("quantity") or 1) * MONTHS_PER.get(interval, Decimal(1)) / count
        end = r.get("canceled")
        if end is None and status not in LIVE_SUBSCRIPTION and status:
            end = start  # ended at an unknown time: counted as started, never as active
        first = week_of(start)
        new = self.at("subscriptions", currency, first)
        new["new"] += 1
        new["records"] += 1
        if end is not None:
            gone = self.at("subscriptions", currency, week_of(end))
            gone["canceled"] += 1
            gone["records"] += 1
        if end is not None and end <= start:
            return True  # never active for a whole moment
        last = week_of(end - timedelta(seconds=1)) if end is not None else None
        self.spans.append((currency, first, last, monthly, label(r.get("plan") or "unspecified")))
        return True

    def close(self) -> None:
        """Fill in each week's active subscriptions and MRR from their start and end weeks; a live
        subscription counts up to the last week the records reach."""
        weeks = sorted({w for (_, _, w) in self.cells})
        latest = weeks[-1] if weeks else None
        for currency, first, last, monthly, plan in self.spans:
            for w in weeks_between(first, last or latest or first):
                c = self.at("subscriptions", currency, w)
                c["active"] += 1
                c["mrr"] += monthly
                bump(c["plans"], plan)
        self.spans = []


# ---------------------------------------------------------------- files

def mapping_for(folder: Path, rel: str) -> dict | None:
    path = folder / "operations.json"
    if not path.is_file():
        return None
    try:
        config = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise OperationsError(f"operations.json: {exc}") from exc
    for entry in config.get("files", []) if type(config) is dict else []:
        if type(entry) is dict and fnmatch.fnmatch(rel.lower(), str(entry.get("match", "")).lower()):
            if entry.get("stream") not in STREAMS + spend.STREAMS + customers.STREAMS or type(entry.get("columns")) is not dict:
                raise OperationsError(f"operations.json: {entry.get('match')!r} needs a stream and columns")
            return entry
    return None


def recognize(rel: str, headers: list[str], mapping: dict | None) -> tuple[str | None, dict, str]:
    """(stream, canonical column -> header, how it was recognized) for one CSV file."""
    if mapping is not None:
        wanted = {k: v for k, v in mapping["columns"].items()
                  if (k in COLUMNS or k in spend.COLUMNS or k in customers.COLUMNS) and v in headers}
        return mapping["stream"], wanted, "operations.json"
    by_norm = {norm(h): h for h in headers}
    found = {}
    for canonical, names in COLUMNS.items():
        for name in names:
            if name in by_norm:
                found[canonical] = by_norm[name]
                break
    lower = rel.lower()
    hinted = next((stream for hint, stream in NAME_HINTS if hint in lower), None)
    if hinted and NEEDS[hinted][0] <= set(found):
        return hinted, found, "file name and columns"
    for stream in ("invoices", "subscriptions", "payouts", "disputes", "payments"):
        need, marks = NEEDS[stream]
        if need <= set(found) and marks & set(found):
            return stream, found, "columns"
    return None, found, ""


def read_csv(path: Path, rel: str, folder: Path, ledger: Ledger, book: "spend.Book | None" = None,
             desk: "customers.Desk | None" = None) -> dict:
    mapping = mapping_for(folder, rel)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(64 * 1024)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(handle, dialect)
        headers = next(reader, [])
        stream, columns, how = recognize(rel, headers, mapping)
        if desk is not None and (stream is None or stream in customers.STREAMS):
            if mapping is None:
                found = customers.find_columns(headers, norm)
                found_stream, found_how = customers.recognize(rel, found)
                if found_stream is not None:
                    stream, columns, how = found_stream, found, found_how
            if stream in customers.STREAMS:
                return read_customers(reader, headers, rel, stream, columns, how, mapping, desk)
        if book is not None and (stream is None or stream in spend.STREAMS):
            if mapping is None:
                columns = spend.find_columns(headers, norm)
                stream, how = spend.recognize(rel, columns)
            if stream is not None:
                return read_spend(reader, headers, rel, stream, columns, how, mapping, book)
        if stream is None:
            return {"file": rel, "status": "UNRECOGNIZED", "headers": [h[:60] for h in headers[:40]],
                    "hint": "Add an entry for it in operations.json: which stream it is and which column holds "
                            "each of amount, currency, created and status."}
        index = {canonical: headers.index(header) for canonical, header in columns.items()}
        date_format = (mapping or {}).get("date_format")
        status_map = {str(k).lower(): v for k, v in ((mapping or {}).get("status_map") or {}).items()}
        minor = (mapping or {}).get("amounts_in") == "minor"
        default_currency = (mapping or {}).get("currency")
        rows = placed = 0
        for row in reader:
            rows += 1
            if rows > MAX_ROWS:
                raise OperationsError(f"{rel}: more than {MAX_ROWS} rows")
            get = lambda k: row[index[k]] if k in index and index[k] < len(row) else None  # noqa: E731
            currency = get("currency") or default_currency or ""
            status = get("status") or ""
            record = {"currency": currency, "status": status_map.get(status.lower(), status)}
            for k in ("amount", "fee", "amount_refunded", "amount_due", "amount_paid"):
                record[k] = amount(get(k), minor, currency)
            for k in ("created", "start", "canceled", "arrival"):
                record[k] = moment(get(k), date_format)
            for k in ("decline_reason", "reason", "interval", "plan"):
                record[k] = get(k)
            record["captured"] = str(get("captured") or "").lower() in ("true", "yes", "1")
            for k in ("interval_count", "quantity"):
                try:
                    record[k] = int(float(get(k))) if get(k) not in (None, "") else None
                except ValueError:
                    record[k] = None
            placed += ledger.add(stream, record)
    found = {"file": rel, "status": "READ", "stream": stream, "recognized_by": how, "rows": rows,
             "rows_used": placed, "columns_used": sorted(columns), "columns_not_read": len(headers) - len(columns)}
    if placed < rows:
        found["hint"] = (f"{rows - placed} of {rows} rows have no readable date or currency, so they are not counted. "
                         "In operations.json, give this file a date_format (e.g. \"%m/%d/%Y\") or a currency.")
    return found


def read_spend(reader, headers: list[str], rel: str, stream: str, columns: dict, how: str, mapping: dict | None,
               book: "spend.Book") -> dict:
    """Bank and card lines, bills and cloud costs, into monthly aggregates."""
    index = {canonical: headers.index(header) for canonical, header in columns.items() if header in headers}
    date_format = (mapping or {}).get("date_format")
    default_currency = (mapping or {}).get("currency")
    flip = (mapping or {}).get("amounts") == "out_positive"  # card exports list charges as positive
    provider = spend.provider_of(headers) if stream == "cloud_costs" else None
    rows = placed = 0
    for row in reader:
        rows += 1
        if rows > MAX_ROWS:
            raise OperationsError(f"{rel}: more than {MAX_ROWS} rows")
        get = lambda k: row[index[k]] if k in index and index[k] < len(row) else None  # noqa: E731
        currency = get("currency") or default_currency or ""
        when = get("usage_start") if stream == "cloud_costs" and get("usage_start") else get("created")
        if when and re.fullmatch(r"\d{6}", when.strip()):  # an invoice month, YYYYMM
            when = f"{when.strip()[:4]}-{when.strip()[4:]}-01"
        record = {"currency": currency, "created": moment(when, date_format), "due": moment(get("due"), date_format),
                  "status": get("status"), "payee": get("payee"), "description": get("description"),
                  "category": get("category"), "service": get("service"), "provider": provider}
        for k in ("amount", "debit", "credit", "cost"):
            record[k] = amount(get(k), False, currency)
        if flip and record["amount"] is not None:
            record["amount"] = -record["amount"]
        placed += book.add(stream, record, label)
    found = {"file": rel, "status": "READ", "stream": stream, "recognized_by": how, "rows": rows, "rows_used": placed,
             "columns_used": sorted(k for k in columns if k not in ("payee", "description")),
             "columns_matched_only": sorted(k for k in columns if k in ("payee", "description")),
             "columns_not_read": len(headers) - len(columns)}
    if placed < rows:
        found["hint"] = (f"{rows - placed} of {rows} rows have no readable date or currency, so they are not counted. "
                         "In operations.json, give this file a currency (bank exports often have none) or a date_format.")
    return found


def read_customers(reader, headers: list[str], rel: str, stream: str, columns: dict, how: str, mapping: dict | None,
                   desk: "customers.Desk") -> dict:
    """Support tickets and sales deals, into weekly aggregates. A subject is only matched, never kept."""
    index = {canonical: headers.index(header) for canonical, header in columns.items() if header in headers}
    date_format = (mapping or {}).get("date_format")
    default_currency = (mapping or {}).get("currency")
    rows = placed = 0
    for row in reader:
        rows += 1
        if rows > MAX_ROWS:
            raise OperationsError(f"{rel}: more than {MAX_ROWS} rows")
        get = lambda k: row[index[k]] if k in index and index[k] < len(row) else None  # noqa: E731
        record = {k: get(k) for k in ("status", "priority", "topic", "ticket_type", "channel", "satisfaction", "subject",
                                      "stage", "lost_reason", "source")}
        record["currency"] = get("currency") or default_currency
        record["amount"] = amount(get("amount"), False, record["currency"] or "")
        for k in ("created", "solved", "close"):
            record[k] = moment(get(k), date_format)
        placed += desk.add(stream, record, label)
    found = {"file": rel, "status": "READ", "stream": stream, "recognized_by": how, "rows": rows, "rows_used": placed,
             "columns_used": sorted(k for k in columns if k != "subject"),
             "columns_matched_only": ["subject"] if "subject" in columns else [],
             "columns_not_read": len(headers) - len(columns)}
    if placed < rows:
        found["hint"] = (f"{rows - placed} of {rows} rows have no readable date, so they are not counted. In "
                         "operations.json, give this file a date_format.")
    return found


def api_record(obj: dict) -> tuple[str | None, dict]:
    """The whitelisted fields of one API object (Stripe's shapes), amounts converted from minor units."""
    stream = API_OBJECTS.get(obj.get("object"))
    if stream is None:
        return None, {}
    currency = str(obj.get("currency") or "")
    m = lambda v: amount(v, True, currency) if v is not None else None  # noqa: E731
    record = {"currency": currency, "status": obj.get("status") or "", "created": moment(obj.get("created")),
              "amount": m(obj.get("amount"))}
    if stream == "payments":
        record["amount_refunded"] = m(obj.get("amount_refunded"))
        record["captured"] = bool(obj.get("captured"))
        record["decline_reason"] = obj.get("failure_code") or (obj.get("outcome") or {}).get("reason")
        if obj.get("object") == "payment_intent":
            record["amount"] = m(obj.get("amount_received") or obj.get("amount"))
    elif stream in ("refunds", "disputes"):
        record["reason"] = obj.get("reason")
    elif stream == "payouts":
        record["arrival"] = moment(obj.get("arrival_date"))
    elif stream == "invoices":
        record["amount_due"], record["amount_paid"] = m(obj.get("amount_due")), m(obj.get("amount_paid"))
    elif stream == "subscriptions":
        items = ((obj.get("items") or {}).get("data") or [])
        total, interval, count, plan = Decimal(0), "month", 1, None
        for item in items:
            price = item.get("price") or item.get("plan") or {}
            recurring = price.get("recurring") or {"interval": price.get("interval"),
                                                   "interval_count": price.get("interval_count")}
            unit = m(price.get("unit_amount") if "unit_amount" in price else price.get("amount")) or Decimal(0)
            total += unit * (item.get("quantity") or 1)
            interval, count = recurring.get("interval") or interval, recurring.get("interval_count") or count
            plan = plan or price.get("nickname") or price.get("lookup_key")
            currency = currency or price.get("currency") or ""
        record.update({"currency": currency, "amount": total, "interval": interval, "interval_count": count,
                       "quantity": 1, "plan": plan, "start": moment(obj.get("start_date") or obj.get("created")),
                       "canceled": moment(obj.get("canceled_at") or obj.get("ended_at"))})
    return stream, record


def read_json(path: Path, rel: str, ledger: Ledger, aggregates: list) -> dict:
    try:
        document = json.loads(path.read_text("utf-8"))
    except ValueError as exc:
        return {"file": rel, "status": "UNREADABLE", "detail": str(exc)[:200]}
    if type(document) is dict and document.get("kind") == AGGREGATE_KIND:
        aggregates.append((rel, document))
        return {"file": rel, "status": "READ", "stream": ",".join(sorted([*document.get("streams", {}),
                                                                          *document.get("spend", {}),
                                                                          *document.get("customers", {})])),
                "recognized_by": "an aggregate written by operations.py pull", "rows": None,
                "source": document.get("source")}
    objects = document.get("data") if type(document) is dict and document.get("object") == "list" else document
    if type(objects) is not list:
        return {"file": rel, "status": "UNRECOGNIZED", "headers": [],
                "hint": "JSON files are read when they hold API objects (a list object or a list)."}
    counts: dict[str, int] = {}
    placed = 0
    for obj in objects:
        stream, record = api_record(obj) if type(obj) is dict else (None, {})
        if stream:
            counts[stream] = counts.get(stream, 0) + 1
            placed += ledger.add(stream, record)
    if not counts:
        return {"file": rel, "status": "UNRECOGNIZED", "headers": [], "hint": "no payment, refund, dispute, payout, "
                "subscription or invoice objects in it"}
    return {"file": rel, "status": "READ", "stream": ",".join(sorted(counts)), "recognized_by": "API objects",
            "rows": sum(counts.values()), "rows_used": placed}


def files_in(folder: Path) -> list[Path]:
    found = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            path = Path(root) / name
            if name.startswith(".") or name == "operations.json" or path.is_symlink():
                continue
            if path.suffix.lower() in (".csv", ".tsv", ".json") + written.SUFFIXES and path.stat().st_size <= MAX_FILE_BYTES:
                found.append(path)
    return found[:MAX_FILES]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- report

def plain(value):
    if isinstance(value, Decimal):
        return float(round(value, 2))
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def merge_aggregates(ledger: Ledger, aggregates: list, sources: list) -> list[dict]:
    """Aggregates from a pull replace what files say about the same stream, currency and week."""
    overlaps = []
    for rel, document in aggregates:
        for stream, by_currency in (document.get("streams") or {}).items():
            if stream not in STREAMS or type(by_currency) is not dict:
                continue
            for currency, weeks in by_currency.items():
                for week, values in (weeks or {}).items():
                    key = (stream, currency, week)
                    if key in ledger.cells and ledger.cells[key]["records"]:
                        overlaps.append(key)
                    fresh = cell(stream)
                    for k, v in values.items():
                        if k in fresh and (isinstance(fresh[k], Decimal) or k == "fees"):
                            fresh[k] = Decimal(str(v)) if v is not None else None
                        elif k in fresh:
                            fresh[k] = v
                    ledger.cells[key] = fresh
        for stream, by_currency in (document.get("snapshot") or {}).items():
            for currency, counts in by_currency.items():
                ledger.snapshot[currency] = dict(counts)
    found: dict[tuple[str, str], int] = {}
    for stream, currency, _ in overlaps:
        found[(stream, currency)] = found.get((stream, currency), 0) + 1
    return [{"stream": s, "currency": c, "weeks": n, "kept": "the pulled aggregate"} for (s, c), n in sorted(found.items())]


def window(cells: list[dict], key: str) -> Decimal:
    return sum((c.get(key) or Decimal(0) for c in cells), Decimal(0))


def ratio(a, b) -> float | None:
    return float(round(Decimal(a) / Decimal(b), 4)) if b else None


def signals_for(currency: str, series: dict[str, list[tuple[str, dict]]], snapshot: dict, current: str) -> list[dict]:
    """What changed or needs a person: the last four complete weeks against the four before."""
    found = []

    def recent(stream):
        rows = [(w, c) for w, c in series.get(stream, []) if w < current]
        return [c for _, c in rows[-WINDOW_WEEKS:]], [c for _, c in rows[-2 * WINDOW_WEEKS:-WINDOW_WEEKS]]

    pay_now, pay_before = recent("payments")
    gross_now, gross_before = window(pay_now, "gross"), window(pay_before, "gross")
    ref_now, ref_before = recent("refunds")
    refunded_now = window(ref_now, "amount") or window(pay_now, "refunded")
    refunded_before = window(ref_before, "amount") or window(pay_before, "refunded")
    rate_now, rate_before = ratio(refunded_now, gross_now), ratio(refunded_before, gross_before)
    if rate_now is not None and rate_before is not None and rate_now >= 0.02 and rate_now >= 1.5 * rate_before:
        found.append({"signal": "REFUND_RATE_RISING", "level": "HIGH", "currency": currency,
                      "detail": f"refunds were {rate_now:.1%} of payments in the last {WINDOW_WEEKS} weeks, "
                                f"{rate_before:.1%} in the {WINDOW_WEEKS} before", "stream": "refunds"})
    if gross_before and gross_now < Decimal("0.8") * gross_before:
        found.append({"signal": "REVENUE_DOWN", "level": "HIGH", "currency": currency,
                      "detail": f"payments taken fell {1 - float(gross_now / gross_before):.0%} against the "
                                f"{WINDOW_WEEKS} weeks before", "stream": "payments"})
    attempted, failed = window(pay_now, "attempted"), window(pay_now, "failed")
    if attempted >= 20 and failed / attempted >= Decimal("0.15"):
        found.append({"signal": "FAILED_PAYMENTS_HIGH", "level": "MEDIUM", "currency": currency,
                      "detail": f"{int(failed)} of {int(attempted)} payment attempts failed "
                                f"({failed / attempted:.0%}) in the last {WINDOW_WEEKS} weeks", "stream": "payments"})
    dis_now, _ = recent("disputes")
    disputes, succeeded = window(dis_now, "count"), window(pay_now, "succeeded")
    if disputes and succeeded and disputes / succeeded >= Decimal("0.0075"):
        found.append({"signal": "DISPUTE_RATE_HIGH", "level": "HIGH", "currency": currency,
                      "detail": f"{int(disputes)} disputes against {int(succeeded)} payments "
                                f"({disputes / succeeded:.2%}) in the last {WINDOW_WEEKS} weeks", "stream": "disputes"})
    open_disputes = sum(c["open"] for _, c in series.get("disputes", []))
    if open_disputes:
        found.append({"signal": "DISPUTES_OPEN", "level": "CRITICAL", "currency": currency,
                      "detail": f"{open_disputes} dispute(s) were open or awaiting a response when exported; "
                                "an unanswered dispute is lost", "stream": "disputes"})
    out_now, _ = recent("payouts")
    failed_payouts = window(out_now, "failed")
    if failed_payouts:
        found.append({"signal": "PAYOUT_FAILED", "level": "CRITICAL", "currency": currency,
                      "detail": f"{int(failed_payouts)} payout(s) failed in the last {WINDOW_WEEKS} weeks: money "
                                "did not reach the bank", "stream": "payouts"})
    sub_now, sub_before = recent("subscriptions")
    churn_now, churn_before = window(sub_now, "canceled"), window(sub_before, "canceled")
    if churn_now >= 3 and churn_now >= Decimal("1.5") * max(churn_before, 1):
        found.append({"signal": "CHURN_RISING", "level": "HIGH", "currency": currency,
                      "detail": f"{int(churn_now)} subscriptions ended in the last {WINDOW_WEEKS} weeks, "
                                f"{int(churn_before)} in the {WINDOW_WEEKS} before", "stream": "subscriptions"})
    inv = series.get("invoices", [])
    unpaid = sum(c["uncollectible"] for _, c in inv)
    if unpaid:
        found.append({"signal": "INVOICES_UNCOLLECTIBLE", "level": "MEDIUM", "currency": currency,
                      "detail": f"{unpaid} invoice(s) marked uncollectible", "stream": "invoices"})
    if (snapshot or {}).get("past_due"):
        found.append({"signal": "SUBSCRIPTIONS_PAST_DUE", "level": "MEDIUM", "currency": currency,
                      "detail": f"{snapshot['past_due']} subscription(s) past due when exported", "stream": "subscriptions"})
    return found


def recent_figures(series: dict[str, list[tuple[str, dict]]], current: str, min_cell: int) -> dict:
    """The headline figures: the last four complete weeks, and the same for the four before."""
    def windows(stream):
        rows = [c for w, c in series.get(stream, []) if w < current]
        return rows[-WINDOW_WEEKS:], rows[-2 * WINDOW_WEEKS:-WINDOW_WEEKS]

    def money(rows, key, records_key="records"):
        if sum(c[records_key] for c in rows) < min_cell:
            return None
        return float(round(window(rows, key), 2))

    out = {}
    pay, pay_before = windows("payments")
    if pay:
        refunds, refunds_before = windows("refunds")
        refunded = lambda r, p: window(r, "amount") if r else window(p, "refunded")  # noqa: E731
        out["payments"] = {
            "taken": money(pay, "gross"), "taken_before": money(pay_before, "gross") if pay_before else None,
            "succeeded": int(window(pay, "succeeded")), "attempted": int(window(pay, "attempted")),
            "failure_rate": ratio(window(pay, "failed"), window(pay, "attempted")),
            "refund_rate": ratio(refunded(refunds, pay), window(pay, "gross")),
            "refund_rate_before": ratio(refunded(refunds_before, pay_before), window(pay_before, "gross"))
            if pay_before else None}
    dis, _ = windows("disputes")
    if series.get("disputes"):
        out["disputes"] = {"count": int(window(dis, "count")), "open": sum(c["open"] for _, c in series["disputes"]),
                           "rate": ratio(window(dis, "count"), window(pay, "succeeded")) if pay else None}
    subs = [c for w, c in series.get("subscriptions", []) if w < current]
    if subs:
        last = subs[-1]
        sub_now, sub_before = windows("subscriptions")
        out["subscriptions"] = {"active": last["active"],
                                "mrr": float(round(last["mrr"], 2)) if max(last["active"], 1) >= min_cell else None,
                                "ended": int(window(sub_now, "canceled")), "ended_before": int(window(sub_before, "canceled")),
                                "started": int(window(sub_now, "new"))}
    out_now, _ = windows("payouts")
    if series.get("payouts"):
        out["payouts"] = {"count": int(window(out_now, "count")), "failed": int(window(out_now, "failed"))}
    inv = series.get("invoices", [])
    if inv:
        out["invoices"] = {"open": sum(c["open"] for _, c in inv), "uncollectible": sum(c["uncollectible"] for _, c in inv)}
    return out


def shown(stream: str, c: dict, min_cell: int) -> dict:
    """A week as shown: counts always; amounts and breakdowns only over at least min_cell records."""
    out = plain(c)
    population = max(c["records"], c.get("active", 0))
    if population < min_cell:
        for k, v in c.items():
            if isinstance(v, (Decimal, dict)) or (k == "fees" and v is not None):
                out[k] = None
        out["withheld"] = True
    return out


def report(folder: Path | None, entities: list[dict] | None = None, min_cell: int = MIN_CELL,
           now: datetime | None = None) -> dict:
    """The operations ledger for a folder of exports and pulled aggregates. `now` names the week in
    progress, which signals leave out."""
    current = week_of(now or datetime.now(timezone.utc))
    base = {"schema_version": 1, "kind": "OPERATIONS_LEDGER", "execution_authorized": False,
            "privacy": {"rows_kept": False, "identifiers_read": False, "min_cell": min_cell,
                        "rule": f"Only aggregates are kept. Amounts and breakdowns of a week with fewer than "
                                f"{min_cell} records are withheld."}}
    if folder is None or not folder.is_dir():
        return {**base, "status": "NO_OPERATIONS_FOLDER", "folder": str(folder) if folder else None, "sources": [],
                "currencies": {}, "signals": [], "code_vs_operations": [], "unrecognized": []}
    ledger, aggregates, sources, documents, commitments = Ledger(), [], [], [], []
    book = spend.Book(now or datetime.now(timezone.utc))
    desk = customers.Desk(now or datetime.now(timezone.utc), week_of, customers.vocabulary(entities or []))
    for path in files_in(folder):
        rel = path.relative_to(folder).as_posix()
        try:
            if path.suffix.lower() in written.SUFFIXES:  # policies, procedures, terms, contracts
                source, found = written.read(path, rel)
                documents.append(source)
                commitments += found
            else:
                source = (read_json(path, rel, ledger, aggregates) if path.suffix.lower() == ".json"
                          else read_csv(path, rel, folder, ledger, book, desk))
        except (OSError, UnicodeError, csv.Error) as exc:
            source = {"file": rel, "status": "UNREADABLE", "detail": str(exc)[:200]}
        sources.append({**source, "sha256": sha256(path), "bytes": path.stat().st_size})
    ledger.close()
    overlaps = merge_aggregates(ledger, aggregates, sources)
    for _, document in aggregates:  # pulled spend (cloud costs by service) replaces files for the same month
        for stream, by_currency in (document.get("spend") or {}).items():
            if stream not in spend.STREAMS or type(by_currency) is not dict:
                continue
            for currency, months in by_currency.items():
                for month, values in (months or {}).items():
                    fresh = spend.cell(stream)
                    for k, v in values.items():
                        if isinstance(fresh.get(k), Decimal):
                            fresh[k] = Decimal(str(v))
                        elif isinstance(fresh.get(k), dict):
                            fresh[k] = {name: Decimal(str(x)) for name, x in v.items()}
                        elif k in fresh:
                            fresh[k] = v
                    book.cells[(stream, currency, month)] = fresh
    currencies: dict[str, dict] = {}
    signals = []
    for currency in sorted({c for (_, c, _) in ledger.cells} | set(ledger.snapshot)):
        series = {s: sorted(((w, c) for (st, cur, w), c in ledger.cells.items() if st == s and cur == currency))
                  for s in STREAMS}
        weeks = sorted({w for rows in series.values() for w, _ in rows})
        if weeks:  # empty weeks between the first and the last are real zeros
            span = weeks_between(weeks[0], weeks[-1])
            series = {s: [(w, dict(rows).get(w) or cell(s)) for w in span] if rows else []
                      for s, rows in ((s, series[s]) for s in STREAMS)}
        totals = {}
        for s, rows in series.items():
            if not rows:
                continue
            total = cell(s)
            for _, c in rows:
                for k, v in c.items():
                    if k == "fees":
                        if v is not None:
                            total["fees"] = (total["fees"] or Decimal(0)) + v
                    elif isinstance(v, (int, Decimal)) and not isinstance(v, bool) and k not in ("active", "mrr"):
                        total[k] += v
                    elif isinstance(v, dict):
                        for label_, n in v.items():
                            total[k][label_] = total[k].get(label_, 0) + n
            if s == "subscriptions":  # where the last week ended, not a sum of weeks
                last = rows[-1][1]
                total["active"], total["mrr"], total["plans"] = last["active"], last["mrr"], dict(last["plans"])
            totals[s] = shown(s, total, min_cell)
        snapshot = ledger.snapshot.get(currency, {})
        found = signals_for(currency, series, snapshot, current)
        signals += found
        currencies[currency] = {
            "first_week": weeks[0] if weeks else None, "last_week": weeks[-1] if weeks else None,
            "streams": {s: [{"week": w, **shown(s, c, min_cell)} for w, c in rows] for s, rows in series.items() if rows},
            "totals": totals, "subscriptions_now": snapshot or None,
            "recent": recent_figures(series, current, min_cell)}
    payouts_by_month: dict[str, dict] = {}
    for (currency, month), value in ledger.payout_months.items():
        payouts_by_month.setdefault(currency, {})[month] = value
    for _, document in aggregates:
        for currency, months in (document.get("payouts_by_month") or {}).items():
            payouts_by_month[currency] = {m: Decimal(str(v)) for m, v in months.items()}
    payments_by_month: dict[str, dict] = {}
    for (stream, currency, week), c in ledger.cells.items():
        if stream == "payments":  # a week counts in the month of its Thursday, as ISO weeks do
            month = (week_start(week) + timedelta(days=3)).strftime("%Y-%m")
            bucket = payments_by_month.setdefault(currency, {})
            bucket[month] = bucket.get(month, Decimal(0)) + c["gross"]
    spending = spend.section(book, min_cell, payouts_by_month, payments_by_month) if book.cells else None
    if spending:
        signals += spending["signals"]
    pulled = set()
    for _, document in aggregates:  # pulled tickets or deals replace what files say about them
        for stream, weeks in (document.get("customers") or {}).items():
            if stream not in customers.STREAMS or type(weeks) is not dict:
                continue
            pulled.add(stream)
            for week, values in weeks.items():
                fresh = customers.cell(stream)
                for k, v in values.items():
                    if k in ("won_amount", "lost_amount"):
                        fresh[k] = {cur: Decimal(str(x)) for cur, x in v.items()}
                    elif k in fresh:
                        fresh[k] = v
                desk.cells[(stream, week)] = fresh
    desk.spans = [span for span in desk.spans if span[0] not in pulled]
    customer = customers.section(desk, current, min_cell, weeks_between)
    if customer:
        signals += customer["signals"]
    read = [s for s in sources if s["status"] == "READ"]
    writing = None
    if documents:
        present = {s for cur in currencies.values() for s in cur["streams"]}
        writing = written.check(commitments, documents, {"currencies": currencies, "spend": spending,
                                                         "customers": customer, "streams_present": sorted(present)},
                                entities or [])
        signals += writing["signals"]
    present = {s for cur in currencies.values() for s in cur["streams"]} | (
        {"subscriptions"} if any(c["subscriptions_now"] for c in currencies.values()) else set())
    return {**base, "status": "OK" if read else "NO_RECORDS_READ", "folder": str(folder), "week_in_progress": current,
            "month_in_progress": spend.month_of(now or datetime.now(timezone.utc)),
            "sources": sources, "unrecognized": [s for s in sources if s["status"] == "UNRECOGNIZED"],
            "overlaps": overlaps, "streams_present": sorted(present), "currencies": currencies, "spend": spending,
            "customers": customer, "written": writing,
            "signals": sorted(signals, key=lambda s: ({"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}[s["level"]], s["signal"])),
            "code_vs_operations": compare(present, entities or [], currencies)}


def folder_revision(folder: Path | None) -> str:
    """A cheap fingerprint of the folder: each file's path, size and modification time."""
    if folder is None or not folder.is_dir():
        return ""
    h = hashlib.sha256()
    for path in files_in(folder) + ([folder / "operations.json"] if (folder / "operations.json").is_file() else []):
        st = path.stat()
        h.update(f"{path.relative_to(folder).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def with_comparison(answer: dict, entities: list[dict], code_suppliers: list[dict] | None = None) -> dict:
    """The ledger compared with the model, the code and the supplier map as they are now."""
    if answer["status"] != "OK":
        return answer
    out = {**answer, "code_vs_operations": compare(set(answer["streams_present"]), entities, answer["currencies"])}
    if answer.get("spend") and code_suppliers is not None:
        out["spend"] = {**answer["spend"], "suppliers_vs_spend": spend.compare(answer["spend"], code_suppliers)}
    if answer.get("customers"):
        out["customers"] = {**answer["customers"],
                            "joined": customers.joined(answer["customers"], answer["currencies"], entities)}
    return out


def compare(present: set, entities: list[dict], currencies: dict) -> list[dict]:
    """What the records show the business does that the model and code do not mention, and the reverse."""
    if not entities:
        return []
    out = []
    span = max((len(c["streams"].get("payments", [])) for c in currencies.values()), default=0)
    for stream in STREAMS:
        pattern = re.compile(STREAM_WORDS[stream], re.I)
        named = [e for e in entities if pattern.search(e.get("label") or "")]  # IDs carry skill names
        business = [e for e in named if e.get("layer") == "business"]
        if stream in present and not named:
            out.append({"finding": "HAPPENS_NOT_IN_CODE", "stream": stream,
                        "detail": f"the records show {TITLES[stream].lower()}, and nothing in the business model or "
                                  "the code is named for them: they are handled somewhere the sources do not show "
                                  "(by hand, in a supplier's dashboard, or in code outside this repository)"})
        elif stream not in present and business and span >= 8:
            out.append({"finding": "IN_CODE_NOT_SEEN", "stream": stream,
                        "detail": f"{len(business)} business item(s) are named for {TITLES[stream].lower()}, and "
                                  f"{span} weeks of records show none: not used, handled by another processor, "
                                  "or missing from the exports", "entities": [{"id": e["id"], "label": e.get("label")}
                                                                             for e in business[:5]]})
        elif stream in present and business:
            out.append({"finding": "SEEN_AND_MODELED", "stream": stream,
                        "detail": f"the records show {TITLES[stream].lower()}, and the model names "
                                  + ", ".join(sorted({e.get('label') or e['id'] for e in business})[:4]),
                        "standing": "INFERRED", "entities": [{"id": e["id"], "label": e.get("label")}
                                                             for e in business[:5]]})
    return out


def markdown(answer: dict) -> str:
    lines = ["# Operations", ""]
    if answer["status"] != "OK":
        return "\n".join(lines + [f"No records read ({answer['status']}). Drop exports in {answer.get('folder')}.", ""])
    lines.append(answer["privacy"]["rule"])
    lines.append("")
    for currency, c in answer["currencies"].items():
        lines += [f"## {currency.upper()} ({c['first_week']} to {c['last_week']})", "", "| Stream | Records | Totals |",
                  "|---|---|---|"]
        for stream, total in c["totals"].items():
            parts = [f"{k} {v:,}" if isinstance(v, (int, float)) else f"{k} withheld"
                     for k, v in total.items() if k not in ("records", "withheld") and not isinstance(v, dict)]
            lines.append(f"| {TITLES[stream]} | {total['records']:,} | {', '.join(parts)} |")
        lines.append("")
    if answer["signals"]:
        lines += ["## Signals", ""] + [f"- **{s['signal']}**" + (f" ({s['currency'].upper()})" if s.get("currency") else "")
                                       + f": {s['detail']}" for s in answer["signals"]] + [""]
    if answer["code_vs_operations"]:
        lines += ["## Code and operations", ""] + [f"- **{f['finding']}** ({f['stream']}): {f['detail']}"
                                                   for f in answer["code_vs_operations"]] + [""]
    lines += ["## Sources", ""] + [f"- `{s['file']}`: {s['status'].lower()}" + (
        f", {s.get('stream')}, {s['rows']:,} rows" if s.get("rows") is not None else "") + f", sha256 {s['sha256'][:12]}"
        for s in answer["sources"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- pull (read-only API)

STRIPE_STREAMS = (("payments", "/v1/charges", {}), ("refunds", "/v1/refunds", {}),
                  ("disputes", "/v1/disputes", {}), ("payouts", "/v1/payouts", {}),
                  ("subscriptions", "/v1/subscriptions", {"status": "all"}), ("invoices", "/v1/invoices", {}))


def pull_stripe(key: str, out: Path, days: int, base: str = "https://api.stripe.com", allow_secret: bool = False,
                now: datetime | None = None) -> dict:
    """Read charges, refunds, disputes, payouts, subscriptions and invoices with a read-only key, aggregating
    each page as it arrives. Writes one aggregate file; no record is written anywhere."""
    if SECRET_KEY.match(key) and not allow_secret:
        raise OperationsError("that is a secret key, which can change the account; create a restricted key with "
                              "read access to charges, refunds, disputes, payouts, subscriptions and invoices (rk_...)")
    now = now or datetime.now(timezone.utc)
    since = int((now - timedelta(days=days)).timestamp())
    ledger, counts = Ledger(), {}
    for stream, path, extra in STRIPE_STREAMS:
        params = {"limit": "100", **extra}
        if stream != "subscriptions":  # live subscriptions started before the window still count
            params["created[gte]"] = str(since)
        after, pages = None, 0
        while pages < MAX_PAGES:
            query = urllib.parse.urlencode({**params, **({"starting_after": after} if after else {})})
            request = urllib.request.Request(f"{base}{path}?{query}", headers={
                "Authorization": f"Bearer {key}", "Stripe-Version": "2024-06-20", "User-Agent": "ontologyex-operations"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    page = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise OperationsError(f"{path}: the key may not read this ({exc.code}); give the restricted key "
                                          f"read access to it, or leave it out") from None
                raise OperationsError(f"{path}: HTTP {exc.code}") from None
            pages += 1
            for obj in page.get("data") or []:
                s, record = api_record(obj)
                if s:
                    counts[s] = counts.get(s, 0) + 1
                    ledger.add(s, record)
            if not page.get("has_more") or not page.get("data"):
                break
            after = page["data"][-1].get("id")
    ledger.close()
    streams: dict = {}
    for (stream, currency, week), c in sorted(ledger.cells.items()):
        streams.setdefault(stream, {}).setdefault(currency, {})[week] = plain(c)
    document = {"schema_version": 1, "kind": AGGREGATE_KIND, "source": "stripe_api",
                "pulled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": days, "objects_read": counts,
                "streams": streams, "snapshot": {"subscriptions": ledger.snapshot},
                "payouts_by_month": {c: {m: float(v) for (cur, m), v in sorted(ledger.payout_months.items()) if cur == c}
                                     for c in sorted({c for c, _ in ledger.payout_months})},
                "semantics": "AGGREGATES_ONLY_NO_RECORDS"}
    out.mkdir(parents=True, exist_ok=True)
    target = out / "stripe-api.ontologyex-aggregate.json"
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    return {"status": "PULLED", "file": str(target), "objects_read": counts}


def _get_json(url: str, headers: dict, what: str) -> dict:
    request = urllib.request.Request(url, headers={**headers, "Accept": "application/json",
                                                   "User-Agent": "ontologyex-operations"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise OperationsError(f"{what}: the token may not read this ({exc.code}); give it read access") from None
        raise OperationsError(f"{what}: HTTP {exc.code}") from None


def _write_customers(out: Path, name: str, source: str, desk: "customers.Desk", now: datetime, counts: dict) -> dict:
    weeks = sorted({w for (_, w) in desk.cells})
    if weeks:
        desk.close(weeks_between)
    document = {"schema_version": 1, "kind": AGGREGATE_KIND, "source": source,
                "pulled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "objects_read": counts,
                "customers": {stream: {w: plain(c) for (st, w), c in sorted(desk.cells.items()) if st == stream}
                              for stream in sorted({st for (st, _) in desk.cells})},
                "semantics": "AGGREGATES_ONLY_NO_RECORDS"}
    out.mkdir(parents=True, exist_ok=True)
    target = out / name
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    return {"status": "PULLED", "file": str(target), "objects_read": counts}


def pull_zendesk(base: str, auth: dict, out: Path, days: int, now: datetime | None = None,
                 entities: list[dict] | None = None) -> dict:
    """Tickets from Zendesk's incremental export (with solve times from metric sets), read with a token, aggregated
    as each page arrives. Subjects are matched against the business items' names (when given) and dropped; no
    ticket, requester or subject is written."""
    now = now or datetime.now(timezone.utc)
    host = urllib.parse.urlsplit(base).netloc
    desk = customers.Desk(now, week_of, customers.vocabulary(entities or []))
    url = f"{base}/api/v2/incremental/tickets/cursor.json?start_time={int((now - timedelta(days=days)).timestamp())}" \
          "&include=metric_sets"
    count = pages = 0
    while url and pages < MAX_PAGES:
        if urllib.parse.urlsplit(url).netloc != host:
            raise OperationsError("zendesk: the next page is on another host; not following it with the token")
        page = _get_json(url, auth, "zendesk tickets")
        pages += 1
        solved = {m.get("ticket_id"): m.get("solved_at") for m in page.get("metric_sets") or []}
        for t in page.get("tickets") or []:
            tags = t.get("tags") or []
            desk.add("tickets", {"created": moment(t.get("created_at")), "solved": moment(solved.get(t.get("id"))),
                                 "status": t.get("status"), "priority": t.get("priority"),
                                 "topic": ",".join(tags) if tags else t.get("type"),
                                 "channel": (t.get("via") or {}).get("channel"),
                                 "satisfaction": (t.get("satisfaction_rating") or {}).get("score"),
                                 "subject": t.get("subject")}, label)
            count += 1
        if page.get("end_of_stream") or not page.get("after_url"):
            break
        url = page["after_url"]
    return _write_customers(out, "zendesk-api.ontologyex-aggregate.json", "zendesk_api", desk, now, {"tickets": count})


HUBSPOT_PROPERTIES = "dealstage,amount,deal_currency_code,createdate,closedate,closed_lost_reason,hs_analytics_source"


def pull_hubspot(token: str, out: Path, now: datetime | None = None, base: str = "https://api.hubapi.com") -> dict:
    """Deals from HubSpot with a private app token that can read deals; won and lost come from each pipeline's
    stage metadata. No deal name, company or owner is read."""
    now = now or datetime.now(timezone.utc)
    auth = {"Authorization": f"Bearer {token}"}
    kinds = {}
    for pipeline in _get_json(f"{base}/crm/v3/pipelines/deals", auth, "hubspot pipelines").get("results", []):
        for stage in pipeline.get("stages", []):
            meta = stage.get("metadata") or {}
            closed = str(meta.get("isClosed", "")).lower() == "true"
            won = closed and float(meta.get("probability") or 0) >= 1
            kinds[stage.get("id")] = "closedwon" if won else "closedlost" if closed else "open"
    desk = customers.Desk(now, week_of)
    url, count, pages = f"{base}/crm/v3/objects/deals?limit=100&properties={HUBSPOT_PROPERTIES}", 0, 0
    while url and pages < MAX_PAGES:
        page = _get_json(url, auth, "hubspot deals")
        pages += 1
        for deal in page.get("results") or []:
            p = deal.get("properties") or {}
            desk.add("deals", {"created": moment(p.get("createdate")), "close": moment(p.get("closedate")),
                               "stage": kinds.get(p.get("dealstage"), p.get("dealstage") or ""),
                               "amount": amount(p.get("amount")), "currency": p.get("deal_currency_code"),
                               "lost_reason": p.get("closed_lost_reason"), "source": p.get("hs_analytics_source")}, label)
            count += 1
        after = ((page.get("paging") or {}).get("next") or {}).get("after")
        url = f"{base}/crm/v3/objects/deals?limit=100&properties={HUBSPOT_PROPERTIES}&after={urllib.parse.quote(str(after))}" \
            if after else None
    return _write_customers(out, "hubspot-api.ontologyex-aggregate.json", "hubspot_api", desk, now, {"deals": count})


def pull_aws(out: Path, months: int, aws: str = "aws", now: datetime | None = None) -> dict:
    """Monthly cost by service from AWS Cost Explorer, through the aws command line and the credentials it is
    already set up with (a read-only call, ce:GetCostAndUsage; AWS charges a small fee per request). Writes
    aggregates only."""
    now = now or datetime.now(timezone.utc)
    first = now.replace(day=1)
    for _ in range(max(1, months) - 1):
        first = (first - timedelta(days=1)).replace(day=1)
    cells: dict = {}
    token, pages = None, 0
    while pages < 50:
        command = [aws, "ce", "get-cost-and-usage", "--time-period",
                   f"Start={first:%Y-%m-%d},End={(now + timedelta(days=1)):%Y-%m-%d}", "--granularity", "MONTHLY",
                   "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE", "--output", "json",
                   *(["--next-page-token", token] if token else [])]
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            raise OperationsError("the aws command line is not installed (or not on PATH)") from None
        if run.returncode != 0:
            raise OperationsError("aws ce get-cost-and-usage failed: " + (run.stderr or run.stdout).strip()[:300])
        answer = json.loads(run.stdout)
        pages += 1
        for period in answer.get("ResultsByTime", []):
            month = str((period.get("TimePeriod") or {}).get("Start", ""))[:7]
            for group in period.get("Groups", []):
                metric = (group.get("Metrics") or {}).get("UnblendedCost") or {}
                currency = str(metric.get("Unit", "")).lower()
                cost = amount(metric.get("Amount"))
                if not re.fullmatch(r"\d{4}-\d{2}", month) or not re.fullmatch(r"[a-z]{3}", currency) or cost is None:
                    continue
                c = cells.setdefault(currency, {}).setdefault(month, spend.cell("cloud_costs"))
                c["records"] += 1
                c["cost"] += cost
                spend.add_to(c["services"], label((group.get("Keys") or ["unspecified"])[0]), cost)
                spend.add_to(c["providers"], "Amazon Web Services", cost)
        token = answer.get("NextPageToken")
        if not token:
            break
    document = {"schema_version": 1, "kind": AGGREGATE_KIND, "source": "aws_cost_explorer",
                "pulled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "months": months,
                "spend": {"cloud_costs": {cur: {m: spend.plain(c) for m, c in sorted(by.items())}
                                          for cur, by in sorted(cells.items())}},
                "semantics": "AGGREGATES_ONLY_NO_RECORDS"}
    out.mkdir(parents=True, exist_ok=True)
    target = out / "aws-cost-explorer.ontologyex-aggregate.json"
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    return {"status": "PULLED", "file": str(target), "months": sorted({m for by in cells.values() for m in by})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="the ledger for a folder of exports")
    rep.add_argument("--dir", type=Path, required=True)
    rep.add_argument("--format", choices=("json", "markdown"), default="json")
    rep.add_argument("--min-cell", type=int, default=MIN_CELL)
    pull = sub.add_parser("pull", help="aggregate a read-only API into the folder")
    pull.add_argument("provider", choices=("stripe", "aws", "zendesk", "hubspot"))
    pull.add_argument("--subdomain", help="zendesk: your Zendesk subdomain (the part before .zendesk.com)")
    pull.add_argument("--out", type=Path, required=True)
    pull.add_argument("--days", type=int, default=180)
    pull.add_argument("--months", type=int, default=6, help="aws: months of cost by service")
    pull.add_argument("--aws", default="aws", help="aws: the aws command line to run")
    pull.add_argument("--key-env", default="STRIPE_API_KEY", help="the environment variable holding the key")
    pull.add_argument("--api-base", default="https://api.stripe.com")
    pull.add_argument("--allow-secret-key", action="store_true",
                      help="accept an sk_ key (it can change the account; a restricted read-only key is safer)")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            answer = report(args.dir, min_cell=max(1, args.min_cell))
            print(markdown(answer) if args.format == "markdown" else json.dumps(answer, indent=2, sort_keys=True))
            return 0
        if args.provider == "aws":
            print(json.dumps(pull_aws(args.out, min(max(args.months, 1), 36), args.aws), indent=2))
            return 0
        days = min(max(args.days, 1), 3650)
        if args.provider == "zendesk":
            if not args.subdomain or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.subdomain):
                raise OperationsError("give --subdomain, the part of your Zendesk address before .zendesk.com")
            oauth = os.environ.get("ZENDESK_OAUTH_TOKEN", "")
            email, token = os.environ.get("ZENDESK_EMAIL", ""), os.environ.get("ZENDESK_API_TOKEN", "")
            if oauth:
                auth = {"Authorization": f"Bearer {oauth}"}
            elif email and token:
                import base64
                auth = {"Authorization": "Basic " + base64.b64encode(f"{email}/token:{token}".encode()).decode()}
            else:
                raise OperationsError("set ZENDESK_OAUTH_TOKEN (an OAuth token with the read scope), or ZENDESK_EMAIL "
                                      "and ZENDESK_API_TOKEN")
            print(json.dumps(pull_zendesk(f"https://{args.subdomain}.zendesk.com", auth, args.out, days), indent=2))
            return 0
        if args.provider == "hubspot":
            token = os.environ.get("HUBSPOT_TOKEN", "")
            if not token:
                raise OperationsError("set HUBSPOT_TOKEN to a private app token with the crm.objects.deals.read scope")
            print(json.dumps(pull_hubspot(token, args.out), indent=2))
            return 0
        key = os.environ.get(args.key_env, "")
        if not key:
            raise OperationsError(f"set {args.key_env} to a restricted, read-only key")
        print(json.dumps(pull_stripe(key, args.out, min(max(args.days, 1), 3650), args.api_base,
                                     args.allow_secret_key), indent=2))
        return 0
    except OperationsError as exc:
        print(f"operations: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
