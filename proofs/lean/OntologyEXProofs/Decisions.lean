import OntologyEXSpec.Decisions

/-! # Proofs: decisions (untrusted; checked against `OntologyEXSpec/Decisions.lean`) -/
namespace OntologyEX.Proofs.Decisions
open OntologyEX.Model.Decisions OntologyEX.Spec.Decisions

section
variable {K V : Type} [DecidableEq K] [DecidableEq V]

/-- A batch is applicable: distinct items, each exactly as expected. -/
def Ok (s : State K V) (b : List (Op K V)) : Prop := (keys b).Nodup ∧ ∀ op ∈ b, s op.key = op.expect

instance (s : State K V) (b : List (Op K V)) : Decidable (Ok s b) := inferInstanceAs (Decidable (_ ∧ _))

omit [DecidableEq K] in

theorem all_holds {s : State K V} {b : List (Op K V)} : b.all (holds s) = true ↔ ∀ op ∈ b, s op.key = op.expect := by
  simp [List.all_eq_true, holds]

theorem commit_of_ok {s : State K V} {b : List (Op K V)} (h : Ok s b) : commit s b = some (write s b) := by
  unfold commit
  rw [if_pos ⟨h.1, all_holds.mpr h.2⟩]

theorem commit_of_not_ok {s : State K V} {b : List (Op K V)} (h : ¬ Ok s b) : commit s b = none := by
  unfold commit
  rw [if_neg (fun hc => h ⟨hc.1, all_holds.mp hc.2⟩)]

theorem ok_of_commit {s s' : State K V} {b : List (Op K V)} (h : commit s b = some s') : Ok s b ∧ s' = write s b := by
  by_cases hok : Ok s b
  · rw [commit_of_ok hok] at h
    exact ⟨hok, (Option.some.inj h).symm⟩
  · rw [commit_of_not_ok hok] at h
    cases h

omit [DecidableEq K] [DecidableEq V] in
theorem mem_keys {b : List (Op K V)} {op : Op K V} (h : op ∈ b) : op.key ∈ keys b :=
  List.mem_map_of_mem h

omit [DecidableEq V] in
theorem find_none {b : List (Op K V)} {k : K} (h : k ∉ keys b) :
    b.find? (fun op => decide (op.key = k)) = none := by
  rw [List.find?_eq_none]
  intro op hop hk
  exact h (by simpa using (of_decide_eq_true hk) ▸ mem_keys hop)

omit [DecidableEq V] in
theorem find_mem {b : List (Op K V)} (hn : (keys b).Nodup) {op : Op K V} (hop : op ∈ b) :
    b.find? (fun o => decide (o.key = op.key)) = some op := by
  induction b with
  | nil => cases hop
  | cons x t ih =>
    simp only [keys, List.map_cons, List.nodup_cons] at hn
    rcases List.mem_cons.mp hop with rfl | ht
    · simp
    · have hx : x.key ≠ op.key := fun e => hn.1 (e ▸ mem_keys ht)
      rw [List.find?_cons, decide_eq_false hx]
      exact ih hn.2 ht

omit [DecidableEq V] in
theorem write_in {s : State K V} {b : List (Op K V)} (hn : (keys b).Nodup) {op : Op K V} (hop : op ∈ b) :
    write s b op.key = op.set := by
  simp only [write, find_mem hn hop]

omit [DecidableEq V] in
theorem write_out {s : State K V} {b : List (Op K V)} {k : K} (h : k ∉ keys b) : write s b k = s k := by
  simp only [write, find_none h]

omit [DecidableEq K] [DecidableEq V] in
theorem key_cases {b : List (Op K V)} {k : K} (h : k ∈ keys b) : ∃ op ∈ b, op.key = k := by
  simpa [keys] using h

omit [DecidableEq K] [DecidableEq V] in
theorem keys_inverse (b : List (Op K V)) : keys (inverse b) = keys b := by
  simp [keys, inverse, List.map_map, Function.comp_def]

