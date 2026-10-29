import OntologyEXModel.Decisions

/-! # Trusted statements: decisions apply whole, to what was seen, and undo exactly

Read this file to know what is proved about confirming, rejecting and undoing decisions;
the proofs (`OntologyEXProofs/Decisions.lean`) are checked by Lean against these exact
propositions, and this file is pinned in `STATEMENTS.json`. Each holds for any kind of
item (`K`) and any kind of decided entry (`V`).
-/
namespace OntologyEX.Spec.Decisions
open OntologyEX.Model.Decisions

/-- A stale approval is refused: if any item is no longer what the person saw, nothing applies. -/
def StaleRefused : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s : State K V) (b : List (Op K V)),
    (∃ op ∈ b, s op.key ≠ op.expect) → commit s b = none

/-- A batch that names an item twice is refused. -/
def TwiceRefused : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s : State K V) (b : List (Op K V)),
    ¬ (keys b).Nodup → commit s b = none

/-- A batch whose items are distinct and all as the person saw them is not refused. -/
def CurrentApplies : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s : State K V) (b : List (Op K V)),
    (keys b).Nodup → (∀ op ∈ b, s op.key = op.expect) → ∃ s', commit s b = some s'

/-- What applies is the whole batch, on items that were exactly as seen. -/
def WholeBatch : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s s' : State K V) (b : List (Op K V)),
    commit s b = some s' → ∀ op ∈ b, s op.key = op.expect ∧ s' op.key = op.set

/-- A batch changes nothing but its own items. -/
def NothingElse : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s s' : State K V) (b : List (Op K V)),
    commit s b = some s' → ∀ k, k ∉ keys b → s' k = s k

/-- Undoing a batch right after it applied gives back the state before it. -/
def UndoRestores : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s s' : State K V) (b : List (Op K V)),
    commit s b = some s' → commit s' (inverse b) = some s

/-- Undoing a batch after any number of later batches on other items puts back exactly that
batch's items and keeps every later decision. -/
def UndoKeepsLaterDecisions : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s s₁ s₂ : State K V) (b : List (Op K V))
    (later : List (List (Op K V))),
    commit s b = some s₁ → later.foldlM commit s₁ = some s₂ →
    (∀ c ∈ later, ∀ k ∈ keys c, k ∉ keys b) →
    ∃ s₃, commit s₂ (inverse b) = some s₃ ∧ (∀ k ∈ keys b, s₃ k = s k) ∧ (∀ k, k ∉ keys b → s₃ k = s₂ k)

/-- An undo is refused when a later decision changed one of the batch's items: it never
overwrites a decision made after the one it undoes. -/
def UndoRefusedAfterChange : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s s₁ s₂ : State K V) (b : List (Op K V)),
    commit s b = some s₁ → (∃ k ∈ keys b, s₂ k ≠ s₁ k) → commit s₂ (inverse b) = none

/-- Batches on different items give the same result in either order. -/
def IndependentCommute : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s : State K V) (b₁ b₂ : List (Op K V)),
    (∀ k ∈ keys b₁, k ∉ keys b₂) →
    ((commit s b₁).bind fun t => commit t b₂) = ((commit s b₂).bind fun t => commit t b₁)

/-- A decision on other items never makes an approval stale, nor a stale one current. -/
def OthersNeverStale : Prop :=
  ∀ (K V : Type) [DecidableEq K] [DecidableEq V] (s s' : State K V) (b c : List (Op K V)),
    commit s b = some s' → (∀ k ∈ keys c, k ∉ keys b) → (commit s' c = none ↔ commit s c = none)

end OntologyEX.Spec.Decisions
