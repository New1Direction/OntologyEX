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

/-! ## Agent proposals (`link_suggest.merge_proposals`, `agent_tools.py`)

An agent's proposal joins the same queue as the ranked suggestions, unless the link is
blocked (already linked, or rejected by a person). Nothing in the queue is a link until
a person's confirm decision names it. -/

def mergeProposals (blocked : Link → Bool) (suggs props : List Suggestion) : List Suggestion :=
  suggs ++ props.filter (fun p => !blocked p.link)

/-- Agent proposals, like any suggestion, change no link without a decision. -/
theorem proposals_alone_change_nothing (links : List Link) (blocked : Link → Bool)
    (suggs props : List Suggestion) :
    applyDecisions links (mergeProposals blocked suggs props) [] = links :=
  suggestions_alone_change_nothing links _

/-- A link a person rejected is never queued again by an agent proposal. -/
theorem blocked_proposal_never_queued (blocked : Link → Bool) (suggs props : List Suggestion)
    (p : Suggestion) (hb : blocked p.link = true) (hs : p ∉ suggs) :
    p ∉ mergeProposals blocked suggs props := by
  intro h
  rcases List.mem_append.mp h with h | h
  · exact hs h
  · have h2 := (List.mem_filter.mp h).2
    simp [hb] at h2

/-- Every link added after agent proposals carries a person's confirm decision. -/
theorem agent_links_were_confirmed (links : List Link) (blocked : Link → Bool)
    (suggs props : List Suggestion) (ds : List DecisionRec) (l : Link)
    (h : l ∈ applyDecisions links (mergeProposals blocked suggs props) ds) :
    l ∈ links ∨ ∃ s ∈ mergeProposals blocked suggs props,
      (∃ d ∈ ds, d.id = s.id ∧ d.decision = .confirm) ∧ s.link = l :=
  added_links_were_confirmed links _ ds l h

end OntologyEX.Business

/-!
# Entity context (`entity_context.py`, `engine/src/context.rs`)

Each relationship has a standing from where it comes from. A chain of relationships (an
explanation path, or the route by which a change could reach something) is only as strong
as its weakest link. These laws are what lets an operator read CONFIRMED as "a person
decided every step".
-/

namespace OntologyEX.Context

inductive Standing where
  | confirmed | source | inferred | unknown
deriving DecidableEq, Repr

def Standing.rank : Standing → Nat
  | .confirmed => 0 | .source => 1 | .inferred => 2 | .unknown => 3

inductive Origin where
  | declaredInFile | personDecision | rankedSuggestion | agentProposal | seenInTraces | gap
deriving DecidableEq, Repr

def standingOf : Origin → Standing
  | .declaredInFile => .source
  | .personDecision => .confirmed
  | .rankedSuggestion => .inferred
  | .agentProposal => .inferred
  | .seenInTraces => .inferred
  | .gap => .unknown

/-- Only a person's decision makes a relationship CONFIRMED. -/
theorem confirmed_only_by_person (o : Origin) : standingOf o = .confirmed ↔ o = .personDecision := by
  cases o <;> decide

/-- An agent's proposal is never CONFIRMED, whoever the agent acts for. -/
theorem agent_proposal_is_inferred : standingOf .agentProposal = .inferred := rfl

/-- The weaker of two standings; the first on ties. -/
def weaker (a b : Standing) : Standing := if a.rank < b.rank then b else a

def pathStanding : List Standing → Standing
  | [] => .confirmed
  | s :: rest => weaker s (pathStanding rest)

theorem weaker_ge_left (a b : Standing) : a.rank ≤ (weaker a b).rank := by
  cases a <;> cases b <;> decide

theorem weaker_ge_right (a b : Standing) : b.rank ≤ (weaker a b).rank := by
  cases a <;> cases b <;> decide

theorem weaker_cases (a b : Standing) : weaker a b = a ∨ weaker a b = b := by
  cases a <;> cases b <;> decide