omit [DecidableEq K] [DecidableEq V] in
theorem mem_inverse {b : List (Op K V)} {op' : Op K V} :
    op' ∈ inverse b ↔ ∃ op ∈ b, op' = ⟨op.key, op.set, op.expect⟩ := by
  simp only [inverse, List.mem_map]
  constructor
  · rintro ⟨op, h, rfl⟩; exact ⟨op, h, rfl⟩
  · rintro ⟨op, h, rfl⟩; exact ⟨op, h, rfl⟩

/-- After a batch applied, its inverse applies to any state that agrees on the batch's items,
and gives back the old entries there and the given state elsewhere. -/
theorem undo_on {s s₁ s₂ : State K V} {b : List (Op K V)} (h : commit s b = some s₁)
    (agree : ∀ k ∈ keys b, s₂ k = s₁ k) :
    commit s₂ (inverse b) = some (fun k => if k ∈ keys b then s k else s₂ k) := by
  obtain ⟨⟨hn, hexp⟩, rfl⟩ := ok_of_commit h
  have hn' : (keys (inverse b)).Nodup := by rw [keys_inverse]; exact hn
  have hok : Ok s₂ (inverse b) := by
    refine ⟨hn', fun op' hop' => ?_⟩
    obtain ⟨op, hop, rfl⟩ := mem_inverse.mp hop'
    show s₂ op.key = op.set
    rw [agree op.key (mem_keys hop), write_in hn hop]
  rw [commit_of_ok hok]
  congr 1
  funext k
  by_cases hk : k ∈ keys b
  · obtain ⟨op, hop, rfl⟩ := key_cases hk
    have hinv : (⟨op.key, op.set, op.expect⟩ : Op K V) ∈ inverse b := mem_inverse.mpr ⟨op, hop, rfl⟩
    rw [if_pos hk, write_in hn' hinv]
    exact (hexp op hop).symm
  · rw [if_neg hk, write_out (by rw [keys_inverse]; exact hk)]

theorem fold_frame {ks : List K} :
    ∀ (bs : List (List (Op K V))) (s₁ s₂ : State K V), bs.foldlM commit s₁ = some s₂ →
      (∀ c ∈ bs, ∀ k ∈ keys c, k ∉ ks) → ∀ k ∈ ks, s₂ k = s₁ k
  | [], s₁, s₂, h, _, k, _ => by
    simp only [List.foldlM_nil] at h
    cases h; rfl
  | c :: cs, s₁, s₂, h, hd, k, hk => by
    simp only [List.foldlM_cons] at h
    cases hc : commit s₁ c with
    | none => rw [hc] at h; cases h
    | some t =>
      rw [hc] at h
      have ht := fold_frame cs t s₂ h (fun c' hc' => hd c' (List.mem_cons_of_mem _ hc')) k hk
      obtain ⟨_, rfl⟩ := ok_of_commit hc
      rw [ht, write_out (fun hkc => hd c (List.mem_cons_self ..) k hkc hk)]

end

theorem stale_refused : StaleRefused := by
  intro K V _ _ s b ⟨op, hop, hne⟩
  exact commit_of_not_ok fun h => hne (h.2 op hop)

theorem twice_refused : TwiceRefused := by
  intro K V _ _ s b hn
  exact commit_of_not_ok fun h => hn h.1

theorem current_applies : CurrentApplies := by
  intro K V _ _ s b hn hexp
  exact ⟨_, commit_of_ok ⟨hn, hexp⟩⟩

theorem whole_batch : WholeBatch := by
  intro K V _ _ s s' b h op hop
  obtain ⟨⟨hn, hexp⟩, rfl⟩ := ok_of_commit h
  exact ⟨hexp op hop, write_in hn hop⟩

theorem nothing_else : NothingElse := by
  intro K V _ _ s s' b h k hk
  obtain ⟨_, rfl⟩ := ok_of_commit h
  exact write_out hk

theorem undo_restores : UndoRestores := by
  intro K V _ _ s s' b h
  rw [undo_on h (fun _ _ => rfl)]
  congr 1
  funext k
  by_cases hk : k ∈ keys b
  · rw [if_pos hk]
  · rw [if_neg hk]
    obtain ⟨_, rfl⟩ := ok_of_commit h
    exact write_out hk

theorem undo_keeps_later_decisions : UndoKeepsLaterDecisions := by
  intro K V _ _ s s₁ s₂ b later h hl hd
  refine ⟨_, undo_on h (fold_frame later s₁ s₂ hl hd), fun k hk => ?_, fun k hk => ?_⟩
  · simp [hk]
  · simp [hk]

theorem undo_refused_after_change : UndoRefusedAfterChange := by
  intro K V _ _ s s₁ s₂ b h ⟨k, hk, hne⟩
  obtain ⟨⟨hn, _⟩, rfl⟩ := ok_of_commit h
  obtain ⟨op, hop, rfl⟩ := key_cases hk
  apply commit_of_not_ok
  intro hok
  have hinv : (⟨op.key, op.set, op.expect⟩ : Op K V) ∈ inverse b := mem_inverse.mpr ⟨op, hop, rfl⟩
  exact hne ((hok.2 _ hinv).trans (write_in hn hop).symm)

section
variable {K V : Type} [DecidableEq K] [DecidableEq V]

omit [DecidableEq V] in
/-- Whether a batch applies after another one on other items is decided as before it. -/
theorem ok_after {s : State K V} {b c : List (Op K V)} (hd : ∀ k ∈ keys c, k ∉ keys b) : Ok (write s b) c ↔ Ok s c := by
  have same : ∀ op ∈ c, write s b op.key = s op.key := fun op hop => write_out (hd _ (mem_keys hop))
  constructor
  · rintro ⟨hc, he⟩; exact ⟨hc, fun op hop => (same op hop).symm.trans (he op hop)⟩
  · rintro ⟨hc, he⟩; exact ⟨hc, fun op hop => (same op hop).trans (he op hop)⟩

omit [DecidableEq V] in
theorem write_comm {s : State K V} {b₁ b₂ : List (Op K V)} (h₁ : (keys b₁).Nodup) (h₂ : (keys b₂).Nodup)
    (hd : ∀ k ∈ keys b₁, k ∉ keys b₂) : write (write s b₁) b₂ = write (write s b₂) b₁ := by
  funext k
  by_cases k1 : k ∈ keys b₁
  · have k2 : k ∉ keys b₂ := hd k k1
    obtain ⟨op, hop, rfl⟩ := key_cases k1
    rw [write_out k2, write_in h₁ hop, write_in h₁ hop]
  · by_cases k2 : k ∈ keys b₂
    · obtain ⟨op, hop, rfl⟩ := key_cases k2
      rw [write_in h₂ hop, write_out k1, write_in h₂ hop]
    · rw [write_out k2, write_out k1, write_out k1, write_out k2]

theorem bind_commit {s : State K V} {b₁ b₂ : List (Op K V)} (hd : ∀ k ∈ keys b₁, k ∉ keys b₂) :
    ((commit s b₁).bind fun t => commit t b₂) =
      if Ok s b₁ ∧ Ok s b₂ then some (write (write s b₁) b₂) else none := by
  have hd' : ∀ k ∈ keys b₂, k ∉ keys b₁ := fun k k2 k1 => hd k k1 k2
  by_cases o1 : Ok s b₁
  · rw [commit_of_ok o1, Option.bind_some]
    by_cases o2 : Ok s b₂
    · rw [commit_of_ok ((ok_after hd').mpr o2), if_pos ⟨o1, o2⟩]
    · rw [commit_of_not_ok (fun h => o2 ((ok_after hd').mp h)), if_neg (fun h => o2 h.2)]
  · rw [commit_of_not_ok o1, Option.bind_none, if_neg (fun h => o1 h.1)]

end

theorem independent_commute : IndependentCommute := by
  intro K V _ _ s b₁ b₂ hd
  have hd' : ∀ k ∈ keys b₂, k ∉ keys b₁ := fun k k2 k1 => hd k k1 k2
  rw [bind_commit hd, bind_commit hd']
  by_cases h : Ok s b₁ ∧ Ok s b₂
  · rw [if_pos h, if_pos ⟨h.2, h.1⟩, write_comm h.1.1 h.2.1 hd]
  · rw [if_neg h, if_neg (fun h' => h ⟨h'.2, h'.1⟩)]

theorem others_never_stale : OthersNeverStale := by
  intro K V _ _ s s' b c h hd
  obtain ⟨_, rfl⟩ := ok_of_commit h
  have hok := ok_after (s := s) (b := b) hd
  constructor
  · intro hc
    by_cases o : Ok s c
    · rw [commit_of_ok (hok.mpr o)] at hc; cases hc
    · exact commit_of_not_ok o
  · intro hc
    by_cases o : Ok s c
    · rw [commit_of_ok o] at hc; cases hc
    · exact commit_of_not_ok (fun h' => o (hok.mp h'))

end OntologyEX.Proofs.Decisions
