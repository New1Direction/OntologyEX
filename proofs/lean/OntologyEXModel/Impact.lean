/-! # Model: what relies on an entity, and how sure that is

The computation in `entity_context._impact` (Python) and `context.rs::impact` (Rust),
written in Lean. Entities are numbers and a relationship `r` says `r.src` relies on
`r.dst` with weakness `r.weak` (0 CONFIRMED, 1 SOURCE, 2 INFERRED; UNKNOWN relationships
are dropped before this runs). A chain is as weak as its weakest hop.

The code keeps one layer per hop count: for each entity, the weakness of its strongest
chain of exactly `k` hops to the origin. Across layers it keeps the strongest so far and
replaces it only when a later layer is strictly stronger, so ties keep the fewest hops.
That is `layer` and `strongest` below. What is not modelled: the path the code reports
as a witness, the order of the result list, and leaving the origin itself out of it.

The statements about this model are in `OntologyEXSpec/Impact.lean`; `Conformance.lean`
prints `strongest` on generated graphs and `tests/conformance.py` compares it with Python.
-/
namespace OntologyEX.Model.Impact

/-- `src` relies on `dst`; `weak` is how weak that relationship is. -/
structure Rel where
  src : Nat
  dst : Nat
  weak : Nat
deriving DecidableEq, Repr

/-- The least of a list, if it has one. -/
def minOpt : List Nat → Option Nat
  | [] => none
  | x :: xs =>
    match minOpt xs with
    | none => some x
    | some m => some (min x m)

/-- One more hop: for `v`, the strongest of `max (weakness at r.dst) r.weak` over the
relationships `r` from `v`. -/
def step (rs : List Rel) (prev : Nat → Option Nat) (v : Nat) : Option Nat :=
  minOpt (rs.filterMap fun r => if r.src = v then (prev r.dst).map (fun w => max w r.weak) else none)

/-- Layer `k`: the weakness of each entity's strongest chain of exactly `k` hops to `o`. -/
def layer (rs : List Rel) (o : Nat) : Nat → Nat → Option Nat
  | 0 => fun v => if v = o then some 0 else none
  | k + 1 => step rs (layer rs o k)

/-- `(weakness, hops)` of `v`'s strongest chain of 1 to `d` hops, the fewest hops on ties. -/
def strongest (rs : List Rel) (o : Nat) : Nat → Nat → Option (Nat × Nat)
  | 0, _ => none
  | d + 1, v =>
    match strongest rs o d v, layer rs o (d + 1) v with
    | none, none => none
    | none, some w => some (w, d + 1)
    | some b, none => some b
    | some b, some w => if w < b.1 then some (w, d + 1) else some b

end OntologyEX.Model.Impact
