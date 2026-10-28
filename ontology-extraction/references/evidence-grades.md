# Evidence grades

`evidence_grade.py` gives each contract rule an **ordinal** grade, G0–G4. A grade says how
far the rule's evidence links were checked. It is computed from the verified bundle and from
recorded events. It is never authored into the model, never converted to a number or a
confidence, and never a permission. The idea comes from MisakaNet's E0–E4 lesson scale,
without its numeric weights.

| Grade | Label | What must be true (and every lower grade) |
|---|---|---|
| G0 | Cited | The compiler verified the cited bytes and file digests. Every compiled rule starts here. |
| G1 | Reviewed | A recorded review says the cited spans support the **current** claim, and no review rejects it. |
| G2 | Multi-kind | The rule is `observed` and cites two or more evidence kinds (for example requirement + implementation). |
| G3 | Exercised | A recorded behavior check passed for the current claim, and none failed. |
| G4 | Observed in use | A fresh agent used a handoff and its work passed independent checks. |

Rules:

- **Cumulative.** A passing behavior check does not skip a missing review.
- **Inferred rules stop at G1.** An inferred rule never becomes an executable acceptance condition.
- **G2 is not independence.** Two kinds from separate files can still share one origin
  (see [provenance](provenance.md)). The grade reports kinds, not independent support.
- **G4 has no producer yet.** No tool in this repository observes fresh-agent use, so no rule
  can reach G4. That is the honest answer, not a gap to fill with a declaration or a vote.
- **Unknowns and conflicts are not graded.** They are not claims.

## Claim binding

Each event is bound to a `claim_sha256`: a digest of the rule's root goal, ID, task,
statement, status, check, and the exact cited spans (path, file digest, lines, kind, quote
digest). If any of these change in a new version, earlier events are reported as
`stale_events` and no longer count. Unchanged rules keep their events across versions.

## Commands

```bash
# Grade every rule. Without --events, every rule is G0.
python "$SKILL_ROOT/scripts/evidence_grade.py" grade SKILL --events reviews/events.json

# Append one event with the rule's current claim digest. Append-only and idempotent.
python "$SKILL_ROOT/scripts/evidence_grade.py" record SKILL --events reviews/events.json \
  --rule remaining-balance --event REVIEW_SUPPORTS --by reviewer-name --ref "line 17 states it"
python "$SKILL_ROOT/scripts/evidence_grade.py" record SKILL --events reviews/events.json \
  --rule remaining-balance --event BEHAVIOR_CHECK_PASSED --by ci --ref tests/test_refund.py::test_remainder
```

Events: `REVIEW_SUPPORTS`, `REVIEW_REJECTS`, `BEHAVIOR_CHECK_PASSED`, `BEHAVIOR_CHECK_FAILED`.
Behavior events must name the check that ran. Keep the events file outside the skill bundle;
the bundle is hash-verified and the tool refuses a path inside it.

**Events are recorded assertions, not authenticated approvals.** The `--by` value is not
verified. An agent must not record review or behavior events for its own work: only the
person who reviewed the span or ran the check records the event. A source span that
supports a claim can still be obsolete or wrong.
