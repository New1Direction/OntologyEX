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
