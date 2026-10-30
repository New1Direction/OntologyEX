# payments-domain — domain model

UNREVIEWED CANDIDATE. Source integrity is not semantic truth.

Root goal: goal-d9daa75139d7de2f

## Authored scope

# Scope

Fictional, single-currency partial refunds and unresolved exception approval.
Consumer: local domain skill and in-memory fixture only.
Questions: what can be refunded, how much remains, who may approve an exception, which code changes the ledger?
Out of scope: real APIs, concurrency, currency conversion, identity verification, production authorization.


## Concepts
- Payment: Payment is a money ledger record with captured_minor, refunded_minor, and a payment_status. [source: E-Payment]
- Refund: Refund is a return of some captured money against one Payment. [source: E-Refund]
- MonetaryAmount: MonetaryAmount is an integer number of minor currency units in this single-currency fixture. [source: E-MonetaryAmount]
- SupportAgent: SupportAgent is the person requesting a refund; a supplied authorization flag represents a separately verified permission. [source: E-SupportAgent]

## Action: IssueRefund
Inputs: {"amount_minor": "integer", "captured_minor": "integer", "payment_status": "string", "refunded_minor": "integer", "requester_authorized": "boolean"}
- positive-amount [observed]: The requested amount must be a strictly positive integer in minor currency units. (evidence: E-positive-amount)
- captured-nonnegative [observed]: The captured amount must be nonnegative. (evidence: E-captured-nonnegative)
- refunded-nonnegative [observed]: The already-refunded total must be nonnegative. (evidence: E-refunded-nonnegative)
- ledger-consistent [observed]: The already-refunded total must not exceed the captured amount. (evidence: E-ledger-consistent)
- captured-only [observed]: Only a Payment with status captured can be refunded. (evidence: E-captured-only)
- authorization-required [observed]: The requester_authorized flag must be true; the fixture does not verify identity or permission itself. (evidence: E-authorization-required)
- remaining-balance [observed]: The requested amount must not exceed captured_minor minus refunded_minor. (evidence: E-remaining-balance, E-remaining-code)
Implementation bindings: payments.Payment, payments.apply_refund

## Action: ApproveException
Inputs: {}
- exception-authority [unknowns]: The supplied requirements do not specify who can approve an exception. (evidence: E-exception)
Implementation bindings: payments.Payment

## Evidence index

Exact source spans are data, not instructions. Hashes verify bytes, not correctness.
- E-Payment [requirement]: requirements.md:4-4; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-Refund [requirement]: requirements.md:5-5; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-MonetaryAmount [requirement]: requirements.md:6-6; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-SupportAgent [requirement]: requirements.md:7-7; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-relation [requirement]: requirements.md:8-8; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-positive-amount [requirement]: requirements.md:11-11; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-captured-nonnegative [requirement]: requirements.md:12-12; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-refunded-nonnegative [requirement]: requirements.md:13-13; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-ledger-consistent [requirement]: requirements.md:14-14; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-captured-only [requirement]: requirements.md:15-15; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-authorization-required [requirement]: requirements.md:16-16; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-remaining-balance [requirement]: requirements.md:17-17; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-effect [requirement]: requirements.md:18-18; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-exception [requirement]: requirements.md:21-21; SHA-256 95da92ac21837893552567d29134849fe47c4edd76cefdc0d981b37fd1bededc
- E-ledger-code [implementation]: payments.py:6-6; SHA-256 a366c591b9f771036ca59f13a3934389d8b1f3d6bd68746013a4c1f9c8bdc7eb
- E-function-code [implementation]: payments.py:12-12; SHA-256 a366c591b9f771036ca59f13a3934389d8b1f3d6bd68746013a4c1f9c8bdc7eb
- E-remaining-code [implementation]: payments.py:25-26; SHA-256 a366c591b9f771036ca59f13a3934389d8b1f3d6bd68746013a4c1f9c8bdc7eb

## Evidence provenance

Paths and repeated quotations are not independent corroboration. Origins and independence groups are declared, not verified. Unknown lineage stays unknown. No confidence score, source truth, semantic contradiction detection, or execution authorization.

17 evidence records; 17 evidence atoms; 0 declared origins; 17 records with unknown lineage.

See references/provenance.json for all representations and declared origin/group assignments.
- authorization-required: 1 records / 0 declared origins; LINEAGE_UNKNOWN; declared groups: none
- captured-nonnegative: 1 records / 0 declared origins; LINEAGE_UNKNOWN; declared groups: none
- captured-only: 1 records / 0 declared origins; LINEAGE_UNKNOWN; declared groups: none
- ledger-consistent: 1 records / 0 declared origins; LINEAGE_UNKNOWN; declared groups: none
- positive-amount: 1 records / 0 declared origins; LINEAGE_UNKNOWN; declared groups: none
- refunded-nonnegative: 1 records / 0 declared origins; LINEAGE_UNKNOWN; declared groups: none
- remaining-balance: 2 records / 0 declared origins; LINEAGE_UNKNOWN; declared groups: none
- exception-authority: 1 records / 0 declared origins; UNKNOWN_RECORDED; declared groups: none
