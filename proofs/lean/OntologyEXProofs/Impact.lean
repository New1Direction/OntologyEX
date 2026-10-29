import OntologyEXSpec.Impact

/-! # Proofs: impact standing (untrusted; checked against `OntologyEXSpec/Impact.lean`) -/
namespace OntologyEX.Proofs.Impact
open OntologyEX.Model.Impact OntologyEX.Spec.Impact

theorem minOpt_none {xs : List Nat} : minOpt xs = none ↔ xs = [] := by
  cases xs with
  | nil => simp [minOpt]
  | cons x t => cases h : minOpt t <;> simp [minOpt, h]

theorem minOpt_some {xs : List Nat} {m : Nat} (h : minOpt xs = some m) : m ∈ xs ∧ ∀ x ∈ xs, m ≤ x := by
  induction xs generalizing m with
  | nil => simp [minOpt] at h
  | cons x t ih =>
    cases ht : minOpt t with
    | none =>
      simp only [minOpt, ht, Option.some.injEq] at h
      subst h
      have := minOpt_none.mp ht
      subst this
      simp
    | some m' =>
      simp only [minOpt, ht, Option.some.injEq] at h
      subst h
      obtain ⟨hm, hall⟩ := ih ht
      constructor
      · by_cases hx : x ≤ m'
        · simp [Nat.min_eq_left hx]
        · simp [Nat.min_eq_right (Nat.le_of_lt (Nat.lt_of_not_le hx)), hm]
      · intro y hy
        rcases List.mem_cons.mp hy with rfl | hy
        · exact Nat.min_le_left _ _
        · exact Nat.le_trans (Nat.min_le_right _ _) (hall y hy)

theorem minOpt_le {xs : List Nat} {x : Nat} (hx : x ∈ xs) : ∃ m, minOpt xs = some m ∧ m ≤ x := by
  cases h : minOpt xs with
  | none => exact absurd (minOpt_none.mp h ▸ hx) (List.not_mem_nil)
  | some m => exact ⟨m, rfl, (minOpt_some h).2 x hx⟩

/-- A candidate of `step`: a relationship from `v` whose far end has a chain. -/
theorem mem_step {rs : List Rel} {prev : Nat → Option Nat} {v x : Nat} :
    x ∈ rs.filterMap (fun r => if r.src = v then (prev r.dst).map (fun w => max w r.weak) else none) ↔
      ∃ r ∈ rs, r.src = v ∧ ∃ w, prev r.dst = some w ∧ max w r.weak = x := by
  simp only [List.mem_filterMap]
  constructor
  · rintro ⟨r, hr, he⟩
    by_cases hs : r.src = v
    · simp only [hs, if_true, Option.map_eq_some_iff] at he
      obtain ⟨w, hw, rfl⟩ := he
      exact ⟨r, hr, hs, w, hw, rfl⟩
    · simp [hs] at he
  · rintro ⟨r, hr, hs, w, hw, rfl⟩
    exact ⟨r, hr, by simp [hs, hw]⟩

/-- Every chain is matched or beaten by its layer. -/
theorem chain_layer {rs : List Rel} {o k v w : Nat} (h : Chain rs o k v w) :
    ∃ m, layer rs o k v = some m ∧ m ≤ w := by
  induction h with
  | here => exact ⟨0, by simp [layer], Nat.le_refl 0⟩
  | @hop k u w r hr hd _ ih =>
    obtain ⟨m, hm, hle⟩ := ih
    have hmem : max m r.weak ∈ rs.filterMap
        (fun r' => if r'.src = r.src then (layer rs o k r'.dst).map (fun w => max w r'.weak) else none) :=
      mem_step.mpr ⟨r, hr, rfl, m, hd ▸ hm, rfl⟩
    obtain ⟨m', hm', hle'⟩ := minOpt_le hmem
    refine ⟨m', hm', Nat.le_trans hle' ?_⟩
    exact Nat.max_le.mpr ⟨Nat.le_trans hle (Nat.le_max_left _ _), Nat.le_max_right _ _⟩

/-- What a layer answers is the weakness of a real chain. -/
theorem layer_chain {rs : List Rel} {o : Nat} : ∀ {k v w : Nat}, layer rs o k v = some w → Chain rs o k v w
  | 0, v, w, h => by
    by_cases hv : v = o
    · simp [layer, hv] at h
      subst hv; subst h; exact .here
    · simp [layer, hv] at h
  | k + 1, v, w, h => by
    obtain ⟨hmem, _⟩ := minOpt_some h
    obtain ⟨r, hr, hs, w0, hw0, rfl⟩ := mem_step.mp hmem
    subst hs
    exact .hop r hr rfl (layer_chain hw0)

