# Business layer verification: Python, Rust, Lean, Bend

The business operating picture makes three decisions that people act on:

1. **Action health.** Which actions are AT RISK, POSSIBLY IMPACTED, UNKNOWN, and so on
   (`business_view.rollup`).
2. **Evidence grades.** How far a rule's evidence was checked, G0–G4
   (`evidence_grade.ladder`).
3. **Link decisions.** Which binding-to-component links exist (`link_suggest`).

Each one is implemented, tested, and stated as laws in more than one language. The
Python code is the reference that the control room runs.

| Layer | What it does | Where | CI job |
|---|---|---|---|
| Python | Reference implementation and unit tests | `ontology-extraction/scripts/` | Validator |
| Rust | Same link matcher, rollup, and ladder; optional engine for suggestions (`--view-engine rust`) | `engine/src/business.rs` | rust-core |
| Lean 4 | Proofs about the rollup, ladder, and link decisions; prints a conformance table | `proofs/lean/` | lean-policy |
| Bend | The same safety rules as laws, proven by Bend's checker | `proofs/bend/business.bend` | bend-laws |

## What is proven

**Lean** (`proofs/lean/OntologyEXProofs.lean`, namespace `OntologyEX.Business`):

- `rollup_nil`: no linked system gives UNLINKED.
- `error_forces_atRisk`: one erroring linked system makes the action AT RISK.
- `reportedOk_sound`: REPORTED OK only when a system is linked and every linked
  system reports ok with no failing dependency.
- `not_ok_never_reportedOk`, `impacted_never_reportedOk`: unknown, stale, warning,
  or a declared dependency on a failing system never shows REPORTED OK.
- `rollup_perm`: the order in which systems are linked never changes the health.
- `ladder_never_g4`, `rejection_is_g0`, `inferred_at_most_g1`, `g3_needs_every_rung`,
  `passing_check_monotone`, `failed_check_antitone`.
- `suggestions_alone_change_nothing`, `rejections_add_nothing`,
  `added_links_were_confirmed`, `decisions_keep_links`: a suggestion never becomes a
  link without an explicit confirm decision, and a reject never adds one.

**Lean, source combining** (`OntologyEX.Fusion`): `fresh_error_dominates` (an ok from
one feed cannot hide another feed's fresh error), `fuse_ok_sound`, `all_expired_is_stale`,
`fuse_perm` (line and source order never matter). The conformance table's `fusion`
section (259 rows) is computed by Python through `control_room.build_state`, by the
Rust core's `project`, and by Lean's `fuse`, and all three must agree.

**Lean, agent proposals** (`OntologyEX.Business`, `link_suggest.merge_proposals`):
`proposals_alone_change_nothing` (an agent's proposal, like any suggestion, adds no
link without a decision), `blocked_proposal_never_queued` (a link a person rejected,
or one already linked, is never queued again by a proposal), `agent_links_were_confirmed`
(every link added after proposals carries a person's confirm decision).

**Lean, entity context** (`OntologyEX.Context`, `entity_context.py`, `engine/src/context.rs`):
`confirmed_only_by_person` (only a person's decision gives CONFIRMED),
`agent_proposal_is_inferred`, `hop_no_weaker_than_path` (a chain never reads stronger
than any of its links), `path_confirmed_iff` (a path is CONFIRMED exactly when every
hop is), `inferred_hop_taints_path`, `path_is_a_hop`. The conformance table's
`path_standing` section (341 rows: every chain of up to four standings) is computed
by Python's `path_standing`, Rust's `path_standing` and Lean's `pathStanding`, and all
three must agree; `tests/rust_context_parity.py` then checks the whole Rust entity
context, explanations and search against Python on the demo and 120 seeded bundles.

**Lean, decision watermarks** (`OntologyEX.Context.linkStanding`, `watermark.py`):
`changed_evidence_never_confirmed` (a confirmation whose recorded evidence differs from
the evidence now never reads as CONFIRMED), `reconfirm_is_current`,
`confirmed_link_matches_its_evidence` (a watermarked link that reads as CONFIRMED rests
on exactly the evidence it recorded), `stale_link_taints_path` (no path through such a
link reads as CONFIRMED). The conformance table's `watermark` section (6 rows) is
printed by Python, Rust and Lean.

**Lean, change impact** (`OntologyEX.Impact`, `entity_context.change_impact`):
`touched_monotone` (adding changes never hides an entity a smaller change touches),
`every_change_accounted` (each changed file touches an entity or is reported as
unmapped), `unmapped_touches_nothing` (a file reported as unmapped names no entity).
Rust computes the same report as Python (`tests/rust_context_parity.py`, demo change
sets and seeded random ones).

**Bend** (`proofs/bend/business.bend`, checked by `bendlang/bend` at a pinned commit):
`empty_is_unlinked`, `error_anywhere_is_at_risk`, `unknown_anywhere_is_not_ok`,
`ladder_never_g4`, `rejection_is_g0`, `inferred_at_most_g1`,
`suggestions_alone_change_nothing`, `reject_is_inert`, `reject_adds_nothing`.

## How the languages are tied together

- `tests/conformance.py expected` prints the Python table: every list of up to three
  linked systems over the ten (status, impacted) values (1,111 rows) and all 64
  ladder inputs.
- `ontologyex-view conformance` (Rust) and `.lake/build/bin/conformance` (Lean,
  printed from the proven definitions) must produce exactly that table.
- `tests/rust_business_parity.py` compares the Rust and Python link matchers on the
  demo, edge cases, and 300 seeded random inputs.

## Limits

- The Lean and Bend definitions are models. The proofs are about the models. The
  connection to the Python and Rust code is **exhaustive testing over a bounded
  domain**, not a refinement proof.
- Bend's own `--verdict` recheck (its Lean-proven kernel) is not run in CI.
- Bend is used here for its prover, not its GPU runtime. The admission gate in
  [rust-lean-bend2-architecture.md](rust-lean-bend2-architecture.md) still applies
  to any runtime use.
- None of this proves that a source-reported status is true, that a link is right,
  or that a rule matches the business. People still review those.

## Run locally

```bash
cargo test --manifest-path engine/Cargo.toml --all-targets
cargo build --manifest-path engine/Cargo.toml --bin ontologyex-view
python tests/rust_business_parity.py
(cd proofs/lean && lake build && lake build conformance && .lake/build/bin/conformance > /tmp/lean.json)
python tests/conformance.py check /tmp/lean.json
bun /path/to/bendlang/bend/bend2/main.ts proofs/bend/business.bend   # ALL PROOFS CHECK
```
