#!/usr/bin/env python3
"""One-command local demo of the business operating picture. Synthetic data only.

Builds a fictional business repository, compiles the pre-authored payments domain
skill, declares binding-to-service links, writes synthetic health samples and
synthetic grade events, then (with --serve) opens the read-only control room.
No LLM, network, real payment system, or production observation is involved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
