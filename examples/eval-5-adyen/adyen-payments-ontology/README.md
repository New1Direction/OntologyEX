# adyen payments ontology (eval-5)

The Agent Ontology Kit in **research mode** on Adyen's online card-payments API, scoped to a
**payments-operations agent** (capture / refund / cancel + dispute defense + payout monitoring).
Mined from public `docs.adyen.com`; validated by the kit's own `scaffold.py`.

This run exists as a deliberate **competitor swap** against `eval-1-stripe`: same field, different
processor. See `comparison-vs-stripe.md` for the side-by-side that tests the kit's core claim —
*L1 survives the swap, L2/L3 diverge.*

## Files

| File | Layer | Holds |
|---|---|---|
| `00-scope.md` | — | target (Adyen, research), consumer (MCP), boundary, competency questions |
| `10-upper.yaml` | L0 | 9 gist anchors (same set as eval-1 for comparability) |
| `20-domain.yaml` | L1 | 13 classes + 12 relations |
| `30-task.yaml` | L2 | 8 payments-ops tasks |
| `40-application.yaml` | L3 | 17 Adyen artifacts (endpoints, webhooks, derived tools) |
| `50-mappings.yaml` | — | generated app→domain→upper crosswalk (17 rows) |
| `mcp-tools.json` | binding | 8 MCP tools, preconditions folded into descriptions |

## Reproduce

```bash
python ../../../ontology-extraction/scripts/scaffold.py validate .   # 0 errors, 0 warnings
python ../../../ontology-extraction/scripts/scaffold.py mappings .
```

## What the model captures that a flat endpoint list doesn't

- **Capture is first-class.** Adyen separates authorisation from capture; the ontology makes
  `Capture` its own L1 noun with `captures_payment`, and the agent tool knows it can't capture
  what's already captured.
- **Cancel vs refund is a state gate.** `CancelPayment` requires *not-yet-captured*; after capture
  the only path is `RefundPayment`. That precondition rides in the tool description.
- **Disputes have stages.** `Dispute` (the challenge, may be RFI with no funds moved) is distinct
  from `Chargeback` (the forced-debit stage). `defend_dispute` is gated on NOTIFICATION_OF_CHARGEBACK.
- **`accept_dispute` is irreversible** — flagged for human confirmation, same discipline as Stripe's
  `close_dispute`.
