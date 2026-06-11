# Competitor swap: Stripe (eval-1) vs Adyen (eval-5)

The kit's central claim is that **L1 (the domain layer) survives a competitor swap** — the nouns
belong to the *field*, not the vendor — while **L2 (tasks) and L3 (application) diverge** with each
processor's API. Running the kit on two different payments providers tests that claim directly.

Both runs use the same L0 (gist) anchor set, scoped to a payments agent. The overlap below is
computed by `scaffold` data, matching canonical ids **plus synonyms** (so Stripe `DisputeEvidence`
matches Adyen `DefenseDocument` because the latter lists it as a synonym).

## L1 — the domain layer (SHARED: 8 / 9)

| Stripe L1 | Adyen L1 | Same field-level concept? |
|---|---|---|
| `Charge` | `Payment` (syn *Charge*) | ✅ an authorised fund movement |
| `Refund` | `Refund` | ✅ identical |
| `Dispute` | `Dispute` | ✅ identical |
| `DisputeEvidence` | `DefenseDocument` (syn *DisputeEvidence*) | ✅ evidence to contest |
| `Customer` | `Customer` (syn *Shopper*) | ✅ the payer |
| `MerchantAccount` | `MerchantAccount` | ✅ identical |
| `SupportAgent` | `PaymentsAgent` (syn *SupportAgent*) | ✅ the operator |
| `MonetaryAmount` | `MonetaryAmount` | ✅ identical |
| `PaymentIntent` | — | ⚠️ Stripe-specific framing (Adyen folds intent into the authorisation) |

**8/9 Stripe concepts map straight across.** This is the claim holding up in practice: swap the
vendor, keep the domain. An agent that reasons over `Refund`, `Dispute`, `Customer`, `MerchantAccount`
needs no retraining when you change processors — only the bottom layer is rewired.

### Adyen adds 5 concepts (a richer model, because its API exposes more)

`Capture`, `Cancellation`, `Chargeback`, `DefenseReason`, `Payout`

These aren't disagreements with Stripe — they're concepts Stripe's *support-agent* scope didn't
surface. Adyen separates **authorisation from capture** (so `Capture` and `Cancellation` are
first-class), stages disputes (`Chargeback` distinct from the `Dispute`/RFI it escalates from),
defends under coded reasons (`DefenseReason`), and exposes settlement (`Payout`). Point the kit at
Stripe's *full* API (PaymentIntent capture, payouts) and these would converge further.

## L2 — the task layer (DIVERGES with each API)

| Stripe tasks (4) | Adyen tasks (8) |
|---|---|
| `ProcessRefund`, `CancelRefund` | `RefundPayment`, **`CapturePayment`**, **`CancelPayment`** |
| `SubmitDisputeEvidence`, `CloseDispute` | `SupplyDefenseDocument`, `DefendDispute`, `AcceptDispute`, `RetrieveDefenseReasons` |
| — | `MonitorPayout` |

The verbs follow the endpoints. Adyen's auth/capture split and staged-defense flow produce more
tasks — and therefore more tools. But notice the **shape repeats**: each task is IOPE
(inputs/outputs/preconditions/effects), and the irreversible concede (`CloseDispute` ↔
`AcceptDispute`) gets the same "confirm with a human" treatment in both.

## L3 — the application layer (FULLY vendor-specific, as designed)

| Stripe L3 | Adyen L3 |
|---|---|
| `refund_object`, `dispute_object`, `charge_object` | `api.captures`, `api.refunds`, `api.cancels`, `api.dispute` |
| `tool.process_refund`, … | `field.pspReference`, `webhook.NOTIFICATION_OF_CHARGEBACK`, `webhook.CAPTURE`, `tool.capture_payment`, … |

Nothing here transfers — `pspReference`, `NOTIFICATION_OF_CHARGEBACK`, the `/payments/{pspRef}/captures`
path are pure Adyen. That's correct: **L3 is by definition yours (the vendor's)**. Delete Adyen and
every term in this column vanishes; the L1 column above stays standing.

## Takeaway

```
        L0  identical  (same gist anchors)
        L1  8/9 shared ........ the domain is the field, not the vendor
        L2  diverges .......... verbs follow endpoints
        L3  0% shared ......... vendor-specific by definition
```

The gradient — universal at top, vendor-specific at bottom — is exactly what the four-layer split
promises. The comparison is reproducible: both ontologies validate clean, and the overlap figure
comes from their own YAML, not hand-assertion.
