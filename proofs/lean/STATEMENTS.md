# Changes to the trusted Lean statements

Newest first. Written by `tests/lean_statements.py update`.

## 2026-10-29

Decisions: a batch of compare-and-set operations applies whole, only to items exactly as seen; changes nothing else; undoing it restores exactly its items and keeps later decisions on other items, and is refused when a later decision changed one of them; batches on different items commute and never make each other stale.

- changed: `OntologyEXModel/Decisions.lean`
- changed: `OntologyEXSpec/Decisions.lean`

## 2026-10-29

Impact standing: the strongest chain of 1 to D hops, nothing without a chain, independent of relationship order and of entity names.

- changed: `OntologyEXModel/Impact.lean`
- changed: `OntologyEXSpec/Impact.lean`