/-- A layer answers the strongest chain of exactly its hop count. -/
theorem layer_some {rs : List Rel} {o k v w : Nat} (h : layer rs o k v = some w) :
    Chain rs o k v w ∧ ∀ w', Chain rs o k v w' → w ≤ w' := by
  refine ⟨layer_chain h, fun w' hc => ?_⟩
  obtain ⟨m, hm, hle⟩ := chain_layer hc
  rw [h] at hm
  cases hm
  exact hle

theorem layer_none {rs : List Rel} {o k v : Nat} (h : layer rs o k v = none) : ∀ w, ¬ Chain rs o k v w := by
  intro w hc
  obtain ⟨m, hm, _⟩ := chain_layer hc
  rw [h] at hm
  cases hm

theorem strongest_none {rs : List Rel} {o v : Nat} :
    ∀ D, strongest rs o D v = none ↔ ∀ k w, 1 ≤ k → k ≤ D → ¬ Chain rs o k v w
  | 0 => by simp only [strongest, true_iff]; intro k w h1 h2; omega
  | D + 1 => by
    have ih := strongest_none (rs := rs) (o := o) (v := v) D
    constructor
    · intro h k w h1 h2 hc
      cases hs : strongest rs o D v with
      | some b =>
        cases hl : layer rs o (D + 1) v with
        | none => simp [strongest, hs, hl] at h
        | some x => simp only [strongest, hs, hl] at h; split at h <;> simp at h
      | none =>
        cases hl : layer rs o (D + 1) v with
        | some x => simp [strongest, hs, hl] at h
        | none =>
          by_cases hk : k ≤ D
          · exact ih.mp hs k w h1 hk hc
          · have : k = D + 1 := by omega
            subst this
            exact layer_none hl w hc
    · intro h
      have hs : strongest rs o D v = none := ih.mpr (fun k w h1 h2 => h k w h1 (by omega))
      have hl : layer rs o (D + 1) v = none := by
        cases hl : layer rs o (D + 1) v with
        | none => rfl
        | some x => exact absurd (layer_chain hl) (h (D + 1) x (by omega) (Nat.le_refl _))
      simp [strongest, hs, hl]

