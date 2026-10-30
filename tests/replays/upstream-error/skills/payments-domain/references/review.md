# Candidate acceptance checklist

Status: PENDING HUMAN REVIEW

- Verify source-to-claim semantics, intended policy versus current implementation.
- Inspect unknowns, conflicts, inferred rules, and unsupported conditions.
- Compare with the previous accepted version; inspect all impacted tasks.
- Run independent behavioral tests, including held-out and legitimate success cases.
- Check live-state, authorization, race, idempotency, and integration boundaries.
- Record reviewer, date, bundle ID, test evidence, and decision externally.

No human approval, security guarantee, or agent-performance improvement is claimed.
The checksums detect accidental drift, not an attacker able to rewrite the manifest.