theorem weaker_confirmed (a b : Standing) :
    weaker a b = .confirmed ↔ a = .confirmed ∧ b = .confirmed := by
  cases a <;> cases b <;> decide

/-- No hop is weaker than the path: a path never reads stronger than any of its hops. -/
theorem hop_no_weaker_than_path : ∀ (hs : List Standing) (h : Standing), h ∈ hs →
    h.rank ≤ (pathStanding hs).rank
  | [], _, hh => by cases hh
  | s :: rest, h, hh => by
    rcases List.mem_cons.mp hh with he | hr
    · rw [he]; exact weaker_ge_left s (pathStanding rest)
    · exact Nat.le_trans (hop_no_weaker_than_path rest h hr) (weaker_ge_right s (pathStanding rest))

/-- A path is CONFIRMED exactly when a person confirmed every hop. -/
theorem path_confirmed_iff : ∀ hs : List Standing,
    pathStanding hs = .confirmed ↔ ∀ h ∈ hs, h = .confirmed
  | [] => by simp [pathStanding]
  | s :: rest => by
    show weaker s (pathStanding rest) = .confirmed ↔ _
    rw [weaker_confirmed, path_confirmed_iff rest]
    simp

/-- One inferred hop makes the whole path at best INFERRED. -/
theorem inferred_hop_taints_path (hs : List Standing) (h : Standing.inferred ∈ hs) :
    2 ≤ (pathStanding hs).rank :=
  hop_no_weaker_than_path hs .inferred h

/-- A non-empty path's standing is the standing of one of its hops. -/
theorem path_is_a_hop : ∀ hs : List Standing, hs ≠ [] → pathStanding hs ∈ hs
  | [], h => (h rfl).elim
  | [s], _ => by cases s <;> first | decide | simp [pathStanding, weaker, Standing.rank]
  | s :: t :: rest, _ => by
    have ih := path_is_a_hop (t :: rest) (List.cons_ne_nil _ _)
    show weaker s (pathStanding (t :: rest)) ∈ s :: t :: rest
    rcases weaker_cases s (pathStanding (t :: rest)) with h | h
    · rw [h]; exact List.mem_cons_self ..
    · rw [h]; exact List.mem_cons_of_mem _ ih

/-! ## Decision watermarks (`watermark.py`, `entity_context.evidence_check`)

A confirmation records the evidence it rested on. It reads as CONFIRMED only while that
evidence is unchanged; links decided before watermarks (`none`) keep CONFIRMED. -/

def linkStanding {W : Type} [DecidableEq W] (recorded : Option W) (now : W) : Standing :=
  match recorded with
  | none => .confirmed
  | some r => if r = now then .confirmed else .inferred

/-- A confirmation whose evidence changed never reads as CONFIRMED. -/
theorem changed_evidence_never_confirmed {W : Type} [DecidableEq W] (r now : W) (h : r ≠ now) :
    linkStanding (some r) now ≠ .confirmed := by
  simp [linkStanding, h]

/-- Re-confirming records the evidence as it is now, which reads as CONFIRMED. -/
theorem reconfirm_is_current {W : Type} [DecidableEq W] (now : W) :
    linkStanding (some now) now = .confirmed := by
  simp [linkStanding]

/-- A watermarked link that reads as CONFIRMED rests on exactly the evidence it recorded. -/
theorem confirmed_link_matches_its_evidence {W : Type} [DecidableEq W] (r now : W)
    (h : linkStanding (some r) now = .confirmed) : r = now := by
  by_cases hr : r = now
  · exact hr
  · exact absurd h (changed_evidence_never_confirmed r now hr)

/-- A path through a link whose evidence changed is never CONFIRMED. -/
theorem stale_link_taints_path {W : Type} [DecidableEq W] (hs : List Standing) (r now : W)
    (h : r ≠ now) (hm : linkStanding (some r) now ∈ hs) : pathStanding hs ≠ .confirmed := by
  intro hc
  exact changed_evidence_never_confirmed r now h ((path_confirmed_iff hs).mp hc _ hm)

