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
| Lean 4 | Proofs about the rollup, ladder, link decisions and impact standing; prints a conformance table; trusted statements pinned apart from proofs | `proofs/lean/` | lean-policy |
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

**Lean, the decision queue for any fact** (`OntologyEX.Queue`): `proposals_alone_change_nothing`,
`blocked_proposal_never_queued` and `added_facts_were_confirmed` hold for any kind of
fact, so they cover agent dependency proposals (`dependency_proposals_change_nothing`)
and rule proposals (`rule_proposals_change_nothing`)
as `OntologyEX.Business` covers links: a proposal adds nothing until a person confirms
it, and a pair a person rejected, already confirmed, or the repository declares is
never queued again.

**Lean, stale views** (`OntologyEX.Stale`, `stale_view.py`): a decision carries the
fingerprints of what the page showed, and the control room applies it only when every
one still matches the state it is about to record. `stale_view_changes_nothing`,
`current_view_applies` and `one_stale_refuses_batch`: a decision made on a view that
changed writes nothing, and one changed item refuses the whole batch.

**Lean, impact standing** (statement `OntologyEXSpec/Impact.lean`, proofs
`OntologyEXProofs/Impact.lean`, model `OntologyEXModel/Impact.lean`; code
`entity_context._impact` and `context.rs::impact`). What relies on an entity stands as its
strongest chain of 1 to 4 hops, the weakest hop deciding each chain:
- `StrongestChain`: the answer `(weakness, hops)` is the weakness of a real chain of that
  many hops, no chain within the limit is stronger, and every shorter chain is weaker;
- `NothingWithoutAChain`: nothing is answered exactly when no chain exists;
- `OrderFree` and `NameFree`: the order relationships come in, and the names entities
  have, never change the answer.
The conformance table's `impact` section (300 generated graphs) is printed by Lean from
`Model.Impact.strongest` and compared with Python; Rust matches Python
(`tests/rust_context_parity.py`). The code this replaced answered by whichever chain it
reached first in name order: it differs from the proven model on 12 of those 300 graphs.

**Bend** (`proofs/bend/business.bend`, checked by `bendlang/bend` at a pinned commit):
`empty_is_unlinked`, `error_anywhere_is_at_risk`, `unknown_anywhere_is_not_ok`,
`ladder_never_g4`, `rejection_is_g0`, `inferred_at_most_g1`,
`suggestions_alone_change_nothing`, `reject_is_inert`, `reject_adds_nothing`.

## Trusted statements, apart from the proofs

New proofs follow the split of
[anthropics/formal-math](https://github.com/anthropics/formal-math), where a frozen
challenge states the theorem and a comparator checks that a solution proves exactly it:

| Folder | Holds | Trusted? |
|---|---|---|
| `proofs/lean/OntologyEXModel/` | the code being reasoned about, written in Lean | read it to see it mirrors the code; the conformance tables test that it does |
| `proofs/lean/OntologyEXSpec/` | the statements, each a `def Name : Prop` | **yes: this is what is claimed** |
| `proofs/lean/OntologyEXProofs/` | the proofs | no: Lean checks them |

`tests/lean_statements.py` keeps the split honest:
- `STATEMENTS.json` pins the sha256 of every Model and Spec file. A changed statement
  fails CI until it is recorded with a reason (`update --reason`, kept in `STATEMENTS.md`).
- Model and Spec files may not contain `theorem`, `axiom`, `instance` or `sorry`. Proof files
  may not contain `sorry`, `axiom`, `native_decide`, `implemented_by`, `extern` or `unsafe`.
- `check --lean` (CI, lean-policy) builds a file with `example : Spec.S := Proofs.t` for each
  statement, so each theorem must prove the pinned proposition exactly. It then reads
  `#print axioms` for each theorem; anything beyond `propext`, `Quot.sound` and
  `Classical.choice` fails. `leanchecker` replays the modules in Lean's kernel.

Because the proofs import the statements, Lean's type check plays the comparator's part.
The pin keeps the statements themselves from drifting. The older proofs in
`OntologyEXProofs.lean` still keep definitions, statements and proofs together.

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
python tests/lean_statements.py check --lean   # with lake on PATH
python tests/conformance.py check /tmp/lean.json
bun /path/to/bendlang/bend/bend2/main.ts proofs/bend/business.bend   # ALL PROOFS CHECK
```

## Golden fixtures

Parity tests prove Python and Rust agree with each other; they cannot catch both changing
the same wrong way. `tests/golden.py` pins the answers. Three inputs: the operating-picture
demo, and two small repositories under `tests/golden/repos` (a shop that takes payments
and keeps card fields, a clinic that keeps patient records), each given a fixed git
history with fixed authors and dates and read against a fixed clock. Their outputs
(discovery, suppliers, owners, compliance, the command center, entity context, change
impact and the weekly digest) are stored under `tests/golden/expected`, after
normalizing only what is not behaviour: wall-clock times and the temporary directory.

`check` regenerates them and prints the JSON paths that differ; CI runs it in the Python
suite and, with the Rust engine built, checks Rust's discovery, context and change impact
against the same files. A change meant to alter an answer is made with
`update --reason "why"`, which records the reason per file in `MANIFEST.json` and in
`CHANGES.md`; an expected file edited by hand no longer matches its manifest hash and fails.

