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

/-!
# Business operating picture — rollup, evidence grades, link decisions

Models of `business_view.rollup`, `evidence_grade.ladder`, and the link decision
rule in `link_suggest`. The definitions below are also printed as an exhaustive
table by `lake exe conformance` and compared with Python and Rust in CI
(tests/conformance.py). That is bounded testing, not a refinement proof.
-/

namespace OntologyEX.Business

inductive Status where
  | ok | warning | error | stale | unknown
deriving DecidableEq, Repr

structure Sys where
  status : Status
  impacted : Bool
deriving DecidableEq, Repr

inductive ActionHealth where
  | atRisk | possiblyImpacted | degraded | stale | unknown | unlinked | reportedOk
deriving DecidableEq, Repr

def isError (s : Sys) : Bool := s.status == Status.error
def isImpacted (s : Sys) : Bool := s.impacted
def isWarning (s : Sys) : Bool := s.status == Status.warning
def isStale (s : Sys) : Bool := s.status == Status.stale
def notOk (s : Sys) : Bool := s.status != Status.ok

def rollupCore (xs : List Sys) : ActionHealth :=
  if xs.any isError then .atRisk
  else if xs.any isImpacted then .possiblyImpacted
  else if xs.any isWarning then .degraded
  else if xs.any isStale then .stale
  else if xs.any notOk then .unknown
  else .reportedOk

/-- The most urgent source-reported condition among an action's linked systems. -/
def rollup (xs : List Sys) : ActionHealth :=
  if xs.isEmpty then .unlinked else rollupCore xs

theorem any_false {α : Type} {p : α → Bool} {xs : List α} (h : ¬ xs.any p = true) :
    ∀ x ∈ xs, p x = false := by
  intro x hx
  cases hpx : p x
  · rfl
  · exact absurd (List.any_eq_true.mpr ⟨x, hx, hpx⟩) h

theorem nonempty_of_mem {α : Type} {x : α} {xs : List α} (h : x ∈ xs) : xs.isEmpty = false := by
  cases xs with
  | nil => cases h
  | cons _ _ => rfl

/-- No linked system: health is UNLINKED, never a status. -/
theorem rollup_nil : rollup [] = .unlinked := rfl

/-- One failing linked system makes the action AT RISK, whatever the others report. -/
theorem error_forces_atRisk (xs : List Sys) (s : Sys) (hs : s ∈ xs)
    (he : s.status = .error) : rollup xs = .atRisk := by
  have hany : xs.any isError = true := List.any_eq_true.mpr ⟨s, hs, by simp [isError, he]⟩
  unfold rollup rollupCore
  rw [if_neg (by simp [nonempty_of_mem hs]), if_pos hany]

theorem not_ok_of_bne (st : Status) (h : (st != Status.ok) = false) : st = .ok := by
  revert h
  cases st <;> decide

/-- REPORTED OK only when there is a linked system and every one reports ok
    with no failing dependency. -/
theorem reportedOk_sound (xs : List Sys) (h : rollup xs = .reportedOk) :
    xs ≠ [] ∧ ∀ s ∈ xs, s.status = .ok ∧ s.impacted = false := by
  unfold rollup rollupCore at h
  by_cases hE : xs.isEmpty = true
  · rw [if_pos hE] at h; cases h
  · rw [if_neg hE] at h
    by_cases h1 : xs.any isError = true
    · rw [if_pos h1] at h; cases h
    · rw [if_neg h1] at h
      by_cases h2 : xs.any isImpacted = true
      · rw [if_pos h2] at h; cases h
      · rw [if_neg h2] at h
        by_cases h3 : xs.any isWarning = true
        · rw [if_pos h3] at h; cases h
        · rw [if_neg h3] at h
          by_cases h4 : xs.any isStale = true
          · rw [if_pos h4] at h; cases h
          · rw [if_neg h4] at h
            by_cases h5 : xs.any notOk = true
            · rw [if_pos h5] at h; cases h
            · refine ⟨?_, ?_⟩
              · intro hnil
                subst hnil
                exact hE rfl
              · intro s hs
                exact ⟨not_ok_of_bne s.status (any_false h5 s hs), any_false h2 s hs⟩