end OntologyEX.Context

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

/-!
# Live feeds (`feeds.py`, `engine/src/feeds.rs`)

Classification of one sample from its metrics and thresholds, the retention rule
of the observation store, and the nearest-rank p95 index. `lake exe conformance`
prints these definitions over bounded domains and CI compares them with Python
and Rust (tests/feeds_conformance.py).
-/

namespace OntologyEX.Feeds

inductive Status where
  | ok | warning | error
deriving DecidableEq, Repr

def Status.rank : Status → Nat
  | .ok => 0 | .warning => 1 | .error => 2

/-- Metric values and threshold rules as (metric, value) pairs; a rule is hit when
    the metric is present and at or above the threshold. -/
def hit (metrics rules : List (Nat × Nat)) : Bool :=
  rules.any (fun r => metrics.any (fun m => m.1 == r.1 && decide (r.2 ≤ m.2)))

def classify (metrics warning error : List (Nat × Nat)) (down : Bool) : Status :=
  if down || hit metrics error then .error
  else if hit metrics warning then .warning
  else .ok

/-- A target that is down is an error, whatever its metrics say. -/
theorem down_is_error (m w e : List (Nat × Nat)) : classify m w e true = .error := by
  simp [classify]

/-- Thresholds are inclusive: a value equal to an error threshold is an error. -/
theorem threshold_inclusive (m w e : List (Nat × Nat)) (k v : Nat) (hm : (k, v) ∈ m)
    (he : (k, v) ∈ e) : classify m w e false = .error := by
  have h : hit m e = true :=
    List.any_eq_true.mpr ⟨(k, v), he, List.any_eq_true.mpr ⟨(k, v), hm, by simp⟩⟩
  simp [classify, h]

/-- With no thresholds, a target that is up is ok. -/
theorem no_rules_ok (m : List (Nat × Nat)) : classify m [] [] false = .ok := by
  simp [classify, hit]

/-- `hi` reports every metric of `lo`, at the same or a higher value. -/
def dominates (hi lo : List (Nat × Nat)) : Prop :=
  ∀ p ∈ lo, ∃ q ∈ hi, q.1 = p.1 ∧ p.2 ≤ q.2

theorem hit_mono {hi lo : List (Nat × Nat)} (h : dominates hi lo) (rules : List (Nat × Nat))
    (hl : hit lo rules = true) : hit hi rules = true := by
  unfold hit at hl ⊢
  obtain ⟨r, hr, hm⟩ := List.any_eq_true.mp hl
  obtain ⟨p, hp, hpr⟩ := List.any_eq_true.mp hm
  obtain ⟨q, hq, hk, hv⟩ := h p hp
  simp only [Bool.and_eq_true, beq_iff_eq, decide_eq_true_eq] at hpr
  refine List.any_eq_true.mpr ⟨r, hr, List.any_eq_true.mpr ⟨q, hq, ?_⟩⟩
  simp only [Bool.and_eq_true, beq_iff_eq, decide_eq_true_eq]
  exact ⟨hk.trans hpr.1, Nat.le_trans hpr.2 hv⟩

/-- Worse metrics never make a sample look healthier. -/
theorem classify_monotone {hi lo : List (Nat × Nat)} (h : dominates hi lo)
    (w e : List (Nat × Nat)) (d : Bool) :
    (classify lo w e d).rank ≤ (classify hi w e d).rank := by
  have he := hit_mono h e
  have hw := hit_mono h w
  unfold classify
  cases d <;> cases h1 : hit lo e <;> cases h2 : hit hi e <;> cases h3 : hit lo w <;>
    cases h4 : hit hi w <;> simp_all [Status.rank]

/-! Retention: the store keeps the newest `k` samples of a component (by sort key). -/

def ins (x : Nat) : List Nat → List Nat
  | [] => [x]
  | y :: ys => if x ≤ y then x :: y :: ys else y :: ins x ys

