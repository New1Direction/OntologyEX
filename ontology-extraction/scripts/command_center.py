#!/usr/bin/env python3
"""Everything that needs attention in a business, ranked by importance, each with why and what to do.

    python command_center.py --bundle BUNDLE.json

One list from every part of the model: business actions at risk, systems in error that
something relies on, confirmations whose evidence changed, critical suppliers with no
second one declared, questions nobody has answered, decisions waiting for a person,
and the parts of the business nobody can see yet. Each item has an importance score
from 0 to 100 built from named factors (how bad it is, how much of the business
reaches it, whether money moves through it), so the ranking can be read and argued
with, never taken on faith. Coverage says how much of the business is visible at all,
and the field profile says what a business of this kind should be able to see, and
which of those the sources show.

Read-only and deterministic: the same model gives the same ranking. Scores rank
attention; they are not probabilities, forecasts or approvals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from datetime import datetime

import entity_context as ec
import ownership
from policy_check import canonical, digest

LEVELS = ((70, "CRITICAL"), (50, "HIGH"), (30, "MEDIUM"), (0, "LOW"))
MONEY = re.compile(r"pay|refund|charge|invoice|billing|subscri|payout|order|checkout|price|pricing|revenue|ledger|"
                   r"wallet|transfer|fee|tax|stripe|money|cash|credit|debit|commission|purchase", re.I)
SEVERITY = {
    "SYSTEM_ERROR": (60, "a system the business relies on reports an error"),
    "AT_RISK": (55, "a linked system reports an error"),
    "POSSIBLY_IMPACTED": (40, "a system it runs on depends on a failing one"),
    "DEGRADED": (35, "a linked system reports a warning"),
    "STALE_CONFIRMATION": (30, "a person confirmed it, then the evidence changed"),
    "STALE_REPAIR": (25, "the code it was modeled from changed: the model may describe code that is gone"),
    "AGENT_BLOCKED": (30, "an agent tried something a business rule forbids"),
    "SINGLE_CRITICAL_SUPPLIER": (30, "the only supplier of a critical kind"),
    "LINK_GAP": (25, "a confirmed link points at a component that is gone"),
    "OPEN_QUESTION": (25, "the sources leave a question open"),
    "STALE": (20, "the last observation expired"),
    "NOT_LINKED": (20, "no system is linked, so its health cannot be seen"),
    "SINGLE_HIGH_SUPPLIER": (18, "the only supplier of an important kind"),
    "NEVER_OBSERVED": (15, "the business relies on it, and nothing reports its health"),
    "DECISIONS_WAITING": (10, "proposals wait for a person"),
    "NO_BUSINESS_MODEL": (50, "the business actions are not written down, so nothing here reaches the business"),
    "UNOBSERVED_SERVICES": (12, "services report no health at all"),
    "NO_OWNER": (20, "nobody is declared to own it, and nobody changed its code in a year"),
    "ONE_PERSON": (15, "only one person changed its code in a year"),
    "ONE_PERSON_REPOSITORY": (25, "only one person changed the whole repository in a year"),
    "NO_CODEOWNERS": (12, "no CODEOWNERS file names who owns any code"),
    "CARD_FIELDS": (45, "fields named like card numbers or security codes are in the data model"),
    "COMPLIANCE_GAPS": (18, "a standard that applies asks for controls the sources do not show"),
    "COMPLIANCE_ASKED": (10, "customers usually ask for this standard, and the sources do not show all of it"),
    "DISPUTES_OPEN": (65, "disputes wait for a response, and an unanswered dispute is lost"),
    "PAYOUT_FAILED": (65, "a payout failed: money did not reach the bank"),
    "REFUND_RATE_RISING": (45, "refunds take a growing share of what customers pay"),
    "REVENUE_DOWN": (45, "payments taken fell against the weeks before"),
    "DISPUTE_RATE_HIGH": (45, "disputes are above a common warning level"),
    "CHURN_RISING": (40, "more subscriptions are ending than before"),
    "FAILED_PAYMENTS_HIGH": (30, "many payment attempts fail"),
    "INVOICES_UNCOLLECTIBLE": (20, "invoices were written off as uncollectible"),
    "SUBSCRIPTIONS_PAST_DUE": (20, "subscriptions are past due"),
    "HAPPENS_NOT_IN_CODE": (20, "the records show the business doing something the sources do not show"),
    "IN_CODE_NOT_SEEN": (10, "something is built that the records never show in use"),
    "PAYOUTS_NOT_IN_BANK": (60, "money the payment processor paid out is not in the bank lines"),
    "DEPOSITS_WITHOUT_PAYOUTS": (30, "more arrived from payment processors than they paid out"),
    "BILLS_OVERDUE": (40, "bills are past due"),
    "SPEND_RISING": (35, "money out jumped against the months before"),
    "CLOUD_COST_RISING": (30, "cloud costs jumped against the month before"),
    "NEW_SUPPLIER_PAID": (12, "a supplier was paid for the first time"),
    "PAID_NOT_IN_CODE": (12, "a supplier is paid that the code does not use"),
    "IN_CODE_NOT_PAID": (10, "the code uses a supplier nobody is seen paying"),
    "REFUNDS_CONFIRMED_BY_TICKETS": (50, "refunds and tickets about refunds rose together: two sources agree"),
    "SATISFACTION_DROPPING": (40, "more customers rate their support as bad"),
    "WIN_RATE_DOWN": (40, "fewer of the deals that close are won"),
    "TICKETS_RISING": (35, "customers contact support more than before"),
    "TOPIC_SPIKE": (30, "one kind of ticket jumped"),
    "BACKLOG_GROWING": (25, "unsolved tickets pile up"),
    "RESOLUTION_SLOWER": (25, "tickets take longer to solve"),
    "LOSS_REASON_DOMINANT": (20, "one reason loses most deals"),
    "TOPIC_NOT_MODELED": (15, "customers keep raising something nothing in the model is named for"),
    "WRITTEN_PROMISE_NOT_KEPT": (45, "the records show a written promise is not kept"),
    "WRITTEN_RULES_CONFLICT": (40, "two documents promise different things"),
    "WRITTEN_RULE_NOT_IN_CODE": (25, "a written rule has no matching step in the business model or code"),
    "PROCEDURE_NOT_WRITTEN": (20, "the business does something its documents never describe"),
    "PULL_FAILING": (35, "a live pull fails, so the operations figures stop updating"),
    "PULLS_MISCONFIGURED": (35, "pulls.json cannot be read, so no live pull runs"),
    "PULL_STALE": (20, "a live pull has not succeeded for two of its intervals"),
}
CATEGORY = {"SYSTEM_ERROR": "Systems", "AT_RISK": "Business at risk", "POSSIBLY_IMPACTED": "Business at risk",
            "DEGRADED": "Business at risk", "STALE": "Business at risk", "STALE_CONFIRMATION": "Decisions", "STALE_REPAIR": "Decisions", "AGENT_BLOCKED": "Decisions",
            "DECISIONS_WAITING": "Decisions", "SINGLE_CRITICAL_SUPPLIER": "Suppliers", "SINGLE_HIGH_SUPPLIER": "Suppliers",
            "LINK_GAP": "Unknowns", "OPEN_QUESTION": "Unknowns", "NOT_LINKED": "Visibility", "NEVER_OBSERVED": "Visibility",
            "NO_BUSINESS_MODEL": "Visibility", "UNOBSERVED_SERVICES": "Visibility", "NO_OWNER": "Ownership",
            "ONE_PERSON": "Ownership", "ONE_PERSON_REPOSITORY": "Ownership", "NO_CODEOWNERS": "Ownership",
            "CARD_FIELDS": "Compliance", "COMPLIANCE_GAPS": "Compliance", "COMPLIANCE_ASKED": "Compliance",
            **{k: "Operations" for k in ("DISPUTES_OPEN", "PAYOUT_FAILED", "REFUND_RATE_RISING", "REVENUE_DOWN",
                                         "DISPUTE_RATE_HIGH", "CHURN_RISING", "FAILED_PAYMENTS_HIGH",
                                         "INVOICES_UNCOLLECTIBLE", "SUBSCRIPTIONS_PAST_DUE", "HAPPENS_NOT_IN_CODE",
                                         "IN_CODE_NOT_SEEN", "PULL_FAILING", "PULLS_MISCONFIGURED", "PULL_STALE")},
            **{k: "Spend" for k in ("PAYOUTS_NOT_IN_BANK", "DEPOSITS_WITHOUT_PAYOUTS", "BILLS_OVERDUE", "SPEND_RISING",
                                    "CLOUD_COST_RISING", "NEW_SUPPLIER_PAID", "PAID_NOT_IN_CODE", "IN_CODE_NOT_PAID")},
            **{k: "Customers" for k in ("REFUNDS_CONFIRMED_BY_TICKETS", "SATISFACTION_DROPPING", "WIN_RATE_DOWN",
                                        "TICKETS_RISING", "TOPIC_SPIKE", "BACKLOG_GROWING", "RESOLUTION_SLOWER",
                                        "LOSS_REASON_DOMINANT", "TOPIC_NOT_MODELED")},
            **{k: "Written" for k in ("WRITTEN_PROMISE_NOT_KEPT", "WRITTEN_RULES_CONFLICT", "WRITTEN_RULE_NOT_IN_CODE",
                                      "PROCEDURE_NOT_WRITTEN")}}
NEXT_STEP = {
    "SYSTEM_ERROR": "Open the system, read what it reports, and check the business actions that run on it.",
    "AT_RISK": "Open the action: see which linked system fails and what else relies on it.",
    "POSSIBLY_IMPACTED": "Follow the dependency to the failing system; the impact is possible, not confirmed.",
    "DEGRADED": "Check the warning on the linked system before it becomes an error.",
    "STALE_CONFIRMATION": "Re-read the changed lines, then re-confirm or undo the decision.",
    "AGENT_BLOCKED": "Find out why the agent tried it: a prompt or a task that asks for it, or a rule that is wrong. "
                     "Fix the one that is wrong (the audit is .ontologyex/guardrail-audit.jsonl).",
    "STALE_REPAIR": "Read the changed lines (the drift report names where they are now), then rebuild the skill from "
                    "the current code or re-cite the evidence.",
    "SINGLE_CRITICAL_SUPPLIER": "Decide whether a second provider or a written fallback is needed.",
    "SINGLE_HIGH_SUPPLIER": "Note who owns this supplier and what happens if it is down.",
    "LINK_GAP": "Undo the link or confirm the component that replaced it.",
    "OPEN_QUESTION": "Answer it in the domain skill's sources, or name who decides.",
    "STALE": "Restore the feed for the linked system.",
    "NOT_LINKED": "Confirm which system runs it (the Business page suggests links).",
    "NEVER_OBSERVED": "Add a health check or traces for it (feed_setup.py, otel_setup.py).",
    "DECISIONS_WAITING": "Review the proposals: each needs a person to confirm or reject it.",
    "NO_BUSINESS_MODEL": "Write a domain skill for the main workflow (guided onboarding) and start the control room "
                         "with --skill, so health, risk and change impact reach business actions.",
    "UNOBSERVED_SERVICES": "Add health checks (feed_setup.py) or traces (otel_setup.py), one command per service.",
    "NO_OWNER": "Name an owner: a CODEOWNERS line for the code behind it, so someone answers for it.",
    "ONE_PERSON": "Have a second person review or pair on it, so it does not depend on one person.",
    "ONE_PERSON_REPOSITORY": "Write down how the critical parts work and who else can change them.",
    "NO_CODEOWNERS": "Add a CODEOWNERS file: it names who reviews and answers for each part of the code.",
    "CARD_FIELDS": "Confirm no card number or security code is stored; if one is, move it to the payments supplier.",
    "COMPLIANCE_GAPS": "Open Compliance: each gap says what evidence would show it and what a person must provide.",
    "COMPLIANCE_ASKED": "Open Compliance before a customer's security review asks for it.",
    "DISPUTES_OPEN": "Answer each open dispute in the payment processor's dashboard before its evidence is due.",
    "PAYOUT_FAILED": "Check the bank account payouts go to, then have the processor retry the payout.",
    "REFUND_RATE_RISING": "Read the refund reasons on the Operations page and find what changed in that journey.",
    "REVENUE_DOWN": "Compare the weeks on the Operations page, and check the health of the systems checkout runs on.",
    "DISPUTE_RATE_HIGH": "Read the dispute reasons; fraud disputes call for stronger checks at checkout.",
    "CHURN_RISING": "See which plans lose subscribers on the Operations page, and ask the people who left why.",
    "FAILED_PAYMENTS_HIGH": "Read the decline reasons; retries and card-update reminders recover some of them.",
    "INVOICES_UNCOLLECTIBLE": "Review how unpaid invoices are chased, and when one is written off.",
    "SUBSCRIPTIONS_PAST_DUE": "Check the failed renewals and the reminders those customers get.",
    "HAPPENS_NOT_IN_CODE": "Find where it is handled (a person, a dashboard, another system) and write it down.",
    "IN_CODE_NOT_SEEN": "Check whether it is used, handled by another processor, or missing from the exports.",
    "PAYOUTS_NOT_IN_BANK": "Match each payout in the processor's dashboard to a bank deposit; ask the processor about any "
                           "that never arrived.",
    "DEPOSITS_WITHOUT_PAYOUTS": "Check for deposits from another processor or account that the exports leave out.",
    "BILLS_OVERDUE": "Pay or dispute the overdue bills before the supplier stops the service.",
    "SPEND_RISING": "Open Operations: see which category grew, and whether it was planned.",
    "CLOUD_COST_RISING": "See which service grew on the Operations page and whether usage grew with it.",
    "NEW_SUPPLIER_PAID": "Check who signed up for it and whether the code or a person uses it.",
    "PAID_NOT_IN_CODE": "Ask who uses it; cancel it if nobody does.",
    "IN_CODE_NOT_PAID": "Check which account pays for it, or whether production is on a free plan with limits.",
    "REFUNDS_CONFIRMED_BY_TICKETS": "Read the refund reasons and the refund tickets together, and find what changed in "
                                    "the refund journey.",
    "SATISFACTION_DROPPING": "Read the badly rated tickets of the last weeks: which topics, which channel, how long.",
    "WIN_RATE_DOWN": "Read why the recent deals were lost, and compare with what changed in price or product.",
    "TICKETS_RISING": "See which topics grew on the Operations page; they point at the journey that changed.",
    "TOPIC_SPIKE": "Find what changed in that part of the product the week the tickets started.",
    "BACKLOG_GROWING": "Check whether the team is short, or one kind of ticket is stuck.",
    "RESOLUTION_SLOWER": "See which topics take longest, and whether they wait on a person or another team.",
    "LOSS_REASON_DOMINANT": "Take the reason to whoever owns pricing or the product it names.",
    "TOPIC_NOT_MODELED": "Write it down: add it to a domain skill, so changes to it reach the business model.",
    "WRITTEN_PROMISE_NOT_KEPT": "Either change how the work is done or change the promise; customers read the document.",
    "WRITTEN_RULES_CONFLICT": "Decide which is right, fix the other document, and check which one customers see.",
    "WRITTEN_RULE_NOT_IN_CODE": "Check whether the code enforces it; if not, it depends on people remembering.",
    "PROCEDURE_NOT_WRITTEN": "Write down who handles it, how, and by when, before the person who knows is away.",
    "PULL_FAILING": "Read the error on the Operations page: usually a key that expired, lost a permission, or is not "
                    "set in the control room's environment.",
    "PULLS_MISCONFIGURED": "Fix pulls.json in the operations folder; the Operations page says what is wrong with it.",
    "PULL_STALE": "Check that the control room runs with --pulls (or the cron job runs), and that the key still works.",
}
SIGNAL_TITLES = {"DISPUTES_OPEN": "Disputes waiting for a response", "PAYOUT_FAILED": "A payout failed",
                 "REFUND_RATE_RISING": "Refund rate rising", "REVENUE_DOWN": "Payments taken fell",
                 "DISPUTE_RATE_HIGH": "Dispute rate high", "CHURN_RISING": "Subscription cancellations rising",
                 "FAILED_PAYMENTS_HIGH": "Many payments fail", "INVOICES_UNCOLLECTIBLE": "Invoices written off",
                 "SUBSCRIPTIONS_PAST_DUE": "Subscriptions past due", "PAYOUTS_NOT_IN_BANK": "Payouts missing from the bank",
                 "DEPOSITS_WITHOUT_PAYOUTS": "Deposits without payouts", "BILLS_OVERDUE": "Bills overdue",
                 "SPEND_RISING": "Spending jumped", "CLOUD_COST_RISING": "Cloud costs jumped",
                 "NEW_SUPPLIER_PAID": "A new supplier was paid", "SATISFACTION_DROPPING": "Support ratings dropping",
                 "WIN_RATE_DOWN": "Win rate down", "TICKETS_RISING": "Support tickets rising",
                 "TOPIC_SPIKE": "A kind of ticket jumped", "BACKLOG_GROWING": "Support backlog growing",
                 "RESOLUTION_SLOWER": "Tickets take longer to solve", "LOSS_REASON_DOMINANT": "Deals lost to one reason",
                 "WRITTEN_PROMISE_NOT_KEPT": "A written promise is not kept", "WRITTEN_RULES_CONFLICT": "Documents disagree",
                 "WRITTEN_RULE_NOT_IN_CODE": "A written rule is not in the code",
                 "PROCEDURE_NOT_WRITTEN": "Not written down", "PULL_FAILING": "A live pull is failing",
                 "PULLS_MISCONFIGURED": "Live pulls are misconfigured", "PULL_STALE": "A live pull is out of date"}
# What a business in each field should be able to see; each found from supplier categories or names in the model.
FIELDS = {
    "e-commerce": (r"order|cart|checkout|product|sku|inventor|catalog|coupon|fulfil|shipment|storefront", (
        ("Payments provider", ("payments",), None),
        ("Orders", (), r"order"),
        ("Inventory or stock", (), r"inventor|stock|sku"),
        ("Refunds and returns", (), r"refund|return"),
        ("Tax calculation", ("tax",), r"tax"),
        ("Shipping carriers", ("shipping and logistics",), r"shipment|shipping|carrier"),
        ("Fraud or identity checks", ("identity verification",), r"fraud|risk.?score|chargeback"),
        ("Customer email", ("email and messaging",), None))),
    "subscription software": (r"subscri|plan|trial|seat|tenant|workspace|billing|invoice|usage|organi[sz]ation", (
        ("Billing provider", ("payments",), r"billing|invoice"),
        ("Plans and subscriptions", (), r"subscri|plan"),
        ("Sign-in", ("identity and sign-in",), r"auth\b|authenticat|session|login|sign.?in"),
        ("Accounts kept apart (tenants)", (), r"tenant|workspace|organi[sz]ation|team"),
        ("Usage metering", (), r"usage|meter|quota"),
        ("Customer email", ("email and messaging",), None),
        ("Analytics or monitoring", ("analytics and monitoring",), None))),
    "marketplace": (r"seller|buyer|listing|payout|commission|merchant|booking|host|vendor", (
        ("Payouts to sellers", ("payments", "banking and finance data"), r"payout|transfer"),
        ("Seller onboarding and identity checks", ("identity verification",), r"kyc|onboard|verif"),
        ("Listings", (), r"listing|product|offer"),
        ("Disputes and refunds", (), r"dispute|refund|chargeback"),
        ("Fees and commission", (), r"fee|commission"),
        ("Messaging", ("email and messaging",), r"message|notification"))),
    "financial services": (r"ledger|account|transfer|transaction|kyc|loan|wallet|balance|card|reconcil|deposit", (
        ("Ledger", (), r"ledger|journal|entry"),
        ("Reconciliation", (), r"reconcil"),
        ("Identity checks (KYC)", ("identity verification",), r"kyc|identity"),
        ("Banking data", ("banking and finance data",), r"bank"),
        ("Audit trail", (), r"audit"),
        ("Limits and fraud checks", (), r"limit|fraud|risk"))),
    "healthcare": (r"patient|appointment|prescription|clinic|diagnos|ehr|medical|practitioner|insurance", (
        ("Patient records", (), r"patient|medical|ehr|chart"),
        ("Appointments", (), r"appointment|booking|schedule"),
        ("Access control", ("identity and sign-in",), r"role|permission|access"),
        ("Audit trail", (), r"audit"),
        ("Consent", (), r"consent"),
        ("Patient messaging", ("email and messaging",), r"notification|reminder"))),
    "logistics": (r"shipment|delivery|fleet|route|warehouse|driver|tracking|dispatch|parcel|carrier", (
        ("Shipments", (), r"shipment|parcel|delivery"),
        ("Tracking", (), r"track"),
        ("Carriers", ("shipping and logistics",), r"carrier"),
        ("Routes and dispatch", (), r"route|dispatch"),
        ("Warehouses", (), r"warehouse|inventor"),
        ("Customer notifications", ("email and messaging",), r"notif"))),
}


# Words every codebase uses for its own plumbing; "createUserRoute" says nothing about logistics.
PLUMBING = {"route", "routes", "router", "handler", "handlers", "controller", "service", "services", "api", "rpc",
            "get", "post", "put", "patch", "delete", "create", "update", "find", "list", "admin", "index", "page",
            "schema", "model", "table", "type", "types", "id", "by", "the", "for"}


def _words(text: str) -> str:
    """An identifier or label as plain lowercase words: createAdminOrderRoute -> 'admin order'."""
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return " ".join(w for w in re.split(r"[^A-Za-z0-9]+", split.lower()) if w and w not in PLUMBING)


def _mentions(pattern: str | None, text: str) -> bool:
    return bool(pattern) and re.search(r"\b(?:" + pattern + ")", text) is not None


def _level(score: int) -> str:
    return next(name for floor, name in LEVELS if score >= floor)


def _item(kind: str, subject: dict | None, title: str, why: str, reach: list[dict], extra: list[dict] = (),
          standing: str = "SOURCE", links: list[str] = (), evidence: list[dict] = ()) -> dict:
    """One ranked item with the factors its score is made of."""
    points, reason = SEVERITY[kind]
    business = [r for r in reach if r.get("layer") == "business"]
    factors = [{"factor": "severity", "points": points, "detail": reason}]
    if business:
        factors.append({"factor": "reach", "points": min(25, 8 * len(business)),
                        "detail": f"{len(business)} business object(s) or action(s) reach it: "
                                  + ", ".join(sorted({r['label'] for r in business})[:5])})
    texts = [r.get("label", "") for r in business] + [title]
    if any(MONEY.search(t or "") for t in texts):
        factors.append({"factor": "money", "points": 15, "detail": "money moves through it"})
    factors.extend(extra)
    score = min(100, sum(f["points"] for f in factors))
    key = digest(canonical([kind, subject["id"] if subject else title]))[:16]
    return {"id": key, "kind": kind, "category": CATEGORY[kind], "level": _level(score), "score": score,
            "title": title, "why": why, "subject": subject, "standing": standing, "factors": factors,
            "next_step": NEXT_STEP[kind], "links": list(dict.fromkeys([*(subject and [subject["id"]] or []), *links]))[:10],
            "evidence": list(evidence)}


# ---------------------------------------------------------------- evidence on every finding
#
# Each finding names what it rests on: records of what was observed, read or decided, where from, when, and
# whether that is still current. `role` says whether a record is the basis of the conclusion or only context
# (what the finding is about). A conclusion whose basis is all expired or missing is not a conclusion: it reads
# UNKNOWN. `contradicted_by` says what would show the finding wrong.

EVIDENCE_STATES = ("COMPLETE", "PARTIAL", "STALE", "MISSING")
HEALTH_KINDS = {"SYSTEM_ERROR", "AT_RISK", "POSSIBLY_IMPACTED", "DEGRADED", "STALE", "NEVER_OBSERVED"}
CONTRADICTS = {
    "SYSTEM_ERROR": "a fresh ok sample from {subject} newer than {newest}",
    "AT_RISK": "fresh ok samples from the systems it runs on, or a person undoing the link",
    "POSSIBLY_IMPACTED": "fresh ok samples from the failing systems it depends on, or the dependency undone",
    "DEGRADED": "fresh ok samples from the systems it runs on",
    "STALE": "a fresh sample from the systems it runs on",
    "NEVER_OBSERVED": "any health sample from {subject}",
    "UNOBSERVED_SERVICES": "a health sample from each silent service",
    "NOT_LINKED": "a confirmed link from this action to a system",
    "STALE_CONFIRMATION": "the cited files as they were, or a person re-confirming it",
    "STALE_REPAIR": "the skill rebuilt from the current code, or the lines restored",
    "DECISIONS_WAITING": "a person confirming or rejecting each proposal",
    "OPEN_QUESTION": "the question answered in the skill's sources",
    "LINK_GAP": "the component back in the repository, or the link undone",
    "NO_BUSINESS_MODEL": "a domain skill for the main workflow",
    "NO_OWNER": "a CODEOWNERS rule covering its code, or a commit in the last year",
    "ONE_PERSON": "a second person changing its code",
    "ONE_PERSON_REPOSITORY": "commits from a second person",
    "NO_CODEOWNERS": "a CODEOWNERS file",
    "SINGLE_CRITICAL_SUPPLIER": "a second supplier of the kind declared in the sources",
    "SINGLE_HIGH_SUPPLIER": "a second supplier of the kind declared in the sources",
    "AGENT_BLOCKED": "the rule shown wrong for that call, by a person",
    "CARD_FIELDS": "the fields shown to hold no card data",
}
CATEGORY_CONTRADICTS = {
    "Operations": "the next complete week's records back within the usual range",
    "Spend": "the next month's records back within the usual range",
    "Customers": "the next complete week's tickets and deals back within the usual range",
    "Written": "the documents or the records changed so they agree",
    "Compliance": "evidence for the controls in the sources, or a person providing it",
}


def _age(collected: str | None, now: datetime | None) -> int | None:
    if not collected or now is None:
        return None
    try:
        return max(0, int((now - datetime.fromisoformat(collected.replace("Z", "+00:00"))).total_seconds()))
    except ValueError:
        return None


def _lines(e: dict) -> str:
    """path:start-end of a cited span (path alone when the lines are not given)."""
    return (f"{e.get('path')}:{e['start_line']}-{e.get('end_line', e['start_line'])}" if e.get("start_line")
            else str(e.get("path")))


def record(kind: str, ref: str, state: str, detail: str, role: str = "basis", source: str | None = None,
           component: str | None = None, collected_at: str | None = None, now: datetime | None = None,
           environment: str | None = None) -> dict:
    """One piece of evidence: what it is, where it came from, when, and whether it is still current."""
    return {"kind": kind, "ref": ref, "source": source, "component": component, "environment": environment,
            "collected_at": collected_at, "age_seconds": _age(collected_at, now), "state": state, "role": role,
            "detail": detail}


def _observations(node: dict, now: datetime | None, environment: str | None, role: str = "basis") -> list[dict]:
    """A system's health samples as evidence: one per source, current or expired; or the absence of any."""
    out = []
    for sample in node.get("observation_sources", []):
        out.append(record("observation", f"{node['id']}@{sample['observed_at']}", "COMPLETE" if sample["fresh"] else "STALE",
                          f"{sample['source']} reported {sample['status']}", role, sample["source"], node["id"],
                          sample["observed_at"], now, environment))
    if not out:
        out.append(record("absence", node["id"], "MISSING", f"nothing reports the health of {node['label']}", role,
                          component=node["id"], environment=environment))
    return out