/-- Unknown, stale, or warning on any linked system can never show REPORTED OK. -/
theorem not_ok_never_reportedOk (xs : List Sys) (s : Sys) (hs : s ∈ xs)
    (h : s.status ≠ .ok) : rollup xs ≠ .reportedOk :=
  fun hr => h ((reportedOk_sound xs hr).2 s hs).1

/-- A declared dependency on a failing system can never show REPORTED OK. -/
theorem impacted_never_reportedOk (xs : List Sys) (s : Sys) (hs : s ∈ xs)
    (h : s.impacted = true) : rollup xs ≠ .reportedOk := by
  intro hr
  have := ((reportedOk_sound xs hr).2 s hs).2
  rw [h] at this
  cases this

theorem any_perm {α : Type} {p : α → Bool} {l₁ l₂ : List α} (h : l₁.Perm l₂) :
    l₁.any p = l₂.any p := by
  cases h1 : l₁.any p <;> cases h2 : l₂.any p
  · rfl
  · obtain ⟨x, hx, hp⟩ := List.any_eq_true.mp h2
    have h3 : l₁.any p = true := List.any_eq_true.mpr ⟨x, h.mem_iff.mpr hx, hp⟩
    rw [h1] at h3; cases h3
  · obtain ⟨x, hx, hp⟩ := List.any_eq_true.mp h1
    have h3 : l₂.any p = true := List.any_eq_true.mpr ⟨x, h.mem_iff.mp hx, hp⟩
    rw [h2] at h3; cases h3
  · rfl

theorem isEmpty_perm {α : Type} {l₁ l₂ : List α} (h : l₁.Perm l₂) :
    l₁.isEmpty = l₂.isEmpty := by
  have hl := h.length_eq
  cases l₁ <;> cases l₂ <;> first | rfl | simp at hl

/-- The order in which systems are linked never changes an action's health. -/
theorem rollup_perm {l₁ l₂ : List Sys} (h : l₁.Perm l₂) : rollup l₁ = rollup l₂ := by
  unfold rollup rollupCore
  rw [isEmpty_perm h, any_perm (p := isError) h, any_perm (p := isImpacted) h,
      any_perm (p := isWarning) h, any_perm (p := isStale) h, any_perm (p := notOk) h]

/-! ## Evidence grade ladder (`evidence_grade.ladder`) -/

inductive Grade where
  | g0 | g1 | g2 | g3 | g4
deriving DecidableEq, Repr

inductive Reason where
  | rejected | notReviewed | inferred | oneKind | checkFailed | notExercised | noG4Producer
deriving DecidableEq, Repr

def Grade.rank : Grade → Nat
  | .g0 => 0 | .g1 => 1 | .g2 => 2 | .g3 => 3 | .g4 => 4

def ladder (rejected supported observed multiKind failed passed : Bool) : Grade × Reason :=
  if rejected then (.g0, .rejected)
  else if !supported then (.g0, .notReviewed)
  else if !observed then (.g1, .inferred)
  else if !multiKind then (.g1, .oneKind)
  else if failed then (.g2, .checkFailed)
  else if !passed then (.g2, .notExercised)
  else (.g3, .noG4Producer)

/-- No recorded event can produce G4: it has no producer. -/
theorem ladder_never_g4 :
    ∀ a b c d e f : Bool, (ladder a b c d e f).1 ≠ .g4 := by decide

/-- A rejecting review always wins. -/
theorem rejection_is_g0 :
    ∀ b c d e f : Bool, (ladder true b c d e f).1 = .g0 := by decide

/-- An inferred rule stops at G1. -/
theorem inferred_at_most_g1 :
    ∀ a b d e f : Bool, ((ladder a b false d e f).1).rank ≤ 1 := by decide

/-- G3 needs every rung: no rejection, a review, observed status, two evidence
    kinds, no failed check, and a passing check. -/
theorem g3_needs_every_rung :
    ∀ a b c d e f : Bool, (ladder a b c d e f).1 = .g3 →
      a = false ∧ b = true ∧ c = true ∧ d = true ∧ e = false ∧ f = true := by decide

/-- Recording a passing check never lowers a grade. -/
theorem passing_check_monotone :
    ∀ a b c d e : Bool, (ladder a b c d e false).1.rank ≤ (ladder a b c d e true).1.rank := by
  decide

