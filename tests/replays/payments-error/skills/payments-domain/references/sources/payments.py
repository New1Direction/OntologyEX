"""Fictional, in-memory ledger transition. Not a payment API or authorization layer."""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Payment:
    captured_minor: int
    refunded_minor: int
    payment_status: str


def apply_refund(payment: Payment, amount_minor: int) -> Payment:
    """Apply a caller-approved transition; return a new record, never mutate input.

    Basic ledger invariants are checked here as defense in depth. The caller still
    owns authorization, current business policy, synchronization, and idempotency.
    """
    values = (payment.captured_minor, payment.refunded_minor, amount_minor)
    if not all(type(v) is int and abs(v) <= 2**63 - 1 for v in values):
        raise ValueError("integer minor units required")
    if not 0 <= payment.refunded_minor <= payment.captured_minor:
        raise ValueError("inconsistent ledger")
    if payment.payment_status != "captured":
        raise ValueError("payment is not captured")
    if not 0 < amount_minor <= payment.captured_minor - payment.refunded_minor:
        raise ValueError("amount outside refundable remainder")
    return replace(payment, refunded_minor=payment.refunded_minor + amount_minor)
