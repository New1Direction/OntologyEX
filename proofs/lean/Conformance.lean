import OntologyEXProofs

/-! Prints the rollup, ladder, fusion, path-standing, impact and feeds tables from the proven definitions, in
the formats of tests/conformance.py and tests/feeds_conformance.py, so CI can compare
them with Python and Rust. -/

open OntologyEX.Business

def healthName : ActionHealth → String
  | .atRisk => "AT_RISK" | .possiblyImpacted => "POSSIBLY_IMPACTED" | .degraded => "DEGRADED"
  | .stale => "STALE" | .unknown => "UNKNOWN" | .unlinked => "UNLINKED" | .reportedOk => "REPORTED_OK"

def gradeName : Grade → String
  | .g0 => "G0" | .g1 => "G1" | .g2 => "G2" | .g3 => "G3" | .g4 => "G4"

def reasonName : Reason → String
  | .rejected => "REJECTED" | .notReviewed => "NOT_REVIEWED" | .inferred => "INFERRED"
  | .oneKind => "ONE_KIND" | .checkFailed => "CHECK_FAILED" | .notExercised => "NOT_EXERCISED"
  | .noG4Producer => "NO_G4_PRODUCER"

def values : List Sys :=
  [⟨.ok, false⟩, ⟨.ok, true⟩, ⟨.warning, false⟩, ⟨.warning, true⟩, ⟨.error, false⟩,
   ⟨.error, true⟩, ⟨.stale, false⟩, ⟨.stale, true⟩, ⟨.unknown, false⟩, ⟨.unknown, true⟩]

def natList (xs : List Nat) : String := "[" ++ ", ".intercalate (xs.map toString) ++ "]"

/-- Every index sequence over `base` values of exactly length n, last position fastest. -/
def seqs (base : Nat) : Nat → List (List Nat)
  | 0 => [[]]
  | n + 1 => (seqs base n).foldr (fun p acc => (List.range base).map (fun i => p ++ [i]) ++ acc) []

def shownName : OntologyEX.Fusion.Shown → String
  | .ok => "ok" | .warning => "warning" | .error => "error" | .stale => "stale" | .unknown => "unknown"

def samples : List OntologyEX.Fusion.Sample :=
  [⟨.ok, false⟩, ⟨.ok, true⟩, ⟨.warning, false⟩, ⟨.warning, true⟩, ⟨.error, false⟩, ⟨.error, true⟩]

def fusionRow (seq : List Nat) : String :=
  let xs := seq.map (fun i => samples.getD i ⟨.ok, false⟩)
  "[" ++ natList seq ++ ", \"" ++ shownName (OntologyEX.Fusion.fuse xs) ++ "\"]"

def rollupRow (seq : List Nat) : String :=
  let systems := seq.map (fun i => values.getD i ⟨.ok, false⟩)
  "[" ++ natList seq ++ ", \"" ++ healthName (rollup systems) ++ "\"]"

def gradeRow (bits : Nat) : String :=
  let f := (List.range 6).map (fun k => (bits.testBit (5 - k)))
  let (g, r) := ladder (f.getD 0 false) (f.getD 1 false) (f.getD 2 false)
    (f.getD 3 false) (f.getD 4 false) (f.getD 5 false)
  "[" ++ natList (f.map (fun b => if b then 1 else 0)) ++ ", \"" ++ gradeName g ++ "\", \"" ++
    reasonName r ++ "\"]"

def statusName : OntologyEX.Feeds.Status → String
  | .ok => "ok" | .warning => "warning" | .error => "error"

/-- Index 0..24: each of two metrics absent (0) or value 0..3 (1..4). -/
def metricPairs (i : Nat) : List (Nat × Nat) :=
  (if i / 5 = 0 then [] else [(0, i / 5 - 1)]) ++ (if i % 5 = 0 then [] else [(1, i % 5 - 1)])

def classifyRows : List String :=
  (List.range 25).flatMap fun m => (List.range 25).flatMap fun w => (List.range 25).flatMap fun e =>
    [false, true].map fun d =>
      "\"" ++ statusName (OntologyEX.Feeds.classify (metricPairs m) (metricPairs w) (metricPairs e) d) ++ "\""