def attach_evidence(items: list[dict], bundle: dict, now: datetime | None) -> None:
    """Fill each finding's evidence from what it is about, mark what would contradict it, and demote a
    conclusion whose basis is all expired or missing to UNKNOWN."""
    state = bundle.get("state") or {}
    environment = state.get("environment")
    nodes = {n["id"]: n for n in state.get("nodes", [])}
    files = {e["id"]: e for e in state.get("source_evidence", [])}
    scanned = state.get("discovery_scanned_at") or state.get("generated_at")
    picture = bundle.get("business") or {}
    rows = {r["key"]: r for r in [*picture.get("actions", []), *picture.get("objects", [])]}
    stale_items = {i["id"] for i in (bundle.get("drift") or {}).get("items", []) if i["state"] == "STALE_REPAIR"}
    for item in items:
        evidence = list(item.get("evidence") or [])
        if not evidence:
            for key in [*([item["subject"]["id"]] if item.get("subject") else []), *item.get("links", [])]:
                node = nodes.get(key)
                if node is not None:
                    role = "basis" if item["kind"] in HEALTH_KINDS else "context"
                    if item["kind"] in HEALTH_KINDS:
                        evidence += _observations(node, now, environment, role)
                    declared = files.get(node.get("evidence_id"))
                    if declared:
                        evidence.append(record("file", declared["path"] + (f":{node['source_line']}" if node.get("source_line")
                                                                           else ""), "COMPLETE",
                                               f"declares {node['label']} (sha256 {declared['sha256'][:12]})",
                                               "context" if item["kind"] in HEALTH_KINDS else "basis", "repository",
                                               key, scanned, now))
                row = rows.get(key)
                if row is not None:
                    for e in row.get("evidence", [])[:3]:
                        evidence.append(record("file", _lines(e),
                                               "STALE" if key in stale_items else "COMPLETE",
                                               f"skill {row.get('skill')} cites it for {row['id']}",
                                               "context" if item["kind"] in HEALTH_KINDS else "basis",
                                               f"skill {row.get('skill')}", key))
        unique: dict[tuple, dict] = {}
        for e in evidence:  # the subject is also among the links: one record each
            unique.setdefault((e["kind"], e["ref"], e["role"]), e)
        item["evidence"] = list(unique.values())[:12]
        basis = [e for e in item["evidence"] if e["role"] == "basis"]
        newest = max((e["collected_at"] for e in basis if e.get("collected_at")), default="the last sample")
        template = CONTRADICTS.get(item["kind"]) or CATEGORY_CONTRADICTS.get(item["category"])
        item["contradicted_by"] = template.format(subject=(item.get("subject") or {}).get("label") or "it",
                                                  newest=newest) if template else None
        if item["standing"] in ("SOURCE", "INFERRED") and not any(e["state"] in ("COMPLETE", "PARTIAL") for e in basis):
            item["standing"] = "UNKNOWN"
            item["unknown_because"] = ("its evidence has expired" if any(e["state"] == "STALE" for e in basis)
                                       else "nothing it rests on is held")


