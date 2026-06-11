# Scope — stripe-support-agent
## Target
Stripe (research mode) — payments support-ops slice.
## Consumer
Agent tool surface (MCP) for a support-operations agent.
## Boundary
IN: Charge, PaymentIntent, Refund, Dispute, Evidence, Customer, MerchantAccount.
OUT: Payouts, Balance, Products/Prices, Connect, Issuing, Terminal (finance/commerce ops, not support).
## Competency questions
### L1
- [x] What can a Refund reference, and with what cardinality?
- [x] What statuses can a Refund and a Dispute hold?
- [x] Who funds a Charge and who receives it?
### L2
- [x] Preconditions for cancelling a refund?
- [x] What irreversible effect does closing a dispute have?
- [x] Constraint on total refunded amount per charge?
### L3
- [x] Which tool mutates dispute evidence and what is the payload limit?
- [x] Which API object realizes Refund and which tasks use it?
