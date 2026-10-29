/-! # Model: decisions, a batch of compare-and-set operations

`decisions.commit` (Python) and `decide.rs::commit` (Rust), written in Lean. The state is
what the decisions file says about each item (a link, a dependency or a rule, named by its
decision ID): its decided entry, which list and the row, or `none` when it is not decided.

An operation names one item, the entry the person saw (`expect`) and the entry to write
(`set`). A batch applies only if its items are distinct and every item is exactly as
expected; then each item becomes what its operation sets and nothing else changes.
Otherwise nothing changes at all (`none`). Undoing a batch is the batch with `expect` and
`set` swapped; the code builds it from the before and after it recorded in the history file.

What is not modelled: the code compares versions (the first 24 hex digits of the sha256 of
an entry's canonical JSON) where the model compares entries, so the model assumes no two
different entries share a version; the order of rows in the file; the history file itself;
operations that change nothing being left out of the recorded changes.

The statements about this model are in `OntologyEXSpec/Decisions.lean`.
-/
namespace OntologyEX.Model.Decisions

/-- Each item's decided entry, or `none`. -/
abbrev State (K V : Type) := K → Option V

/-- One compare-and-set: `key` must be at `expect`, and becomes `set`. -/
structure Op (K V : Type) where
  key : K
  expect : Option V
  set : Option V

variable {K V : Type} [DecidableEq K] [DecidableEq V]

/-- The items a batch touches. -/
def keys (b : List (Op K V)) : List K := b.map (·.key)

/-- The item is exactly as the operation expects. -/
def holds (s : State K V) (op : Op K V) : Bool := decide (s op.key = op.expect)

/-- Every operation of `b` written: an item a batch names takes its operation's entry. -/
def write (s : State K V) (b : List (Op K V)) : State K V :=
  fun k => match b.find? (fun op => decide (op.key = k)) with
    | some op => op.set
    | none => s k

/-- The batch, whole or not at all. -/
def commit (s : State K V) (b : List (Op K V)) : Option (State K V) :=
  if (keys b).Nodup ∧ b.all (holds s) = true then some (write s b) else none

/-- The batch that undoes `b` once it applied: each item expected at what `b` set, and set
back to what `b` expected. -/
def inverse (b : List (Op K V)) : List (Op K V) := b.map fun op => ⟨op.key, op.set, op.expect⟩

end OntologyEX.Model.Decisions