def retainRows : List String :=
  (List.range 6).flatMap fun n => (seqs 4 n).flatMap fun xs =>
    [2, 3, 4].map fun k => natList (OntologyEX.Feeds.keepNewest k xs)

def p95Rows : List String :=
  (List.range 100000).map fun i => toString (OntologyEX.Feeds.p95Index (i + 1))

def standingName : OntologyEX.Context.Standing → String
  | .confirmed => "CONFIRMED" | .source => "SOURCE" | .inferred => "INFERRED" | .unknown => "UNKNOWN"

def standings : List OntologyEX.Context.Standing := [.confirmed, .source, .inferred, .unknown]

def pathRow (seq : List Nat) : String :=
  let hs := seq.map (fun i => standings.getD i .confirmed)
  "[" ++ natList seq ++ ", \"" ++ standingName (OntologyEX.Context.pathStanding hs) ++ "\"]"

/-- Recorded evidence 0 (none), 1 or 2 (values 0 or 1) against evidence now 0 or 1. -/
def watermarkRow (c : Nat × Nat) : String :=
  let recorded : Option Nat := if c.1 = 0 then none else some (c.1 - 1)
  "[" ++ natList [c.1, c.2] ++ ", \"" ++ standingName (OntologyEX.Context.linkStanding recorded c.2) ++ "\"]"

/-! Impact: 300 generated graphs (the same generator as tests/conformance.py), and for each
entity but the origin the `(weakness, hops)` that `Model.Impact.strongest` answers within 4 hops. -/

def draw (m : Nat) : StateM Nat Nat := do
  let s := ((← get) * 1103515245 + 12345) % 2147483648
  set s
  pure (s / 65536 % m)

def graph : StateM Nat (Nat × List OntologyEX.Model.Impact.Rel) := do
  let n := 2 + (← draw 5)
  let e ← draw 9
  let mut rs : List OntologyEX.Model.Impact.Rel := []
  for _ in [0:e] do
    let s ← draw n
    let d ← draw n
    let w ← draw 3
    rs := rs ++ [⟨s, d, w⟩]
  pure (n, rs)

def impactRow (i : Nat) : String :=
  let (n, rs) := (graph.run (i + 1)).1
  let edges := rs.map fun r => natList [r.src, r.dst, r.weak]
  let found := (List.range n).tail.map fun v =>
    match OntologyEX.Model.Impact.strongest rs 0 4 v with
    | some (w, k) => natList [v, w, k]
    | none => natList [v]
  "[[" ++ ", ".intercalate edges ++ "], [" ++ ", ".intercalate found ++ "]]"

def main : IO Unit := do
  let rows := (seqs 10 0 ++ seqs 10 1 ++ seqs 10 2 ++ seqs 10 3).map rollupRow
  let grades := (List.range 64).map gradeRow
  let fusion := (seqs 6 0 ++ seqs 6 1 ++ seqs 6 2 ++ seqs 6 3).map fusionRow
  let paths := (seqs 4 0 ++ seqs 4 1 ++ seqs 4 2 ++ seqs 4 3 ++ seqs 4 4).map pathRow
  let marks := [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)].map watermarkRow
  let impact := (List.range 300).map impactRow
  IO.println ("{\"kind\": \"BUSINESS_CONFORMANCE_TABLE\", \"execution_authorized\": false, " ++
    "\"rollup\": [" ++ ", ".intercalate rows ++ "], \"grade\": [" ++
    ", ".intercalate grades ++ "], \"fusion\": [" ++ ", ".intercalate fusion ++ "], " ++
    "\"path_standing\": [" ++ ", ".intercalate paths ++ "], " ++
    "\"watermark\": [" ++ ", ".intercalate marks ++ "], " ++
    "\"impact\": [" ++ ", ".intercalate impact ++ "], " ++
    "\"classify\": [" ++ ", ".intercalate classifyRows ++ "], \"retain\": [" ++
    ", ".intercalate retainRows ++ "], \"p95\": [" ++ ", ".intercalate p95Rows ++ "]}")
