"""Written operations: what the business says it does, checked against what its records show.

Policies, procedures, terms and contracts (Markdown, text, HTML, Word and simple PDF files in
the operations folder) are read for commitments a business can keep or break, each cited to
the file and line it is written on:

- REFUND_WINDOW: refunds within N days;
- REFUND_APPROVAL: refunds over an amount need someone's approval;
- RESOLUTION_TIME: tickets resolved within N hours;
- DISPUTE_RESPONSE: disputes or chargebacks answered within N days;
- PAYMENT_TERMS: invoices or bills paid within N days (net N);
- RENEWAL_NOTICE: a contract that renews on its own unless notice is given N days before;
- UPTIME: an availability promise, in percent.

Each commitment is then held to the records where the records can say:
- RESOLUTION_TIME against the share of tickets solved within that time;
- PAYMENT_TERMS against bills past due;
- REFUND_APPROVAL against the business model and code (is there a rule or an approval step);
- DISPUTE_RESPONSE against disputes open when exported (a person checks their deadlines).
What the aggregates cannot settle (a refund window, uptime) is said to need a person, never
guessed. Two documents that state different values for the same commitment are a conflict.
And what the records show happening (refunds, disputes, support) with no written procedure at
all is "not written down".

Quoted lines have emails, phone numbers and long numbers masked; nothing else is taken from a
document. Matching is by sentence patterns, so a commitment worded differently is missed, and
every finding says which line it rests on so a person can check it.
"""
from __future__ import annotations

import html
import re
import zipfile
import zlib
from pathlib import Path

SUFFIXES = (".md", ".markdown", ".txt", ".html", ".htm", ".docx", ".pdf")
MAX_DOC_BYTES = 20 * 1024 * 1024
MAX_LINES = 50_000
NUM = r"(\d{1,4}(?:\.\d+)?)"
UNITS = r"(business days?|calendar days?|days?|business hours?|hours?|weeks?)"

# (kind, pattern, how to read the value); patterns run on one sentence at a time, lower-cased.
PATTERNS = (
    ("REFUND_WINDOW", re.compile(rf"refund\w*\b[^.]{{0,80}}?\bwithin {NUM} {UNITS}"), "days"),
    ("REFUND_WINDOW", re.compile(rf"{NUM}[- ]day (?:money[- ]back|refund)"), "days"),
    ("REFUND_APPROVAL", re.compile(r"refund\w*\b[^.]{0,40}?\b(?:over|above|exceeding|greater than|more than) "
                                   r"(?:[$€£]|usd |eur |gbp )?([\d,]+(?:\.\d+)?)[^.]{0,80}?\b(?:approv|sign[- ]off|sign off)"),
     "amount"),
    ("RESOLUTION_TIME", re.compile(rf"(?:resolv\w*|solv\w*|close\w*)\b[^.]{{0,60}}?\bwithin {NUM} {UNITS}"), "hours"),
    ("DISPUTE_RESPONSE", re.compile(rf"(?:dispute|chargeback)\w*\b[^.]{{0,60}}?\bwithin {NUM} {UNITS}"), "days"),
    ("PAYMENT_TERMS", re.compile(r"\bnet[ -]?(\d{1,3})\b"), "days_plain"),
    ("PAYMENT_TERMS", re.compile(rf"(?:invoice|bill|payment)\w*\b[^.]{{0,40}}?\b(?:due|paid|payable) within {NUM} {UNITS}"),
     "days"),
    ("PAYMENT_TERMS", re.compile(rf"\bpay\w*\b[^.]{{0,40}}?(?:invoice|bill)\w*[^.]{{0,20}}?\bwithin {NUM} {UNITS}"),
     "days"),
    ("RENEWAL_NOTICE", re.compile(rf"(?:auto[- ]?renew|automatically renew|renews? automatically)[^.]{{0,160}}?\b{NUM}"
                                  rf"\s?(days?|months?)'?s? (?:prior |advance )?(?:written )?notice"), "notice"),
    ("RENEWAL_NOTICE", re.compile(rf"\b{NUM}\s?(days?|months?)'?s? (?:prior |advance )?(?:written )?notice[^.]{{0,120}}?"
                                  r"(?:renew|cancel)"), "notice"),
    ("UPTIME", re.compile(r"(\d{2}(?:\.\d{1,3})?)\s?%[^.]{0,40}?\b(?:uptime|availability|available)"), "percent"),
)
TITLES = {"REFUND_WINDOW": "Refund window", "REFUND_APPROVAL": "Refunds that need approval",
          "RESOLUTION_TIME": "Time to resolve tickets", "DISPUTE_RESPONSE": "Time to answer disputes",
          "PAYMENT_TERMS": "Payment terms", "RENEWAL_NOTICE": "Renewal notice", "UPTIME": "Uptime"}
