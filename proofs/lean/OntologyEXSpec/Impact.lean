import OntologyEXModel.Impact

/-! # Trusted statement: impact standing is the strongest chain

Read this file to know what is proved about impact; nothing else needs trusting. The
proofs (`OntologyEXProofs/Impact.lean`) are checked by Lean against these exact
propositions in `Audit.lean`, and this file is pinned in `STATEMENTS.json`: changing it
fails CI until the change is recorded with a reason.
-/
namespace OntologyEX.Spec.Impact
open OntologyEX.Model.Impact

/-- `Chain rs o k v w`: following exactly `k` relationships of `rs` from `v` arrives at
`o`, and the weakest of them has weakness `w`. The empty chain stays at `o`, weakness 0. -/
inductive Chain (rs : List Rel) (o : Nat) : Nat → Nat → Nat → Prop
  | here : Chain rs o 0 o 0
  | hop {k u w : Nat} (r : Rel) : r ∈ rs → r.dst = u → Chain rs o k u w →
      Chain rs o (k + 1) r.src (max w r.weak)

/-- `strongest` answers `(w, k)` exactly when there is a chain of `k` hops (1 to `D`) with
weakness `w`, no chain of 1 to `D` hops is stronger, and every shorter chain is weaker. -/
def StrongestChain : Prop :=
  ∀ (rs : List Rel) (o D v w k : Nat),
    strongest rs o D v = some (w, k) ↔
      (1 ≤ k ∧ k ≤ D ∧ Chain rs o k v w) ∧
      (∀ k' w', 1 ≤ k' → k' ≤ D → Chain rs o k' v w' → w ≤ w') ∧
      (∀ k' w', 1 ≤ k' → k' < k → Chain rs o k' v w' → w < w')

/-- `strongest` answers nothing exactly when no chain of 1 to `D` hops exists. -/
def NothingWithoutAChain : Prop :=
  ∀ (rs : List Rel) (o D v : Nat),
    strongest rs o D v = none ↔ ∀ k w, 1 ≤ k → k ≤ D → ¬ Chain rs o k v w

/-- The order relationships come in does not change the answer. -/
def OrderFree : Prop :=
  ∀ (rs rs' : List Rel) (o D v : Nat), (∀ r, r ∈ rs ↔ r ∈ rs') →
    strongest rs o D v = strongest rs' o D v

/-- Renaming entities (any one-to-one renaming) does not change the answer. -/
def NameFree : Prop :=
  ∀ (f : Nat → Nat), (∀ a b, f a = f b → a = b) → ∀ (rs : List Rel) (o D v : Nat),
    strongest (rs.map fun r => { r with src := f r.src, dst := f r.dst }) (f o) D (f v) =
      strongest rs o D v

end OntologyEX.Spec.Impact