def report(bundle: dict) -> dict:
    """The ranked attention list, coverage, and field profile for one model."""
    state, picture = bundle["state"], bundle.get("business") or {}
    entities = ec._entities(bundle)
    relationships = ec._relationships(bundle, entities, ec._history(bundle))
    reach_cache: dict[str, list[dict]] = {}

    def reach(entity_id: str) -> list[dict]:
        if entity_id not in reach_cache:
            rows = ec._impact(entity_id, relationships, entities) if entity_id in entities else []
            own = [ec._side(entities, entity_id)] if entities.get(entity_id, {}).get("layer") == "business" else []
            reach_cache[entity_id] = own + rows
        return reach_cache[entity_id]
    system_reach = reach  # `reach` is reused below for what the records reach

    def side(entity_id):
        return ec._side(entities, entity_id) if entity_id in entities else None

    items = []
    for row in [*picture.get("actions", []), *picture.get("objects", [])]:
        health = row.get("health")
        if health in ("AT_RISK", "POSSIBLY_IMPACTED", "DEGRADED", "STALE"):
            items.append(_item(health, side(row["key"]), f"{row['id']} is {health.replace('_', ' ').lower()}",
                               row.get("health_reason") or "", reach(row["key"]), links=row.get("systems", [])))
        elif health == "UNLINKED" and "action" in row["key"]:
            items.append(_item("NOT_LINKED", side(row["key"]), f"Nobody can see the systems behind {row['id']}",
                               "No confirmed link ties this action to a system, so its health is unknown.",
                               reach(row["key"]), standing="UNKNOWN",
                               evidence=[record("absence", row["key"], "COMPLETE", "no confirmed link names this action",
                                                source="links", component=row["key"])]))
        for blocker in row.get("blockers", []):
            if blocker.get("type") in ("unknown", "conflict"):
                cited = next((x.get("evidence", []) for x in [*row.get("unknowns", []), *row.get("conflicts", [])]
                              if x.get("id") == blocker.get("id")), [])
                items.append(_item("OPEN_QUESTION", side(row["key"]), f"Open question on {row['id']}",
                                   blocker.get("statement") or "", reach(row["key"]), standing="UNKNOWN",
                                   evidence=[record("file", _lines(e), "COMPLETE",
                                                    f"the skill records the question here ({blocker.get('type')})",
                                                    source=f"skill {row.get('skill')}", component=row["key"])
                                             for e in cited[:3]]))
    for node in state.get("nodes", []):
        relied = [r for r in reach(node["id"]) if r.get("layer") == "business"]
        if node.get("status") == "error" and relied:
            items.append(_item("SYSTEM_ERROR", side(node["id"]), f"{node['label']} reports an error",
                               f"{node['label']} is in error and {len(relied)} business item(s) rely on it.",
                               reach(node["id"])))
        elif node.get("status") == "unknown" and relied and node.get("kind") != "external_supplier":
            items.append(_item("NEVER_OBSERVED", side(node["id"]), f"No health signal from {node['label']}",
                               f"{len(relied)} business item(s) rely on {node['label']}, and nothing reports its "
                               "health.", reach(node["id"]), standing="UNKNOWN"))
    for mark in picture.get("link_watermarks", []):
        check = ec.evidence_check(mark.get("recorded"), mark.get("now"))
        if check["status"] == "CHANGED_SINCE_DECISION":
            items.append(_item("STALE_CONFIRMATION", side(mark.get("component")),
                               f"Re-confirm {mark.get('binding')} runs on {(side(mark.get('component')) or {}).get('label')}",
                               "The files this confirmation rested on changed: " + ", ".join(check["changed"]),
                               reach(mark.get("component")), standing="INFERRED",
                               evidence=[record("file", path, "COMPLETE", "read now; it differs from what was recorded "
                                                "when the link was confirmed", source="repository")
                                         for path in check["changed"][:6]]
                               + [record("decision", str(mark.get("id")), "COMPLETE",
                                         f"a person confirmed {mark.get('binding')} runs on {mark.get('component')}",
                                         "context", "links")]))
    for rule in picture.get("rule_decided", []):
        if rule.get("status") == "CONFIRMED_BEFORE_EVIDENCE_CHANGED":
            items.append(_item("STALE_CONFIRMATION", side(rule.get("target")), f"Re-confirm the rule: {rule.get('statement')}",
                               f"The lines it cites ({rule['citation']['path']}) changed since it was confirmed.",
                               reach(rule.get("target")), standing="INFERRED",
                               evidence=[record("file", f"{rule['citation']['path']}:{rule['citation']['start_line']}-"
                                                f"{rule['citation']['end_line']}", "COMPLETE",
                                                "read now; the lines differ from the ones confirmed", source="repository"),
                                         record("decision", str(rule.get("id")), "COMPLETE", "a person confirmed the rule",
                                                "context", "rules")]))
    spans = {(x["skill"], x["evidence"]): x for x in (bundle.get("drift") or {}).get("spans", [])}
    for item in (bundle.get("drift") or {}).get("items", []):  # business items whose cited code changed (drift.py)
        if item["state"] == "STALE_REPAIR":
            where = [spans[(item["skill"], e)] for e in item["changed"] if (item["skill"], e) in spans]
            items.append(_item("STALE_REPAIR", side(item["id"]), f"{item['label']} was modeled from code that changed",
                               "Its cited lines are " + "; ".join(
                                   f"{x['status'].lower().replace('_', ' ')} at {x['path']}:{x['start_line']}-{x['end_line']}"
                                   for x in where[:3]) + ".", reach(item["id"]), standing="INFERRED",
                               evidence=[record("file", f"{x['path']}:{x.get('start_line') or x['from'][0]}", "COMPLETE",
                                                f"read now: {x['status'].lower().replace('_', ' ')} since the skill was built",
                                                source="repository", component=item["id"]) for x in where[:6]]))
    for hit in (bundle.get("guardrails") or {}).get("denied_by_rule", []):  # guardrail.py, the last 7 days
        subject = side(f"{hit['skill']}:action:{hit['task']}")
        items.append(_item("AGENT_BLOCKED", subject, f"An agent was stopped {hit['denied']} time(s) by {hit['task']}'s "
                           f"rule {hit['rule']}", f"The guard denied {', '.join(hit['tools'])} because the call broke "
                           f"the rule {hit['rule']}.", reach(subject["id"]) if subject else [],
                           evidence=[record("record", ".ontologyex/guardrail-audit.jsonl", "COMPLETE",
                                            f"{hit['denied']} denial(s) of {', '.join(hit['tools'])} in the last "
                                            f"{(bundle.get('guardrails') or {}).get('days', 7)} days", source="guardrail audit")]))
    for gap in picture.get("link_gaps", []):
        link = gap.get("link") or {}
        items.append(_item("LINK_GAP", None, f"{link.get('binding')} is linked to a component that is gone",
                           f"{link.get('component')}: {gap.get('reason')}", [], standing="UNKNOWN",
                           evidence=[record("decision", f"{link.get('skill')}/{link.get('binding')} -> {link.get('component')}",
                                            "COMPLETE", f"declared by {link.get('declared_by')}", "context", "links"),
                                     record("absence", str(link.get("component")), "COMPLETE", str(gap.get("reason")),
                                            source="repository")]))
    supplier_report = bundle.get("suppliers") or {}
    for category in supplier_report.get("categories", []):
        if category.get("single_supplier"):
            kind = "SINGLE_CRITICAL_SUPPLIER" if category["criticality"] == "CRITICAL" else "SINGLE_HIGH_SUPPLIER"
            name = category["suppliers"][0]
            supplier = next((s for s in supplier_report.get("suppliers", []) if s["name"] == name), {})
            items.append(_item(kind, side(supplier.get("id")), f"{name} is the only {category['category']} supplier",
                               f"No second {category['category']} supplier is declared in the sources.",
                               reach(supplier.get("id")) if supplier.get("id") else [],
                               evidence=[record("file", f"{e.get('path')}" + (f":{e['line']}" if e.get("line") else ""),
                                                "COMPLETE", f"{name}: {e.get('kind', 'declared')} {e.get('text') or ''}".strip(),
                                                source="repository", component=supplier.get("id"))
                                         for e in supplier.get("evidence", [])[:3] if e.get("path")]
                               + [record("absence", category["category"], "COMPLETE",
                                         f"no second {category['category']} supplier in the sources", source="repository")]))
    waiting = {"link": len((picture.get("link_suggestions") or {}).get("suggestions", [])),
               "dependency": len((picture.get("dependency_suggestions") or {}).get("suggestions", [])),
               "rule": len((picture.get("rule_suggestions") or {}).get("suggestions", []))}
    for kind, count in waiting.items():
        if count:
            items.append(_item("DECISIONS_WAITING", None, f"{count} {kind} proposal(s) wait for a person",
                               f"Suggested {'dependencies' if kind == 'dependency' else kind + 's'} change nothing until "
                               "someone confirms or rejects them.", [],
                               extra=[{"factor": "volume", "points": min(10, count), "detail": f"{count} waiting"}],
                               standing="INFERRED",
                               evidence=[record("decision", f"{count} {kind} proposal(s)", "COMPLETE",
                                                "suggested, and not yet confirmed or rejected by anyone",
                                                source={"link": "link suggestions", "dependency": "observed calls",
                                                        "rule": "agent rule proposals"}[kind])]))
    if not picture.get("actions"):
        items.append(_item("NO_BUSINESS_MODEL", None, "The business actions are not written down",
                           "Without them, systems, suppliers and changes cannot be traced to what the business does "
                           "or what it earns from.", [], standing="UNKNOWN",
                           evidence=[record("absence", "domain skills", "COMPLETE", "no domain skill is loaded",
                                            source="control room")]))
    services = [n for n in state.get("nodes", []) if n.get("kind") == "declared_service"]
    silent = [n for n in services if not n.get("observed_at")]
    if silent:
        items.append(_item("UNOBSERVED_SERVICES", None, f"{len(silent)} of {len(services)} services report no health",
                           "Nothing reports on " + ", ".join(sorted({n["label"] for n in silent})[:8])
                           + (", ..." if len({n["label"] for n in silent}) > 8 else "") + ".", [], standing="UNKNOWN",
                           evidence=[record("absence", n["id"], "MISSING", f"nothing reports the health of {n['label']}",
                                            component=n["id"], environment=state.get("environment")) for n in silent[:8]],
                           extra=[{"factor": "volume", "points": min(10, len(silent)),
                                   "detail": f"{len(silent)} silent service(s)"}]))
    found = bundle.get("ownership") or {}
    owners = ownership.owners_by_id(found)
    if found.get("status") == "OK":
        single = found.get("git") == "OK" and found.get("contributors") == 1
        for row in found["entities"]:
            if row["layer"] != "business" or "action" not in row["id"]:
                continue
            if row["status"] == "NO_OWNER":
                items.append(_item("NO_OWNER", side(row["id"]), f"Nobody owns {row['label']}",
                                   "No CODEOWNERS rule covers its code, and nobody changed it in the last year.",
                                   reach(row["id"]), standing="UNKNOWN",
                                   evidence=[record("absence", row["id"], "COMPLETE", "no CODEOWNERS rule and no commit "
                                                    "in a year cover its code", source="CODEOWNERS and git history")]))
            elif "ONE_PERSON" in row["risks"] and not single:
                items.append(_item("ONE_PERSON", side(row["id"]), f"Only {row['active'][0]['name']} knows "
                                   f"{row['label']}", f"Only {row['active'][0]['name']} changed its code in the "
                                   "last year.", reach(row["id"]), standing="INFERRED",
                                   evidence=[record("record", "git history, 12 months", "COMPLETE",
                                                    f"{row['active'][0].get('commits', '?')} commit(s), all by "
                                                    f"{row['active'][0]['name']}", source="git")]))
        if single:
            items.append(_item("ONE_PERSON_REPOSITORY", None, "One person changed everything this year",
                               "Every commit in the last year came from one person, so what they know is "
                               "written down nowhere else.", [], standing="INFERRED",
                               evidence=[record("record", "git history, 12 months", "COMPLETE",
                                                "every commit came from one person", source="git")]))
        if found.get("codeowners") is None and found["entities"]:
            items.append(_item("NO_CODEOWNERS", None, "No CODEOWNERS file",
                               "Ownership can only be inferred from who changed the code.", [], standing="UNKNOWN",
                               evidence=[record("absence", "CODEOWNERS", "COMPLETE", "no CODEOWNERS file in the repository",
                                                source="repository")]))
    for pack in (bundle.get("compliance") or {}).get("packs", []):
        missing = [c for c in pack["controls"] if c["status"] in ("NOT_SHOWN_IN_SOURCES", "PARTLY_SHOWN")]
        if missing and pack["applies"] in ("APPLIES", "ASKED_BY_CUSTOMERS"):
            kind = "COMPLIANCE_GAPS" if pack["applies"] == "APPLIES" else "COMPLIANCE_ASKED"
            items.append(_item(kind, None, f"{pack['name']}: {len(missing)} of {len(pack['controls'])} controls not "
                               "fully shown in the sources", pack["why"] + " Not fully shown: "
                               + ", ".join(f"{c['id']} {c['title'].lower()}" for c in missing[:6])
                               + ("..." if len(missing) > 6 else "") + ".", [], standing="UNKNOWN",
                               extra=[{"factor": "volume", "points": min(12, 2 * len(missing)),
                                       "detail": f"{len(missing)} control(s)"}],
                               evidence=[record("absence", c["id"], "COMPLETE", f"{c['title']}: not fully shown in the sources",
                                                source=pack["name"]) for c in missing[:6]]))
        for risk in pack.get("risks", []):
            if risk["risk"] == "CARD_FIELDS_IN_SCHEMA":
                items.append(_item("CARD_FIELDS", None, "Card number fields in the data model", risk["detail"] + " "
                                   + ", ".join(f"{e['path']}:{e['line']}" for e in risk["evidence"]), [],
                                   standing="SOURCE",
                                   evidence=[record("file", f"{e['path']}:{e['line']}", "COMPLETE", "a field named like card "
                                                    "data", source="repository") for e in risk["evidence"][:3]]))
    ops = bundle.get("operations") or {}
    ledger_sources = [record("record", s["file"], "COMPLETE", f"{s.get('stream') or 'records'}, sha256 "
                             f"{(s.get('sha256') or '')[:12]}", "context", "operations folder")
                      for s in ops.get("sources", []) if s.get("status") == "READ"][:3]

    def ledger(name: str, detail: str) -> list[dict]:
        """A finding from the business's own records: the aggregate it rests on, and the files read."""
        stale = any(x["signal"] in ("PULL_STALE", "PULL_FAILING") for x in ops.get("signals", []))
        return [record("record", name, "PARTIAL" if stale else "COMPLETE", detail, source="operations ledger (aggregates)",
                       collected_at=None)] + ledger_sources
    reach = {f["stream"]: [{**e, "layer": "business"} for e in f.get("entities", [])]
             for f in ops.get("code_vs_operations", []) if f["finding"] == "SEEN_AND_MODELED"}
    for signal in ops.get("signals", []):  # from the business's own records, as aggregates
        title = SIGNAL_TITLES[signal["signal"]] + (f": {signal['topic']}" if signal.get("topic") else "")
        items.append(_item(signal["signal"], None, title + (f" ({signal['currency'].upper()})" if signal.get("currency") else ""),
                           signal["detail"][:1].upper() + signal["detail"][1:] + ".", reach.get(signal["stream"], []),
                           standing="SOURCE", links=[e["id"] for e in reach.get(signal["stream"], [])],
                           evidence=ledger(f"{signal['signal']} {signal['stream']}" + (f" {signal['currency']}"
                                                                                      if signal.get("currency") else ""),
                                           signal["detail"])))
    for finding in (ops.get("spend") or {}).get("suppliers_vs_spend", []):  # suppliers paid against suppliers used
        if finding["finding"] in ("PAID_NOT_IN_CODE", "IN_CODE_NOT_PAID"):
            items.append(_item(finding["finding"], None,
                               (f"{finding['supplier']} is paid, and the code does not use it"
                                if finding["finding"] == "PAID_NOT_IN_CODE"
                                else f"{finding['supplier']} is used, and nobody is seen paying it"),
                               finding["detail"][:1].upper() + finding["detail"][1:] + ".", [], standing="INFERRED",
                               evidence=ledger(f"{finding['finding']} {finding['supplier']}", finding["detail"])))
    refund_items = [{**e, "layer": "business"} for e in reach.get("refunds", [])]
    for finding in (ops.get("customers") or {}).get("joined", []):  # what tickets and payment records say together
        if finding["finding"] == "REFUNDS_CONFIRMED_BY_TICKETS":
            items.append(_item(finding["finding"], None, f"Refunds and refund tickets rose together ({finding['currency'].upper()})",
                               finding["detail"][:1].upper() + finding["detail"][1:] + ".", refund_items,
                               links=[e["id"] for e in refund_items],
                               evidence=ledger(f"{finding['finding']} {finding['currency']}", finding["detail"])))
        elif finding["finding"] == "TOPIC_NOT_MODELED":
            items.append(_item(finding["finding"], None, f"Customers raise {finding['topic']!r}; the model does not name it",
                               finding["detail"][:1].upper() + finding["detail"][1:] + ".", [], standing="INFERRED",
                               evidence=ledger(f"{finding['finding']} {finding['topic']}", finding["detail"])))
    for finding in ops.get("code_vs_operations", []):
        if finding["finding"] in ("HAPPENS_NOT_IN_CODE", "IN_CODE_NOT_SEEN"):
            what = finding["stream"]
            items.append(_item(finding["finding"], None,
                               (f"{what.capitalize()} happen, and the sources do not show where"
                                if finding["finding"] == "HAPPENS_NOT_IN_CODE"
                                else f"{what.capitalize()} are built, and the records show none"),
                               finding["detail"][:1].upper() + finding["detail"][1:] + ".",
                               [{**e, "layer": "business"} for e in finding.get("entities", [])], standing="INFERRED",
                               evidence=ledger(f"{finding['finding']} {what}", finding["detail"])))
    for item in items:
        if item["subject"] and item["subject"]["id"] in owners:
            item["owner"] = owners[item["subject"]["id"]]
        else:
            item["owner"] = None
    generated = state.get("generated_at")
    attach_evidence(items, bundle, datetime.fromisoformat(generated.replace("Z", "+00:00")) if generated else None)
    unique = {}
    for item in items:  # one item per kind and subject
        unique.setdefault(item["id"], item)
    ranked = sorted(unique.values(), key=lambda i: (-i["score"], i["category"], i["title"]))
    return {"schema_version": 1, "kind": "COMMAND_CENTER", "items": ranked[:500], "total": len(ranked),
            "by_level": {name: sum(i["level"] == name for i in ranked) for _, name in LEVELS},
            "kpis": kpis(bundle, waiting), "coverage": coverage(bundle, entities), "field": field_profile(bundle, entities),
            "verdict": verdict(bundle, system_reach, ranked),
            "semantics": "IMPORTANCE_RANKS_ATTENTION_NOT_PROBABILITY_OR_APPROVAL", "execution_authorized": False}