/-- Recording a failed check never raises a grade. -/
theorem failed_check_antitone :
    ∀ a b c d f : Bool, (ladder a b c d true f).1.rank ≤ (ladder a b c d false f).1.rank := by
  decide

/-! ## Link decisions (`link_suggest.decide`) -/

structure Link where
  skill : Nat
  binding : Nat
  component : Nat
deriving DecidableEq, Repr

inductive Decision where
  | confirm | reject
deriving DecidableEq, Repr

structure Suggestion where
  id : Nat
  link : Link
deriving DecidableEq, Repr

structure DecisionRec where
  id : Nat
  decision : Decision
deriving DecidableEq, Repr

def confirmedBy (ds : List DecisionRec) (s : Suggestion) : Bool :=
  ds.any (fun d => d.id == s.id && d.decision == Decision.confirm)

/-- Links after decisions: only confirmed suggestions are appended. -/
def applyDecisions (links : List Link) (suggs : List Suggestion) (ds : List DecisionRec) :
    List Link :=
  links ++ (suggs.filter (confirmedBy ds)).map (·.link)

theorem confirmed_has_decision (ds : List DecisionRec) (s : Suggestion)
    (h : confirmedBy ds s = true) : ∃ d ∈ ds, d.id = s.id ∧ d.decision = .confirm := by
  obtain ⟨d, hd, hp⟩ := List.any_eq_true.mp h
  simp only [Bool.and_eq_true, beq_iff_eq] at hp
  exact ⟨d, hd, hp.1, hp.2⟩

theorem filter_unconfirmed (suggs : List Suggestion) (ds : List DecisionRec)
    (h : ∀ s ∈ suggs, confirmedBy ds s = false) : suggs.filter (confirmedBy ds) = [] := by
  induction suggs with
  | nil => rfl
  | cons s t ih =>
    have hs : confirmedBy ds s = false := h s (List.mem_cons_self ..)
    have ht : ∀ x ∈ t, confirmedBy ds x = false := fun x hx => h x (List.mem_cons_of_mem _ hx)
    simp [hs, ih ht]

/-- Suggestions alone never change the links. -/
theorem suggestions_alone_change_nothing (links : List Link) (suggs : List Suggestion) :
    applyDecisions links suggs [] = links := by
  rw [applyDecisions, filter_unconfirmed suggs [] (fun _ _ => rfl)]
  simp

/-- Rejections never add a link. -/
theorem rejections_add_nothing (links : List Link) (suggs : List Suggestion)
    (ds : List DecisionRec) (h : ∀ d ∈ ds, d.decision = .reject) :
    applyDecisions links suggs ds = links := by
  have hnone : ∀ s ∈ suggs, confirmedBy ds s = false := by
    intro s _
    cases hc : confirmedBy ds s
    · rfl
    · obtain ⟨d, hd, _, hdec⟩ := confirmed_has_decision ds s hc
      rw [h d hd] at hdec
      cases hdec
  rw [applyDecisions, filter_unconfirmed suggs ds hnone]
  simp

/-- Every added link comes from a suggestion with an explicit confirm decision. -/
theorem added_links_were_confirmed (links : List Link) (suggs : List Suggestion)
    (ds : List DecisionRec) (l : Link) (h : l ∈ applyDecisions links suggs ds) :
    l ∈ links ∨ ∃ s ∈ suggs, (∃ d ∈ ds, d.id = s.id ∧ d.decision = .confirm) ∧ s.link = l := by
  unfold applyDecisions at h
  rcases List.mem_append.mp h with h | h
  · exact Or.inl h
  · obtain ⟨s, hs, rfl⟩ := List.mem_map.mp h
    have hf := List.mem_filter.mp hs
    exact Or.inr ⟨s, hf.1, confirmed_has_decision ds s hf.2, rfl⟩

/-- Existing links are kept by every decision list. -/
theorem decisions_keep_links (links : List Link) (suggs : List Suggestion)
    (ds : List DecisionRec) (l : Link) (h : l ∈ links) : l ∈ applyDecisions links suggs ds :=
  List.mem_append.mpr (Or.inl h)

end OntologyEX.Business

/-!
# Combining several observation sources (`control_room.build_state`)

