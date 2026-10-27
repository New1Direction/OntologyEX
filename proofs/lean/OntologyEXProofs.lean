/-!
# OntologyEX monitoring policy — small proof kernel

These statements formalize abstract policy decisions only.
They do NOT prove equivalence to the Rust implementation, source
authentication, real-world telemetry validity, or fault prediction.
-/

namespace OntologyEX

inductive Health where
  | unknown
  | stale
  | healthy
  | degraded
  | failed
  | conflicting
deriving DecidableEq, Repr

def latestOrUnknown : Option Health → Health
  | none => .unknown
  | some h => h

theorem missing_observation_is_unknown :
    latestOrUnknown none = Health.unknown := rfl

def mayShowHealthy (fresh : Bool) (conflict : Bool) (reported : Bool) : Bool :=
  fresh && (!conflict) && reported

theorem stale_cannot_show_healthy (conflict reported : Bool) :
    mayShowHealthy false conflict reported = false := by
  cases conflict <;> cases reported <;> rfl

theorem conflicting_cannot_show_healthy (fresh reported : Bool) :
    mayShowHealthy fresh true reported = false := by
  cases fresh <;> cases reported <;> rfl

-- An impact traversal describes a possibility, never changes a dependent's
-- independently observed status.
def dependentObservedHealth (observed : Health) (_upstreamFailed : Bool) : Health :=
  observed

theorem impact_is_not_observed_failure (observed : Health) (failed : Bool) :
    dependentObservedHealth observed failed = observed := rfl

def authorizeExecution (_health : Health) : Bool := false

theorem telemetry_never_authorizes_action (h : Health) :
    authorizeExecution h = false := rfl

end OntologyEX