WATCHED_KINDS = ("declared_service", "service")  # systems whose health is reported (feeds, traces)


def verdict(bundle: dict, reach, ranked: list[dict]) -> dict:
    """The systems' state as the evidence shows it: FAULT, DEGRADED, HEALTHY or INCONCLUSIVE.

    Watched: every system that ever reported health, and every system a business item relies on. Missing or
    expired evidence never reads as healthy: with no fault seen and some watched system silent, the answer is
    INCONCLUSIVE, with the silent systems named. Causes: systems in error, those with no failing system upstream
    first, in the order the ranking puts them."""
    nodes = bundle["state"].get("nodes", [])
    watched = [n for n in nodes if n.get("kind") != "external_supplier" and (
        n.get("observed_at") is not None or n.get("kind") in WATCHED_KINDS
        or any(r.get("layer") == "business" for r in reach(n["id"])))]
    status = {n["id"]: n.get("status") for n in watched}
    fresh = [n for n in watched if n.get("status") in ("ok", "warning", "error")]
    missing = [n["id"] for n in watched if n.get("status") in ("stale", "unknown")]
    order = {}
    for i, item in enumerate(ranked):
        for key in [item["subject"]["id"]] if item.get("subject") else []:
            order.setdefault(key, i)
        for key in item.get("links", []):
            order.setdefault(key, i)

    def ranked_ids(ids: list[str]) -> list[str]:
        return sorted(ids, key=lambda i: (order.get(i, len(ranked)), i))
    errors = [n for n in watched if n.get("status") == "error"]
    roots = [n["id"] for n in errors if not set(n.get("potentially_impacted_by", [])) - {n["id"]}]
    if errors:
        result, causes = "FAULT", ranked_ids(roots) + ranked_ids([n["id"] for n in errors if n["id"] not in roots])
    elif any(n.get("status") == "warning" for n in watched):
        result, causes = "DEGRADED", ranked_ids([n["id"] for n in watched if n.get("status") == "warning"])
    elif not watched or missing:
        result, causes = "INCONCLUSIVE", []
    else:
        result, causes = "HEALTHY", []
    share = len(fresh) / len(watched) if watched else 0.0
    basis = ("no system reports health, and no business item relies on one" if not watched else
             f"{len(fresh)} of {len(watched)} watched system(s) have fresh evidence"
             + (f"; silent or expired: {', '.join(n['label'] for n in watched if n['id'] in missing[:5])}"
                if missing else ""))
    return {"status": result, "causes": causes[:20], "evidence": {"fresh": len(fresh), "watched": len(watched),
            "share": round(share, 3), "missing": missing[:50]},
            "confidence": "HIGH" if watched and not missing else "PARTIAL" if fresh else "NONE", "basis": basis,
            "semantics": "SOURCE_REPORTED_HEALTH_MISSING_EVIDENCE_IS_NEVER_HEALTHY"}