Each source contributes its newest sample, which is either fresh or expired.
The component shows the most severe fresh status; with no fresh sample it is
stale. (A source that contradicts itself at one timestamp fails closed to
unknown before this step.)
-/

namespace OntologyEX.Fusion

inductive Reported where
  | ok | warning | error
deriving DecidableEq, Repr

structure Sample where
  status : Reported
  fresh : Bool
deriving DecidableEq, Repr

inductive Shown where
  | ok | warning | error | stale | unknown
deriving DecidableEq, Repr

def freshError (s : Sample) : Bool := s.fresh && s.status == Reported.error
def freshWarning (s : Sample) : Bool := s.fresh && s.status == Reported.warning
def isFresh (s : Sample) : Bool := s.fresh

def fuse (xs : List Sample) : Shown :=
  if xs.isEmpty then .unknown
  else if xs.any freshError then .error
  else if xs.any freshWarning then .warning
  else if xs.any isFresh then .ok
  else .stale

/-- A fresh error from any source shows as error; another source's ok cannot hide it. -/
theorem fresh_error_dominates (xs : List Sample) (s : Sample) (hs : s ∈ xs)
    (hf : s.fresh = true) (he : s.status = .error) : fuse xs = .error := by
  have hany : xs.any freshError = true :=
    List.any_eq_true.mpr ⟨s, hs, by simp [freshError, hf, he]⟩
  unfold fuse
  rw [if_neg (by simp [Business.nonempty_of_mem hs]), if_pos hany]

/-- OK only when some source is fresh and every fresh source reports ok. -/
theorem fuse_ok_sound (xs : List Sample) (h : fuse xs = .ok) :
    (∃ s ∈ xs, s.fresh = true) ∧ ∀ s ∈ xs, s.fresh = true → s.status = .ok := by
  unfold fuse at h
  by_cases hE : xs.isEmpty = true
  · rw [if_pos hE] at h; cases h
  · rw [if_neg hE] at h
    by_cases h1 : xs.any freshError = true
    · rw [if_pos h1] at h; cases h
    · rw [if_neg h1] at h
      by_cases h2 : xs.any freshWarning = true
      · rw [if_pos h2] at h; cases h
      · rw [if_neg h2] at h
        by_cases h3 : xs.any isFresh = true
        · obtain ⟨w, hw, hwf⟩ := List.any_eq_true.mp h3
          refine ⟨⟨w, hw, hwf⟩, ?_⟩
          intro s hs hf
          have e1 : freshError s = false := Business.any_false h1 s hs
          have e2 : freshWarning s = false := Business.any_false h2 s hs
          cases hst : s.status
          · rfl
          · simp [freshWarning, hf, hst] at e2
          · simp [freshError, hf, hst] at e1
        · rw [if_neg h3] at h; cases h

/-- Expired samples alone never show ok, warning, or error: the component is stale. -/
theorem all_expired_is_stale (xs : List Sample) (hne : xs ≠ [])
    (h : ∀ s ∈ xs, s.fresh = false) : fuse xs = .stale := by
  have n1 : ¬ xs.any freshError = true := by
    intro hc
    obtain ⟨s, hs, hp⟩ := List.any_eq_true.mp hc
    simp [freshError, h s hs] at hp
  have n2 : ¬ xs.any freshWarning = true := by
    intro hc
    obtain ⟨s, hs, hp⟩ := List.any_eq_true.mp hc
    simp [freshWarning, h s hs] at hp
  have n3 : ¬ xs.any isFresh = true := by
    intro hc
    obtain ⟨s, hs, hp⟩ := List.any_eq_true.mp hc
    simp [isFresh, h s hs] at hp
  have n0 : ¬ xs.isEmpty = true := by
    cases xs with
    | nil => exact absurd rfl hne
    | cons _ _ => simp
  unfold fuse
  rw [if_neg n0, if_neg n1, if_neg n2, if_neg n3]

/-- The order of sources (and of lines in the observation file) never matters. -/
theorem fuse_perm {l₁ l₂ : List Sample} (h : l₁.Perm l₂) : fuse l₁ = fuse l₂ := by
  unfold fuse
  rw [Business.isEmpty_perm h, Business.any_perm (p := freshError) h,
      Business.any_perm (p := freshWarning) h, Business.any_perm (p := isFresh) h]

end OntologyEX.Fusion
