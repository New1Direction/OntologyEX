# Scope — adyen

## Target
**Research mode.** Adyen's online card-payments API (a payments processor we don't control),
mined from public docs at `docs.adyen.com` (online-payments, capture, disputes-api,
api-explorer/Checkout). Chosen as a deliberate *competitor swap* against the existing
`eval-1-stripe` run: same field (card payments), different vendor — to test the kit's claim
that the **L1 domain layer survives a competitor swap** while L2/L3 diverge.

## Consumer
**Agent tools (MCP).** A payments-operations agent that captures, refunds, cancels, and
defends disputes. Highest-leverage binding = MCP tool schemas derived from the L2 tasks,
with Adyen's preconditions (manual-capture state, defense deadlines, irreversibility) folded
into the tool descriptions. Format per `references/output-formats.md`.

## Boundary
**In:** the authorisation → capture → refund / cancel modification lifecycle, and the dispute
defense flow (RFI, notification of chargeback, defend / supply documents / accept), plus
light payout/settlement monitoring.
**Out:** checkout/payment-method collection UX, in-person/POS terminals, Adyen for Platforms
(marketplace splits, balance accounts), Issuing, the risk-engine rule configuration, and
alternative-payment-method specifics.

## Competency questions

### L1 (domain)
- [x] What must exist before a Capture can occur? → `captures_payment` → an authorised Payment
- [x] How does Adyen's model differ from Stripe's at the noun level? → adds first-class Capture, Cancellation, Chargeback, DefenseReason
- [x] What does a Refund return funds against? → `refunds_payment`
- [x] What is the difference between a Dispute and a Chargeback here? → Dispute (challenge, may be RFI, no funds moved) vs Chargeback (forced debit stage)

### L2 (task)
- [x] What is the precondition for CancelPayment vs CapturePayment? → cancel needs NOT-yet-captured; capture needs authorised + manual capture
- [x] Which task is irreversible? → AcceptDispute (concede funds)
- [x] What must be true before DefendDispute? → status NOTIFICATION_OF_CHARGEBACK, reason ∈ applicable reasons, before deadline
- [x] Which tasks are read-only (no fund movement)? → RetrieveDefenseReasons, MonitorPayout

### L3 (application)
- [x] Which Adyen artifact realizes the link between a modification and its original payment? → `field.pspReference` (originalReference)
- [x] Which webhook signals a defensible chargeback? → `webhook.NOTIFICATION_OF_CHARGEBACK` → binds Chargeback
- [x] Which endpoint mutates a Capture, and which L1 class does it bind? → `api.captures` → Capture
- [x] Which L3 artifacts bind to no domain concept? → see validator orphan report