# Why the records cannot settle a commitment, and what a person checks instead.
UNSETTLED = {
    "REFUND_WINDOW": "the records hold refunds by week, not how long after its purchase each came; check a sample of "
                     "recent refunds in the processor's dashboard",
    "RENEWAL_NOTICE": "the records hold no contract dates; put the last day to give notice in a calendar",
    "UPTIME": "the health feeds report status, not availability measured over the contract's period",
    "DISPUTE_RESPONSE": "no open dispute in the records",
    "RESOLUTION_TIME": "no tickets in the records to measure it against",
    "PAYMENT_TERMS": "no bills in the records to measure it against",
    "REFUND_APPROVAL": "no business model to look for the approval step in",
}
# The procedures a business needs once its records show it doing these things.
PROCEDURES = {"refunds": r"refund", "disputes": r"dispute|chargeback", "support": r"support|ticket|help ?desk",
              "payouts": r"payout|settlement|reconcil"}
MASK = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d ()-]{7,}\d|\b\d{7,}\b")


def text_of(path: Path) -> tuple[str | None, str]:
    """The text of a document and how it was read, or None and why not."""
    suffix = path.suffix.lower()
    data = path.read_bytes()[:MAX_DOC_BYTES]
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError, OSError):
            return None, "not a readable Word file"
        xml = re.sub(r"</w:p>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml)), "Word"
    if suffix == ".pdf":
        return pdf_text(data)
    text = data.decode("utf-8", "replace")
    if suffix in (".html", ".htm"):
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", text)
        return html.unescape(re.sub(r"<[^>]+>", "", text)), "HTML"
    return text, "text"


def pdf_text(data: bytes) -> tuple[str | None, str]:
    """Text from a simple PDF: the strings shown by its (Flate-compressed or plain) content streams. Scanned or
    unusually encoded PDFs give nothing; save them as text or Word instead."""
    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = match.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        lines = []
        for text_block in re.findall(rb"BT(.*?)ET", raw, re.S):
            parts = re.findall(rb"\((?:\\.|[^\\)])*\)\s*Tj|\[(?:.*?)\]\s*TJ|T\*|Td|TD|'", text_block, re.S)
            line = []
            for part in parts:
                if part in (b"T*", b"Td", b"TD", b"'"):
                    if line:
                        lines.append(b"".join(line))
                        line = []
                    continue
                for s in re.findall(rb"\(((?:\\.|[^\\)])*)\)", part):
                    line.append(re.sub(rb"\\(.)", rb"\1", s))
            if line:
                lines.append(b"".join(line))
        chunks += [line.decode("latin-1") for line in lines]
    text = "\n".join(chunks)
    return (text, "PDF") if text.strip() else (None, "no text found in the PDF (scanned or encoded); save it as text")


def sentences(line: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]


def value_of(how: str, groups: tuple) -> tuple[float, str] | None:
    """The number a pattern found, in the unit its kind is compared in (hours, days, an amount or a percent)."""
    number = float(str(groups[0]).replace(",", ""))
    unit = (groups[1] if len(groups) > 1 and groups[1] else "").lower()
    if how == "amount":
        return number, "amount"
    if how == "percent":
        return (number, "percent") if 90 <= number <= 100 else None
    if how == "days_plain":
        return (number, "days") if 1 <= number <= 180 else None
    if how == "notice":
        return number * (30 if unit.startswith("month") else 1), "days"
    hours = number if "hour" in unit else number * 24 * (7 if unit.startswith("week") else 1)
    return (hours, "hours") if how == "hours" else (round(hours / 24, 2), "days")


def commitments(rel: str, text: str) -> list[dict]:
    found = []
    for n, line in enumerate(text.splitlines()[:MAX_LINES], start=1):
        for sentence in sentences(line):
            low = re.sub(r"\s+", " ", sentence.lower())
            for kind, pattern, how in PATTERNS:
                match = pattern.search(low)
                if not match:
                    continue
                value = value_of(how, match.groups())
                if value is None:
                    continue
                if any(f["kind"] == kind and f["line"] == n and f["file"] == rel for f in found):
                    continue
                quote = MASK.sub("…", re.sub(r"\s+", " ", sentence.strip()))[:240]
                found.append({"kind": kind, "value": value[0], "unit": value[1], "file": rel, "line": n, "text": quote,
                              "business_days": "business" in low})
    return found


def kind_of(rel: str, text: str) -> str:
    low = (rel + " " + text[:3000]).lower()
    if re.search(r"order form|master (services|subscription) agreement|\bmsa\b|this agreement|hereinafter|contract", low):
        return "contract"
    if re.search(r"terms of (service|use)|terms and conditions|\bterms\b", rel.lower()):
        return "terms"
    if re.search(r"procedure|runbook|playbook|how to|\bsop\b|checklist", low):
        return "procedure"
    return "policy"


def read(path: Path, rel: str) -> tuple[dict, list[dict]]:
    """One document: what it is, how it was read, and its commitments."""
    text, how = text_of(path)
    if text is None:
        return {"file": rel, "status": "UNREADABLE", "detail": how}, []
    found = commitments(rel, text)
    return {"file": rel, "status": "READ", "kind": kind_of(rel, text), "read_as": how,
            "lines": text.count("\n") + 1, "commitments": len(found),
            "procedures": sorted(p for p, pattern in PROCEDURES.items() if re.search(pattern, text, re.I))}, found


def share_within(bins: list[int], bounds: tuple, limit: float) -> float | None:
    """The share of durations at or below a limit, from bucket counts (buckets wholly under it)."""
    total = sum(bins)
    if not total:
        return None
    under = sum(n for n, upper in zip(bins, bounds) if upper <= limit)
    return round(under / total, 3)


def check(found: list[dict], documents: list[dict], ledger: dict, entities: list[dict]) -> dict:
    """Commitments held to the records, conflicts between documents, and what nobody wrote down."""
    import customers
    hour_bins = (((ledger.get("customers") or {}).get("recent") or {}).get("tickets") or {}).get("hours_bins")
    bills = [m for cur in ((ledger.get("spend") or {}).get("currencies") or {}).values()
             for m in cur.get("months", {}).get("bills", [])]
    disputes_open = sum(((c.get("recent") or {}).get("disputes") or {}).get("open", 0)
                        for c in (ledger.get("currencies") or {}).values())
    labels = " ".join(f"{e.get('label', '')}" for e in entities)
    rules = " ".join(r.get("statement", "") for e in entities for r in e.get("rules", []) if isinstance(r, dict))
    out = []
    for c in found:
        status, detail = "NEEDS_A_PERSON", UNSETTLED.get(c["kind"], "the records here cannot settle it")
        if c["kind"] == "RESOLUTION_TIME" and hour_bins:
            share = share_within(hour_bins, customers.HOUR_BINS, c["value"])
            if share is not None:
                status = "MET" if share >= 0.9 else "NOT_MET"
                detail = (f"{share:.0%} of the tickets solved in the last 4 complete weeks took {c['value']:g} hours or "
                          "less" + (" (clock hours; the promise counts business hours)" if c["business_days"] else ""))
        elif c["kind"] == "PAYMENT_TERMS" and bills:
            overdue = sum(m.get("overdue", 0) for m in bills)
            status = "NOT_MET" if overdue else "MET"
            detail = (f"{overdue} bill(s) are past their due date and not marked paid" if overdue else
                      "no bill in the records is past its due date")
        elif c["kind"] == "REFUND_APPROVAL" and entities:
            amount = f"{c['value']:g}"
            coded = re.search(r"approv", labels, re.I) or re.search(re.escape(amount), rules)
            status = "IN_THE_MODEL" if coded else "NOT_IN_THE_MODEL"
            detail = ("the business model has an approval step or a rule with this amount" if coded else
                      "nothing in the business model or its rules is an approval step for this: the code may not "
                      "enforce it")
        elif c["kind"] == "DISPUTE_RESPONSE" and disputes_open:
            detail = f"{disputes_open} dispute(s) were open when exported; check each one's deadline"
        out.append({**c, "status": status, "detail": detail})
    conflicts = []
    by_kind: dict[str, list[dict]] = {}
    for c in out:
        if c["kind"] in ("REFUND_WINDOW", "RESOLUTION_TIME", "DISPUTE_RESPONSE") or (
                c["kind"] == "PAYMENT_TERMS" and not c["file"].startswith(("contracts/", "vendors/"))):
            by_kind.setdefault(c["kind"], []).append(c)
    for kind, rows in by_kind.items():
        values = sorted({r["value"] for r in rows})
        files = sorted({r["file"] for r in rows})
        if len(values) > 1 and len(files) > 1:
            conflicts.append({"kind": kind, "values": values, "unit": rows[0]["unit"],
                              "citations": [{"file": r["file"], "line": r["line"], "text": r["text"]} for r in rows][:6],
                              "detail": f"{TITLES[kind].lower()} is written as " + " and ".join(
                                  f"{v:g} {rows[0]['unit']}" for v in values) + " in different documents"})
    present = set(ledger.get("streams_present") or [])
    if (ledger.get("customers") or {}).get("streams", {}).get("tickets"):
        present.add("support")
    written = {p for d in documents for p in d.get("procedures", [])}
    unwritten = [{"procedure": p, "detail": f"the records show {p} happening, and no document here describes how "
                                             f"{p} are handled"} for p in sorted(PROCEDURES)
                 if p in present and p not in written]
    signals = []
    for c in out:
        if c["status"] == "NOT_MET":
            signals.append({"signal": "WRITTEN_PROMISE_NOT_KEPT", "level": "HIGH", "stream": "written",
                            "detail": f"{c['file']}:{c['line']} says \"{c['text']}\"; {c['detail']}"})
        elif c["status"] == "NOT_IN_THE_MODEL":
            signals.append({"signal": "WRITTEN_RULE_NOT_IN_CODE", "level": "MEDIUM", "stream": "written",
                            "detail": f"{c['file']}:{c['line']} says \"{c['text']}\"; {c['detail']}"})
    for conflict in conflicts:
        signals.append({"signal": "WRITTEN_RULES_CONFLICT", "level": "HIGH", "stream": "written",
                        "detail": conflict["detail"] + ": " + "; ".join(f"{x['file']}:{x['line']}"
                                                                       for x in conflict["citations"][:4])})
    for gap in unwritten:
        signals.append({"signal": "PROCEDURE_NOT_WRITTEN", "level": "MEDIUM", "stream": "written",
                        "topic": gap["procedure"], "detail": gap["detail"]})
    return {"documents": documents, "commitments": out, "conflicts": conflicts, "unwritten": unwritten,
            "signals": signals}