def isort : List Nat → List Nat
  | [] => []
  | x :: xs => ins x (isort xs)

def keepNewest (k : Nat) (xs : List Nat) : List Nat :=
  (isort xs).drop ((isort xs).length - k)

theorem ins_le {x y : Nat} {ys : List Nat} (h : x ≤ y) : ins x (y :: ys) = x :: y :: ys := by
  simp [ins, h]

theorem ins_gt {x y : Nat} {ys : List Nat} (h : ¬ x ≤ y) : ins x (y :: ys) = y :: ins x ys := by
  simp [ins, h]

theorem ins_perm (x : Nat) : ∀ l : List Nat, (ins x l).Perm (x :: l)
  | [] => List.Perm.refl _
  | y :: ys => by
    by_cases hxy : x ≤ y
    · rw [ins_le hxy] <;> exact List.Perm.refl _
    · rw [ins_gt hxy]
      exact ((ins_perm x ys).cons y).trans (List.Perm.swap x y ys)

theorem isort_perm : ∀ l : List Nat, (isort l).Perm l
  | [] => List.Perm.nil
  | x :: xs => by
    rw [isort]
    exact (ins_perm x (isort xs)).trans ((isort_perm xs).cons x)

theorem ins_sorted (x : Nat) : ∀ l : List Nat, l.Pairwise (· ≤ ·) → (ins x l).Pairwise (· ≤ ·)
  | [], _ => by simp [ins]
  | y :: ys, h => by
    have hy := List.pairwise_cons.mp h
    by_cases hxy : x ≤ y
    · rw [ins_le hxy]
      refine List.Pairwise.cons ?_ h
      intro a ha
      rcases List.mem_cons.mp ha with rfl | ha
      · exact hxy
      · exact Nat.le_trans hxy (hy.1 a ha)
    · rw [ins_gt hxy]
      refine List.Pairwise.cons ?_ (ins_sorted x ys hy.2)
      intro a ha
      rcases List.mem_cons.mp ((ins_perm x ys).mem_iff.mp ha) with rfl | ha
      · omega
      · exact hy.1 a ha

theorem isort_sorted : ∀ l : List Nat, (isort l).Pairwise (· ≤ ·)
  | [] => List.Pairwise.nil
  | x :: xs => by
    rw [isort]
    exact ins_sorted x (isort xs) (isort_sorted xs)

/-- At most `k` samples per component are kept. -/
theorem keep_at_most (k : Nat) (xs : List Nat) : (keepNewest k xs).length ≤ k := by
  unfold keepNewest
  rw [List.length_drop]
  omega

/-- Every kept sample was in the input: retention never invents a sample. -/
theorem keep_from_input (k : Nat) (xs : List Nat) (x : Nat) (h : x ∈ keepNewest k xs) : x ∈ xs := by
  unfold keepNewest at h
  have h2 : x ∈ List.take ((isort xs).length - k) (isort xs) ++
      List.drop ((isort xs).length - k) (isort xs) := List.mem_append.mpr (Or.inr h)
  rw [List.take_append_drop] at h2
  exact (isort_perm xs).mem_iff.mp h2

/-- A sample that is dropped is no newer than any sample that is kept. -/
theorem dropped_are_older (k : Nat) (xs : List Nat) (x y : Nat) (hx : x ∈ xs)
    (hd : x ∉ keepNewest k xs) (hy : y ∈ keepNewest k xs) : x ≤ y := by
  unfold keepNewest at hd hy
  have hs := isort_sorted xs
  have hx' : x ∈ isort xs := (isort_perm xs).mem_iff.mpr hx
  rw [← List.take_append_drop ((isort xs).length - k) (isort xs)] at hs hx'
  have hcross := (List.pairwise_append.mp hs).2.2
  rcases List.mem_append.mp hx' with ht | hdrop
  · exact hcross x ht y hy
  · exact absurd hdrop hd

/-! The p95 of n samples is the sample at index ⌈0.95 n⌉ - 1 after sorting. -/

