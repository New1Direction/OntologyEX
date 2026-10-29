"""Customers: what they ask for and what they buy, from support tickets and sales deals, as aggregates.

Read by operations.py from the same folder, as weekly aggregates per stream:

- tickets (Zendesk, Freshdesk, Intercom, Help Scout or any helpdesk export): opened, solved,
  the open backlog at each week's end, the median hours to solve, by topic (the helpdesk's own
  tags, type or category), priority and channel, satisfaction, and how many mention each
  business item;
- deals (HubSpot, Salesforce or any CRM export): created, won and lost, their amounts per
  currency, win rate, the median days from created to closed, why deals were lost, and where
  they came from.

What is never kept: a ticket's subject or body, a requester, an agent, a deal's name, its
company or its owner. A subject is read for one thing only: whether it names an item of the
business model (an action like "refund", an object like "Invoice"), so tickets can be counted
against the business; the subject itself is discarded. Topics, reasons and sources are labels
from the system's own fields, and anything shaped like an email or an identifier counts as
"other". Counts and medians of a week with fewer than `min_cell` tickets or deals are withheld
when shown.

Joined with the payment records, this says what neither says alone: refunds rising while
tickets about refunds rise too, a topic customers keep raising that nothing in the model is
named for, and sales slowing while support load grows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import re

STREAMS = ("tickets", "deals")
TITLES = {"tickets": "Support tickets", "deals": "Sales deals"}
MAX_LABELS = 40

COLUMNS = {
    "created": ("created_at", "created", "created_time", "create_date", "created_date", "date_created", "opened_at",
                "createdate", "created_on"),
    "solved": ("solved_at", "resolved_at", "resolved_time", "closed_at", "solved", "resolution_time", "closed_time",
               "date_closed"),
    "status": ("status", "state", "ticket_status"),
    "priority": ("priority", "urgency"),
    "topic": ("tags", "conversation_tags", "topic", "reason_for_contact"),
    "ticket_type": ("type", "ticket_type", "issue_type", "category"),
    "channel": ("via", "channel", "source", "ticket_source"),
    "satisfaction": ("satisfaction", "satisfaction_score", "csat", "rating", "satisfaction_rating"),
    "subject": ("subject", "title", "summary", "ticket_subject"),
    "stage": ("deal_stage", "stage", "dealstage", "opportunity_stage", "stage_name"),
    "amount": ("amount", "deal_amount", "value", "opportunity_amount", "amount_in_company_currency"),
    "currency": ("currency", "deal_currency_code", "currency_code", "currencyisocode"),
    "close": ("close_date", "closedate", "closed_date", "date_closed", "close_date_utc"),
    "lost_reason": ("closed_lost_reason", "lost_reason", "loss_reason", "reason_lost", "closed_lost_reason_"),
    "source": ("lead_source", "original_source", "source", "deal_source", "original_traffic_source"),
    "pipeline": ("pipeline",),
}
# Durations are kept as counts in fixed buckets (upper bounds), never as one number per ticket or deal;
# medians are read from the buckets.
HOUR_BINS = (0.5, 1, 2, 4, 8, 12, 24, 48, 72, 120, 168, 336, 720)
DAY_BINS = (1, 3, 7, 14, 21, 30, 45, 60, 90, 180, 365)
WON = re.compile(r"closed\s*won|\bwon\b|closedwon", re.I)
LOST = re.compile(r"closed\s*lost|\blost\b|closedlost", re.I)
SOLVED = {"solved", "closed", "resolved", "done", "completed"}
GOOD = re.compile(r"^(good|satisfied|positive|great|happy|[45](\.0)?|4/5|5/5|thumbs.?up)$", re.I)
BAD = re.compile(r"^(bad|unsatisfied|negative|poor|unhappy|[12](\.0)?|1/5|2/5|thumbs.?down)$", re.I)


def find_columns(headers: list[str], norm) -> dict[str, str]:
    by_norm = {norm(h): h for h in headers}
    found = {}
    for canonical, names in COLUMNS.items():
        for name in names:
            if name in by_norm and by_norm[name] not in found.values():
                found[canonical] = by_norm[name]
                break
    return found


def recognize(rel: str, found: dict[str, str]) -> tuple[str | None, str]:
    have, lower = set(found), rel.lower()
    if "stage" in have and "created" in have and ({"amount", "close"} & have):
        return "deals", "columns"
    if "created" in have and "status" in have and ({"subject", "topic", "ticket_type", "priority", "solved",
                                                      "satisfaction"} & have) \
            and ("ticket" in lower or "conversation" in lower or "support" in lower or {"priority", "solved"} <= have
                 or "satisfaction" in have):
        return "tickets", "columns"
    return None, ""


def cell(stream: str) -> dict:
    if stream == "tickets":
        return {"records": 0, "opened": 0, "solved": 0, "backlog": 0, "hours": [0] * (len(HOUR_BINS) + 1), "topics": {},
                "priorities": {}, "channels": {}, "good": 0, "bad": 0, "mentions": {}}
    return {"records": 0, "created": 0, "won": 0, "lost": 0, "won_amount": {}, "lost_amount": {},
            "days": [0] * (len(DAY_BINS) + 1), "lost_reasons": {}, "sources": {}, "open": 0}


def into(bins: list[int], bounds: tuple, value: float) -> None:
    bins[next((i for i, b in enumerate(bounds) if value <= b), len(bounds))] += 1


def median_of(bins: list[int], bounds: tuple) -> float | None:
    """The median read from bucket counts, by straight line within its bucket (the last one is open)."""
    total = sum(bins)
    if not total:
        return None
    half, seen = total / 2, 0
    for i, n in enumerate(bins):
        if n and seen + n >= half:
            if i == len(bounds):
                return float(bounds[-1])
            low = bounds[i - 1] if i else 0
            return round(low + (half - seen) / n * (bounds[i] - low), 1)
        seen += n
    return float(bounds[-1])


def add_bins(a: list[int], b: list[int]) -> list[int]:
    return [x + y for x, y in zip(a, b)]


def bump(counter: dict, key: str, by=1) -> None:
    key = key if key in counter or len(counter) < MAX_LABELS else "other"
    counter[key] = counter.get(key, 0) + by


class Desk:
    """Weekly aggregates of tickets and deals; no ticket or deal is kept."""

    def __init__(self, now: datetime, week_of, vocabulary: list[tuple[str, re.Pattern]] | None = None):
        self.cells: dict[tuple[str, str], dict] = {}
        self.now, self.week_of = now, week_of
        self.vocabulary = vocabulary or []
        self.spans: list[tuple[str, str, datetime, datetime | None]] = []  # (stream, first week, start, end)

    def at(self, stream: str, week: str) -> dict:
        if (stream, week) not in self.cells:
            self.cells[(stream, week)] = cell(stream)
        return self.cells[(stream, week)]

    def add(self, stream: str, r: dict, label) -> bool:
        created = r.get("created")
        if created is None:
            return False
        c = self.at(stream, self.week_of(created))
        c["records"] += 1
        if stream == "tickets":
            c["opened"] += 1
            for topic in topics(r.get("topic") or r.get("ticket_type"), label):  # tags, else the ticket's type
                bump(c["topics"], topic)
            if r.get("priority"):
                bump(c["priorities"], label(r["priority"]))
            if r.get("channel"):
                bump(c["channels"], label(r["channel"]))
            score = str(r.get("satisfaction") or "").strip()
            c["good"] += bool(GOOD.match(score))
            c["bad"] += bool(BAD.match(score))
            subject = r.get("subject") or ""
            for name, pattern in self.vocabulary:  # read only to count mentions; never kept
                if pattern.search(subject):
                    bump(c["mentions"], name)
            solved = r.get("solved")
            status = (r.get("status") or "").strip().lower()
            if solved is None and status in SOLVED:
                solved = created  # solved at an unknown time: never in the backlog, no time to solve
            if solved is not None:
                done = self.at(stream, self.week_of(solved))
                done["solved"] += 1
                if solved > created:
                    into(done["hours"], HOUR_BINS, (solved - created).total_seconds() / 3600)
            self.spans.append(("tickets", self.week_of(created), created, solved))
            return True
        c["created"] += 1
        stage = r.get("stage") or ""
        currency = (r.get("currency") or "").strip().lower()[:3] or "unknown"
        value = r.get("amount") or Decimal(0)
        if r.get("source"):
            bump(c["sources"], label(r["source"]))
        closed = r.get("close")
        if WON.search(stage) or LOST.search(stage):
            when = closed or created
            done = self.at(stream, self.week_of(when))
            won = bool(WON.search(stage))
            done["won" if won else "lost"] += 1
            bump(done["won_amount" if won else "lost_amount"], currency, value)
            if not won:
                bump(done["lost_reasons"], label(r.get("lost_reason") or "unspecified"))
            if when > created:
                into(done["days"], DAY_BINS, (when - created).total_seconds() / 86400)
            self.spans.append(("deals", self.week_of(created), created, when))
        else:
            self.spans.append(("deals", self.week_of(created), created, None))
        return True

    def close(self, weeks_between) -> None:
        """Count what was open at the end of each week: the ticket backlog and the open deals."""
        weeks = sorted({w for (_, w) in self.cells})
        if not weeks:
            return
        last = weeks[-1]
        for stream, first, start, end in self.spans:
            end_week = self.week_of(end) if end is not None else None
            for w in weeks_between(first, last):
                if end_week is not None and w >= end_week:
                    break
                self.at(stream, w)["backlog" if stream == "tickets" else "open"] += 1
        self.spans = []


def topics(value, label) -> list[str]:
    """A helpdesk's tags or type as topic labels. Tags come comma-separated, or space-separated as
    lower-case words ("billing refund_request"); a type like "Feature Request" is one topic."""
    text = str(value or "").strip()
    if not text:
        return ["untagged"]
    if re.search(r"[,;|]", text):
        parts = re.split(r"\s*[,;|]\s*", text)
    elif " " in text and all(re.fullmatch(r"[a-z0-9_\-]+", p) for p in text.split()):
        parts = text.split()
    else:
        parts = [text]
    return [label(p) for p in parts if p][:6]


def vocabulary(entities: list[dict]) -> list[tuple[str, re.Pattern]]:
    """The business items a subject can mention: action and object labels, split into words."""
    out = []
    for e in entities:
        if e.get("layer") != "business" or not e.get("label"):
            continue
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", e["label"])
        stem = words[-1].lower() if words else ""
        if len(stem) < 4:
            continue
        out.append((e["label"], re.compile(r"\b" + re.escape(stem[:-1] if stem.endswith("s") else stem) + r"\w*", re.I)))
    return out[:200]


def total_bins(cells: list[dict], key: str) -> list[int]:
    out = None
    for c in cells:
        out = list(c[key]) if out is None else add_bins(out, c[key])
    return out or []


def shown(stream: str, c: dict, min_cell: int) -> dict:
    out = {k: v for k, v in c.items() if k not in ("hours", "days")}
    if stream == "tickets":
        out["median_hours_to_solve"] = median_of(c["hours"], HOUR_BINS)
    else:
        out["median_days_to_close"] = median_of(c["days"], DAY_BINS)
        out["won_amount"] = {k: float(round(v, 2)) for k, v in c["won_amount"].items()}
        out["lost_amount"] = {k: float(round(v, 2)) for k, v in c["lost_amount"].items()}
    if c["records"] < min_cell:
        for k, v in list(out.items()):
            if isinstance(v, dict) or k.startswith("median"):
                out[k] = None
        out["withheld"] = True
    return out


def section(desk: Desk, current: str, min_cell: int, weeks_between) -> dict | None:
    """The customers part of the operations ledger: weeks, totals, recent figures and signals."""
    if not desk.cells:
        return None
    desk.close(weeks_between)
    out, signals = {"streams": {}, "totals": {}, "recent": {}}, []
    for stream in STREAMS:
        rows = sorted((w, c) for (s, w), c in desk.cells.items() if s == stream)
        if not rows:
            continue
        span = weeks_between(rows[0][0], rows[-1][0])
        rows = [(w, dict(rows).get(w) or cell(stream)) for w in span]
        out["streams"][stream] = [{"week": w, **shown(stream, c, min_cell)} for w, c in rows]
        total = cell(stream)
        for _, c in rows:
            for k, v in c.items():
                if isinstance(v, list):
                    total[k] = add_bins(total[k], v)
                elif isinstance(v, dict):
                    for key, n in v.items():
                        bump(total[k], key, n)
                elif k not in ("backlog", "open"):
                    total[k] += v
        total["backlog" if stream == "tickets" else "open"] = rows[-1][1]["backlog" if stream == "tickets" else "open"]
        out["totals"][stream] = shown(stream, total, min_cell)
        done = [(w, c) for w, c in rows if w < current]
        now, before = [c for _, c in done[-4:]], [c for _, c in done[-8:-4]]
        if stream == "tickets" and now:
            opened, opened_before = sum(c["opened"] for c in now), sum(c["opened"] for c in before)
            hours = median_of(total_bins(now, "hours"), HOUR_BINS)
            hours_before = median_of(total_bins(before, "hours"), HOUR_BINS) if before else None
            rated = sum(c["good"] + c["bad"] for c in now)
            rated_before = sum(c["good"] + c["bad"] for c in before)
            bad = sum(c["bad"] for c in now) / rated if rated else None
            bad_before = sum(c["bad"] for c in before) / rated_before if rated_before else None
            mentions, mentions_before = {}, {}
            for c in now:
                for k, n in c["mentions"].items():
                    mentions[k] = mentions.get(k, 0) + n
            for c in before:
                for k, n in c["mentions"].items():
                    mentions_before[k] = mentions_before.get(k, 0) + n
            topics_now, topics_before = {}, {}
            for c in now:
                for k, n in c["topics"].items():
                    topics_now[k] = topics_now.get(k, 0) + n
            for c in before:
                for k, n in c["topics"].items():
                    topics_before[k] = topics_before.get(k, 0) + n
            backlog, backlog_before = now[-1]["backlog"], (before[-1]["backlog"] if before else None)
            out["recent"]["tickets"] = {"opened": opened, "opened_before": opened_before, "backlog": backlog,
                                        "backlog_before": backlog_before, "median_hours": hours,
                                        "median_hours_before": hours_before,
                                        "bad_share": round(bad, 3) if bad is not None else None,
                                        "bad_share_before": round(bad_before, 3) if bad_before is not None else None,
                                        "mentions": mentions if opened >= min_cell else None,
                                        "mentions_before": mentions_before if opened_before >= min_cell else None,
                                        "topics": topics_now if opened >= min_cell else None,
                                        "hours_bins": total_bins(now, "hours") if opened >= min_cell else None}
            if before and opened >= 20 and opened >= 1.5 * max(opened_before, 1):
                signals.append({"signal": "TICKETS_RISING", "level": "HIGH", "stream": "tickets",
                                "detail": f"{opened} tickets opened in the last 4 weeks, {opened_before} in the 4 before"})
            if backlog_before is not None and backlog >= 10 and backlog >= 1.3 * max(backlog_before, 1):
                signals.append({"signal": "BACKLOG_GROWING", "level": "MEDIUM", "stream": "tickets",
                                "detail": f"{backlog} tickets open at the end of the last complete week, "
                                          f"{backlog_before} four weeks earlier"})
            if hours is not None and hours_before and hours >= 1.5 * hours_before and hours >= 4:
                signals.append({"signal": "RESOLUTION_SLOWER", "level": "MEDIUM", "stream": "tickets",
                                "detail": f"the median ticket took {hours:g} hours to solve in the last 4 weeks, "
                                          f"{hours_before:g} in the 4 before"})
            if bad is not None and rated >= 10 and bad >= 0.2 and (bad_before is None or bad >= 1.5 * bad_before):
                signals.append({"signal": "SATISFACTION_DROPPING", "level": "HIGH", "stream": "tickets",
                                "detail": f"{bad:.0%} of rated tickets were rated bad in the last 4 weeks"
                                          + (f", {bad_before:.0%} in the 4 before" if bad_before is not None else "")})
            for topic, n in sorted(topics_now.items()):
                was = topics_before.get(topic, 0)
                if topic not in ("untagged", "other") and n >= 10 and n >= 3 * max(was, 1):
                    signals.append({"signal": "TOPIC_SPIKE", "level": "MEDIUM", "stream": "tickets", "topic": topic,
                                    "detail": f"tickets tagged {topic!r} went from {was} to {n} against the 4 weeks before"})
        if stream == "deals" and now:
            won, lost = sum(c["won"] for c in now), sum(c["lost"] for c in now)
            won_b, lost_b = sum(c["won"] for c in before), sum(c["lost"] for c in before)
            rate = won / (won + lost) if won + lost else None
            rate_before = won_b / (won_b + lost_b) if won_b + lost_b else None
            days = median_of(total_bins(now, "days"), DAY_BINS)
            reasons = {}
            for c in now:
                for k, n in c["lost_reasons"].items():
                    reasons[k] = reasons.get(k, 0) + n
            out["recent"]["deals"] = {"created": sum(c["created"] for c in now), "won": won, "lost": lost,
                                      "win_rate": round(rate, 3) if rate is not None else None,
                                      "win_rate_before": round(rate_before, 3) if rate_before is not None else None,
                                      "median_days_to_close": days, "open": now[-1]["open"],
                                      "lost_reasons": reasons if lost >= min_cell else None}
            if rate is not None and rate_before and won + lost >= 10 and rate <= 0.7 * rate_before:
                signals.append({"signal": "WIN_RATE_DOWN", "level": "HIGH", "stream": "deals",
                                "detail": f"{rate:.0%} of closed deals were won in the last 4 weeks, "
                                          f"{rate_before:.0%} in the 4 before"})
            if lost >= 10:
                top, n = max(reasons.items(), key=lambda kv: (kv[1], kv[0])) if reasons else (None, 0)
                if top not in (None, "unspecified", "other") and n >= 0.4 * lost:
                    signals.append({"signal": "LOSS_REASON_DOMINANT", "level": "MEDIUM", "stream": "deals",
                                    "detail": f"{n} of {lost} deals lost in the last 4 weeks were lost to {top!r}"})
    out["signals"] = signals
    return out


def joined(customers: dict | None, payments: dict | None, entities: list[dict]) -> list[dict]:
    """What tickets and payment records say together, and topics nothing in the model is named for."""
    if not customers or "tickets" not in customers.get("recent", {}):
        return []
    recent = customers["recent"]["tickets"]
    out = []
    mentions, before = recent.get("mentions") or {}, recent.get("mentions_before") or {}
    for currency, cur in (payments or {}).items():
        p = (cur.get("recent") or {}).get("payments") or {}
        rate, rate_before = p.get("refund_rate"), p.get("refund_rate_before")
        refund_items = [k for k in mentions if re.search(r"refund", k, re.I)]
        # Items named alike ("Refund", "IssueRefund") match the same tickets: the largest count, not their sum.
        asked = max((mentions[k] for k in refund_items), default=0)
        asked_before = max((before.get(k, 0) for k in refund_items), default=0)
        if rate and rate_before and rate >= 1.5 * rate_before and asked >= 5 and asked >= 1.5 * max(asked_before, 1):
            out.append({"finding": "REFUNDS_CONFIRMED_BY_TICKETS", "currency": currency,
                        "detail": f"refunds rose to {rate:.1%} of payments ({rate_before:.1%} before), and tickets "
                                  f"mentioning {', '.join(sorted(refund_items))} rose to {asked} ({asked_before} before): "
                                  "two sources agree, so look at what changed in the refund journey",
                        "standing": "SOURCE"})
    labels = {e.get("label", "").lower() for e in entities if e.get("label")}
    words = {w for lab in labels for w in re.findall(r"[a-z]{4,}", lab)}
    for topic, n in sorted((recent.get("topics") or {}).items(), key=lambda kv: -kv[1]):
        if topic in ("untagged", "other") or n < 10:
            continue
        stems = [w[:5] for w in re.findall(r"[a-z]{4,}", topic)]
        if stems and entities and not any(w.startswith(s) or s.startswith(w[:5]) for s in stems for w in words):
            out.append({"finding": "TOPIC_NOT_MODELED", "topic": topic,
                        "detail": f"{n} tickets in the last 4 weeks are tagged {topic!r}, and nothing in the business "
                                  "model is named for it: a part of the business customers deal with that is not "
                                  "written down", "standing": "INFERRED"})
    return out[:20]