/-- What `strongest` answers has all three properties of the statement. -/
theorem strongest_sound {rs : List Rel} {o v : Nat} :
    ∀ {D w k : Nat}, strongest rs o D v = some (w, k) →
      (1 ≤ k ∧ k ≤ D ∧ Chain rs o k v w) ∧
      (∀ k' w', 1 ≤ k' → k' ≤ D → Chain rs o k' v w' → w ≤ w') ∧
      (∀ k' w', 1 ≤ k' → k' < k → Chain rs o k' v w' → w < w')
  | 0, w, k, h => by simp [strongest] at h
  | D + 1, w, k, h => by
    cases hs : strongest rs o D v with
    | none =>
      have none_le := (strongest_none D).mp hs
      cases hl : layer rs o (D + 1) v with
      | none => simp [strongest, hs, hl] at h
      | some x =>
        simp only [strongest, hs, hl, Option.some.injEq, Prod.mk.injEq] at h
        obtain ⟨rfl, rfl⟩ := h
        obtain ⟨hc, hmin⟩ := layer_some hl
        refine ⟨⟨by omega, Nat.le_refl _, hc⟩, ?_, ?_⟩
        · intro k' w' h1 h2 hc'
          by_cases hk : k' ≤ D
          · exact absurd hc' (none_le k' w' h1 hk)
          · have : k' = D + 1 := by omega
            subst this; exact hmin w' hc'
        · intro k' w' h1 h2 hc'
          exact absurd hc' (none_le k' w' h1 (by omega))
    | some b =>
      obtain ⟨bw, bk⟩ := b
      obtain ⟨⟨b1, b2, bc⟩, bmin, bshort⟩ := strongest_sound hs
      cases hl : layer rs o (D + 1) v with
      | none =>
        simp only [strongest, hs, hl, Option.some.injEq, Prod.mk.injEq] at h
        obtain ⟨rfl, rfl⟩ := h
        refine ⟨⟨b1, by omega, bc⟩, ?_, bshort⟩
        intro k' w' h1 h2 hc'
        by_cases hk : k' ≤ D
        · exact bmin k' w' h1 hk hc'
        · have : k' = D + 1 := by omega
          subst this; exact absurd hc' (layer_none hl w')
      | some x =>
        obtain ⟨xc, xmin⟩ := layer_some hl
        by_cases hx : x < bw
        · simp only [strongest, hs, hl, hx, if_true, Option.some.injEq, Prod.mk.injEq] at h
          obtain ⟨rfl, rfl⟩ := h
          refine ⟨⟨by omega, Nat.le_refl _, xc⟩, ?_, ?_⟩
          · intro k' w' h1 h2 hc'
            by_cases hk : k' ≤ D
            · exact Nat.le_of_lt (Nat.lt_of_lt_of_le hx (bmin k' w' h1 hk hc'))
            · have : k' = D + 1 := by omega
              subst this; exact xmin w' hc'
          · intro k' w' h1 h2 hc'
            exact Nat.lt_of_lt_of_le hx (bmin k' w' h1 (by omega) hc')
        · simp only [strongest, hs, hl, hx, if_false, Option.some.injEq, Prod.mk.injEq] at h
          obtain ⟨rfl, rfl⟩ := h
          refine ⟨⟨b1, by omega, bc⟩, ?_, bshort⟩
          intro k' w' h1 h2 hc'
          by_cases hk : k' ≤ D
          · exact bmin k' w' h1 hk hc'
          · have : k' = D + 1 := by omega
            subst this
            exact Nat.le_trans (Nat.le_of_not_lt hx) (xmin w' hc')

theorem strongest_correct : StrongestChain := by
  intro rs o D v w k
  constructor
  · exact strongest_sound
  · rintro ⟨⟨k1, k2, kc⟩, kmin, kshort⟩
    cases hs : strongest rs o D v with
    | none => exact absurd kc ((strongest_none D).mp hs k w k1 k2)
    | some b =>
      obtain ⟨bw, bk⟩ := b
      obtain ⟨⟨b1, b2, bc⟩, bmin, bshort⟩ := strongest_sound hs
      have e1 : bw = w := Nat.le_antisymm (bmin k w k1 k2 kc) (kmin bk bw b1 b2 bc)
      subst e1
      have e2 : bk = k := by
        rcases Nat.lt_trichotomy bk k with hlt | heq | hgt
        · exact absurd (kshort bk bw b1 hlt bc) (Nat.lt_irrefl _)
        · exact heq
        · exact absurd (bshort k bw k1 hgt kc) (Nat.lt_irrefl _)
      subst e2
      rfl

theorem nothing_without_a_chain : NothingWithoutAChain := fun _ _ D _ => strongest_none D

/-- Two answers with the same chains are equal: everything `strongest` says is about chains. -/
theorem same_chains {rs rs' : List Rel} {o o' v v' D : Nat}
    (h : ∀ k w, Chain rs o k v w ↔ Chain rs' o' k v' w) : strongest rs o D v = strongest rs' o' D v' := by
  cases hs : strongest rs o D v with
  | none =>
    exact ((strongest_none D).mpr fun k w h1 h2 hc => (strongest_none D).mp hs k w h1 h2 ((h k w).mpr hc)).symm
  | some b =>
    obtain ⟨w, k⟩ := b
    obtain ⟨⟨k1, k2, kc⟩, kmin, kshort⟩ := strongest_sound hs
    exact ((strongest_correct rs' o' D v' w k).mpr
      ⟨⟨k1, k2, (h k w).mp kc⟩, fun k' w' a b c => kmin k' w' a b ((h k' w').mpr c),
       fun k' w' a b c => kshort k' w' a b ((h k' w').mpr c)⟩).symm

theorem chain_mem {rs rs' : List Rel} {o : Nat} (hm : ∀ r, r ∈ rs → r ∈ rs') {k v w : Nat}
    (h : Chain rs o k v w) : Chain rs' o k v w := by
  induction h with
  | here => exact .here
  | hop r hr hd _ ih => exact .hop r (hm r hr) hd ih

theorem order_free : OrderFree := fun _ _ _ _ _ hm =>
  same_chains fun _ _ => ⟨chain_mem (fun r => (hm r).mp), chain_mem (fun r => (hm r).mpr)⟩

def rename (f : Nat → Nat) (r : Rel) : Rel := { r with src := f r.src, dst := f r.dst }

theorem chain_rename {f : Nat → Nat} {rs : List Rel} {o k v w : Nat} (h : Chain rs o k v w) :
    Chain (rs.map (rename f)) (f o) k (f v) w := by
  induction h with
  | here => exact .here
  | hop r hr hd _ ih => exact .hop (rename f r) (List.mem_map_of_mem hr) (by simp [rename, hd]) ih

theorem chain_unrename {f : Nat → Nat} (inj : ∀ a b, f a = f b → a = b) {rs : List Rel} {o k x w : Nat}
    (h : Chain (rs.map (rename f)) (f o) k x w) : ∀ v, x = f v → Chain rs o k v w := by
  induction h with
  | here => intro v hv; rw [inj _ _ hv.symm]; exact .here
  | @hop k u w r' hr hd _ ih =>
    intro v hv
    obtain ⟨r, hr0, rfl⟩ := List.mem_map.mp hr
    have hsrc : r.src = v := inj _ _ hv
    subst hsrc
    exact .hop r hr0 rfl (ih r.dst (by simp [rename] at hd; exact hd.symm))

theorem name_free : NameFree := fun _ inj _ _ _ _ =>
  same_chains fun _ _ => ⟨fun h => chain_unrename inj h _ rfl, fun h => chain_rename h⟩

end OntologyEX.Proofs.Impact
