# Fictional payments service — requirements v1
This is a local teaching fixture. It is not Stripe, a financial recommendation, or a production payment system.

Payment is a money ledger record with captured_minor, refunded_minor, and a payment_status.
Refund is a return of some captured money against one Payment.
MonetaryAmount is an integer number of minor currency units in this single-currency fixture.
SupportAgent is the person requesting a refund; a supplied authorization flag represents a separately verified permission.
Each Refund belongs to one Payment and is requested by one SupportAgent; a SupportAgent can request many Refunds.

## IssueRefund
The requested amount must be a strictly positive integer in minor currency units.
The captured amount must be nonnegative.
The already-refunded total must be nonnegative.
The already-refunded total must not exceed the captured amount.
Only a Payment with status captured can be refunded.
The requester_authorized flag must be true; the fixture does not verify identity or permission itself.
The requested amount must not exceed captured_minor minus refunded_minor.
On success, add the requested amount to refunded_minor. On rejection or unresolved policy, leave the ledger unchanged.

## ApproveException
Who may approve an exception to these rules is unspecified. Stop for review; do not infer an exception policy.

## Implementation boundary
payments.py contains a pure ledger transition, not a policy or authorization engine.
A caller must check the current policy before using it. No network, money movement, concurrency, idempotency, or production transaction guarantees are included.
