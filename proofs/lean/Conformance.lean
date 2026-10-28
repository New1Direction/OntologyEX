import OntologyEXProofs

/-! Prints the rollup and ladder tables from the proven definitions, in the format
of tests/conformance.py, so CI can compare them with Python and Rust. -/

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

/-- Every index sequence of exactly length n, last position varying fastest. -/
def seqs : Nat → List (List Nat)
  | 0 => [[]]
  | n + 1 => (seqs n).foldr (fun p acc => (List.range 10).map (fun i => p ++ [i]) ++ acc) []

def natList (xs : List Nat) : String := "[" ++ ", ".intercalate (xs.map toString) ++ "]"

def rollupRow (seq : List Nat) : String :=
  let systems := seq.map (fun i => values.getD i ⟨.ok, false⟩)
  "[" ++ natList seq ++ ", \"" ++ healthName (rollup systems) ++ "\"]"

def gradeRow (bits : Nat) : String :=
  let f := (List.range 6).map (fun k => (bits.testBit (5 - k)))
  let (g, r) := ladder (f.getD 0 false) (f.getD 1 false) (f.getD 2 false)
    (f.getD 3 false) (f.getD 4 false) (f.getD 5 false)
  "[" ++ natList (f.map (fun b => if b then 1 else 0)) ++ ", \"" ++ gradeName g ++ "\", \"" ++
    reasonName r ++ "\"]"

def main : IO Unit := do
  let rows := (seqs 0 ++ seqs 1 ++ seqs 2 ++ seqs 3).map rollupRow
  let grades := (List.range 64).map gradeRow
  IO.println ("{\"kind\": \"BUSINESS_CONFORMANCE_TABLE\", \"execution_authorized\": false, " ++
    "\"rollup\": [" ++ ", ".intercalate rows ++ "], \"grade\": [" ++
    ", ".intercalate grades ++ "]}")