def p95Index (n : Nat) : Nat := (95 * n + 99) / 100 - 1

/-- The p95 index always names one of the samples. -/
theorem p95_index_in_range (n : Nat) (h : 0 < n) : p95Index n < n := by
  unfold p95Index
  omega

end OntologyEX.Feeds

/-!
# Change impact (`entity_context.change_impact`, `engine/src/context.rs`)

Each entity names the files it rests on (where it is declared, what its skill cites,
what its confirmed links recorded). A change touches the entities that name a changed
file; a changed file no entity names is reported as unmapped, never dropped.
-/

namespace OntologyEX.Impact

structure Ent where
  id : Nat
  paths : List Nat
deriving DecidableEq, Repr

def touches (cs : List Nat) (e : Ent) : Bool := e.paths.any (fun p => decide (p ∈ cs))

def touched (es : List Ent) (cs : List Nat) : List Ent := es.filter (touches cs)

def unmapped (es : List Ent) (cs : List Nat) : List Nat :=
  cs.filter (fun c => !es.any (fun e => decide (c ∈ e.paths)))

/-- Adding changes never hides an entity a smaller change touches. -/
theorem touched_monotone (es : List Ent) (cs ds : List Nat) (h : ∀ c ∈ cs, c ∈ ds)
    (e : Ent) (he : e ∈ touched es cs) : e ∈ touched es ds := by
  have hm := List.mem_filter.mp he
  apply List.mem_filter.mpr
  refine ⟨hm.1, ?_⟩
  obtain ⟨p, hp, hpc⟩ := List.any_eq_true.mp hm.2
  have hpc' : p ∈ cs := by simpa using hpc
  exact List.any_eq_true.mpr ⟨p, hp, by simpa using h p hpc'⟩

/-- Every changed file is accounted for: it touches some entity, or it is reported unmapped. -/
theorem every_change_accounted (es : List Ent) (cs : List Nat) (c : Nat) (hc : c ∈ cs) :
    (∃ e ∈ touched es cs, c ∈ e.paths) ∨ c ∈ unmapped es cs := by
  by_cases hany : es.any (fun e => decide (c ∈ e.paths)) = true
  · left
    obtain ⟨e, he, hce⟩ := List.any_eq_true.mp hany
    have hce' : c ∈ e.paths := by simpa using hce
    exact ⟨e, List.mem_filter.mpr ⟨he, List.any_eq_true.mpr ⟨c, hce', by simpa using hc⟩⟩, hce'⟩
  · right
    apply List.mem_filter.mpr
    refine ⟨hc, ?_⟩
    show (!es.any (fun e => decide (c ∈ e.paths))) = true
    cases h : es.any (fun e => decide (c ∈ e.paths))
    · rfl
    · exact absurd h hany

/-- An unmapped file names no entity: the report never hides a touch behind "unmapped". -/
theorem unmapped_touches_nothing (es : List Ent) (cs : List Nat) (c : Nat)
    (h : c ∈ unmapped es cs) : ∀ e ∈ es, c ∉ e.paths := by
  intro e he hce
  have h2 : (!es.any (fun e => decide (c ∈ e.paths))) = true := (List.mem_filter.mp h).2
  have hany : es.any (fun e => decide (c ∈ e.paths)) = true :=
    List.any_eq_true.mpr ⟨e, he, by simpa using hce⟩
  rw [hany] at h2
  exact absurd h2 (by decide)

end OntologyEX.Impact

/-!
# The decision queue, for any kind of fact (`decisions.py`, `link_suggest`, `dependency_suggest`)

Links and dependencies go through the same queue: ranked or trace-seen suggestions,
plus agent proposals that are not blocked (already decided, already declared, or
rejected by a person). These laws hold for any fact type, so they cover dependency
proposals (a fact is a source and target) as they cover link proposals.
-/

namespace OntologyEX.Queue

open OntologyEX.Business (Decision DecisionRec)