def kpis(bundle: dict, waiting: dict) -> list[dict]:
    """The headline counts: what works, what is at risk, what nobody can see, what waits for a person."""
    state, picture = bundle["state"], bundle.get("business") or {}
    actions = picture.get("actions", [])
    health = [a.get("health") for a in actions]
    systems = [n for n in state.get("nodes", []) if n.get("kind") == "declared_service"]
    return [
        {"label": "Business actions", "value": len(actions), "tone": "neutral"},
        {"label": "Reported OK", "value": health.count("REPORTED_OK"), "tone": "ok"},
        {"label": "At risk or impacted", "value": sum(h in ("AT_RISK", "POSSIBLY_IMPACTED", "DEGRADED", "STALE")
                                                     for h in health), "tone": "risk"},
        {"label": "Health unknown", "value": sum(h in ("UNKNOWN", "UNLINKED") for h in health), "tone": "unknown"},
        {"label": "Services in error", "value": sum(n.get("status") == "error" for n in systems), "tone": "risk"},
        {"label": "Suppliers", "value": len((bundle.get("suppliers") or {}).get("suppliers", [])), "tone": "neutral"},
        {"label": "Decisions waiting", "value": sum(waiting.values()), "tone": "unknown"},
    ]


def coverage(bundle: dict, entities: dict) -> dict:
    """How much of the business can be seen: linked, observed, confirmed and current, reviewed."""
    state, picture = bundle["state"], bundle.get("business") or {}
    actions = picture.get("actions", [])
    services = [n for n in state.get("nodes", []) if n.get("kind") == "declared_service"]
    marks = [m for m in picture.get("link_watermarks", []) if isinstance(m.get("recorded"), dict)]
    current = sum(ec.evidence_check(m["recorded"], m.get("now"))["status"] == "CURRENT" for m in marks)
    summary = picture.get("summary") or {}

    def ratio(part, whole, label, detail):
        return {"label": label, "part": part, "whole": whole,
                "percent": round(100 * part / whole) if whole else None, "detail": detail}
    return {"rows": [
        ratio(sum(a.get("health") != "UNLINKED" for a in actions), len(actions), "Business actions linked to a system",
              "An action with no confirmed system cannot show its health."),
        ratio(sum(bool(n.get("observed_at")) for n in services), len(services), "Services reporting health",
              "Services with a live observation (health checks, traces, metrics)."),
        ratio(current, len(marks), "Confirmations still current",
              "Confirmations whose recorded evidence is unchanged."),
        ratio(summary.get("rules_reviewed_or_better", 0), summary.get("rules", 0), "Business rules reviewed",
              "Rules whose evidence a person reviewed or better."),
    ] + (bundle.get("ownership") or {}).get("coverage", [])[1:],  # business actions with any owner
        "suppliers": len((bundle.get("suppliers") or {}).get("suppliers", []))}


