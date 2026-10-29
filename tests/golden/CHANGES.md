# Golden fixture changes

Each entry is one `python tests/golden.py update --reason "..."`: the date, why the
expected outputs changed, and which files. A change to what OntologyEX answers lands
here with its reason, or CI fails.

## 2026-10-29: First golden fixtures: the demo, a shop and a clinic, with their expected outputs.

- `clinic/change_impact.json`
- `clinic/command_center.json`
- `clinic/compliance.json`
- `clinic/context.json`
- `clinic/discovery.json`
- `clinic/ownership.json`
- `clinic/suppliers.json`
- `demo/change_impact.json`
- `demo/command_center.json`
- `demo/context.json`
- `demo/digest.json`
- `shop/change_impact.json`
- `shop/command_center.json`
- `shop/compliance.json`
- `shop/context.json`
- `shop/discovery.json`
- `shop/ownership.json`
- `shop/suppliers.json`

## 2026-10-29: Operations: the demo's synthetic payment-processor exports, read as weekly aggregates against the fixed clock, add a payout failure, open disputes, rising refunds, a high dispute rate and written-off invoices to the command center and digest, and pin the operations ledger itself.

- `demo/command_center.json`
- `demo/digest.json`
- `demo/operations.json`

## 2026-10-29: Spend: the demo's bank statement, vendor bills and AWS cost report, read as monthly aggregates, add payouts missing from the bank, overdue bills, a spending jump, rising cloud costs and a first payment to OpenAI; its web app now declares SendGrid and Sentry, so suppliers paid are compared with suppliers used.

- `demo/command_center.json`
- `demo/digest.json`
- `demo/operations.json`

## 2026-10-29: Customers: the demo's helpdesk and CRM exports, read as weekly aggregates, add rising tickets, a growing backlog, slower solving, worse ratings, a CSV-export spike nothing in the model names, a falling win rate lost mostly to price, and refunds confirmed by refund tickets; the operations ledger gains a customers section.

- `demo/command_center.json`
- `demo/digest.json`
- `demo/operations.json`

## 2026-10-29: Written operations: the demo's refund policy, terms of service (Word), support page, finance policy and a supplier order form, read for their commitments and held to the records: an unkept support promise, overdue bills against the payment terms, a refund window written as 14 days in the terms and 30 in the policy, and no written procedure for disputes or payouts.

- `demo/command_center.json`
- `demo/digest.json`
- `demo/operations.json`