structure Sugg (α : Type) where
  id : Nat
  item : α

def confirmedBy {α : Type} (ds : List DecisionRec) (s : Sugg α) : Bool :=
  ds.any (fun d => d.id == s.id && d.decision == Decision.confirm)

/-- The facts after decisions: only confirmed suggestions are added. -/
def applyAll {α : Type} (facts : List α) (suggs : List (Sugg α)) (ds : List DecisionRec) : List α :=
  facts ++ (suggs.filter (confirmedBy ds)).map (·.item)

def mergeProposals {α : Type} (blocked : α → Bool) (suggs props : List (Sugg α)) : List (Sugg α) :=
  suggs ++ props.filter (fun p => !blocked p.item)

theorem confirmed_has_decision {α : Type} (ds : List DecisionRec) (s : Sugg α)
    (h : confirmedBy ds s = true) : ∃ d ∈ ds, d.id = s.id ∧ d.decision = .confirm := by
  obtain ⟨d, hd, hp⟩ := List.any_eq_true.mp h
  simp only [Bool.and_eq_true, beq_iff_eq] at hp
  exact ⟨d, hd, hp.1, hp.2⟩

theorem filter_unconfirmed {α : Type} (suggs : List (Sugg α)) (ds : List DecisionRec)
    (h : ∀ s ∈ suggs, confirmedBy ds s = false) : suggs.filter (confirmedBy ds) = [] := by
  induction suggs with
  | nil => rfl
  | cons s t ih =>
    have hs : confirmedBy ds s = false := h s (List.mem_cons_self ..)
    have ht : ∀ x ∈ t, confirmedBy ds x = false := fun x hx => h x (List.mem_cons_of_mem _ hx)
    simp [hs, ih ht]

/-- Agent proposals of any kind, like any suggestion, change no fact without a decision. -/
theorem proposals_alone_change_nothing {α : Type} (facts : List α) (blocked : α → Bool)
    (suggs props : List (Sugg α)) :
    applyAll facts (mergeProposals blocked suggs props) [] = facts := by
  rw [applyAll, filter_unconfirmed _ [] (fun _ _ => rfl)]
  simp

/-- A blocked fact (rejected by a person, already decided or declared) is never queued by a proposal. -/
theorem blocked_proposal_never_queued {α : Type} (blocked : α → Bool) (suggs props : List (Sugg α))
    (p : Sugg α) (hb : blocked p.item = true) (hs : p ∉ suggs) :
    p ∉ mergeProposals blocked suggs props := by
  intro h
  rcases List.mem_append.mp h with h | h
  · exact hs h
  · have h2 := (List.mem_filter.mp h).2
    simp [hb] at h2

/-- Every fact added after decisions is an existing one or carries a person's confirm decision. -/
theorem added_facts_were_confirmed {α : Type} (facts : List α) (suggs : List (Sugg α))
    (ds : List DecisionRec) (x : α) (h : x ∈ applyAll facts suggs ds) :
    x ∈ facts ∨ ∃ s ∈ suggs, (∃ d ∈ ds, d.id = s.id ∧ d.decision = .confirm) ∧ s.item = x := by
  unfold applyAll at h
  rcases List.mem_append.mp h with h | h
  · exact Or.inl h
  · obtain ⟨s, hs, rfl⟩ := List.mem_map.mp h
    have hf := List.mem_filter.mp hs
    exact Or.inr ⟨s, hf.1, confirmed_has_decision ds s hf.2, rfl⟩

/-- A dependency is a source and a target component. -/
abbrev Dependency := Nat × Nat

/-- An agent's dependency proposals change no dependency until a person confirms one. -/
theorem dependency_proposals_change_nothing (deps : List Dependency) (blocked : Dependency → Bool)
    (suggs props : List (Sugg Dependency)) :
    applyAll deps (mergeProposals blocked suggs props) [] = deps :=
  proposals_alone_change_nothing deps blocked suggs props

end OntologyEX.Queue