def field_profile(bundle: dict, entities: dict) -> dict:
    """Which field the business is in, from the names in its model, and what such a business should see."""
    names = {e["id"]: _words(f"{e['label']} {e.get('summary', '')}") for e in entities.values()
             if e["layer"] == "business" or e["kind"] in ("declared_db_table", "api_route", "declared_service")}
    categories = {s["category"]: s["name"] for s in (bundle.get("suppliers") or {}).get("suppliers", [])}
    scores = {}
    for field, (pattern, _) in FIELDS.items():
        hits = sorted(i for i, text in names.items() if _mentions(pattern, text))
        scores[field] = hits
    field, hits = max(scores.items(), key=lambda kv: (len(kv[1]), kv[0]))
    if len(hits) < 3:
        return {"field": None, "signals": [], "checklist": [],
                "detail": "Not enough names in the model to tell the field. Add a domain skill, or link more systems."}
    checklist = []
    for label, supplier_kinds, pattern in FIELDS[field][1]:
        suppliers = [categories[k] for k in supplier_kinds if k in categories]
        found = sorted(i for i, text in names.items() if _mentions(pattern, text))[:5]
        checklist.append({"item": label, "status": "FOUND" if suppliers or found else "NOT_FOUND_IN_SOURCES",
                          "suppliers": suppliers, "entities": [ec._side(entities, i) for i in found]})
    return {"field": field, "signals": [ec._side(entities, i) for i in hits[:8]], "checklist": checklist,
            "detail": f"{len(hits)} names in the model point to {field}. 'Not found' means the sources do not "
                      "show it, not that the business lacks it."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="a bundle from control_room.py --bundle-only")
    args = parser.parse_args()
    result = report(json.loads(args.bundle.read_text("utf-8")))
    for item in result["items"][:30]:
        print(f"{item['score']:>3} {item['level']:<8} {item['category']:<17} {item['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
