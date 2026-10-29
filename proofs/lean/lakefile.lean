import Lake
open Lake DSL

package ontologyexProofs where

/-- The code being reasoned about, written in Lean (mirrors the Python and Rust). -/
lean_lib OntologyEXModel where
  globs := #[.submodules `OntologyEXModel]

/-- Trusted: the statements. Pinned in STATEMENTS.json. -/
lean_lib OntologyEXSpec where
  globs := #[.submodules `OntologyEXSpec]

/-- Untrusted: the proofs. tests/lean_statements.py holds each against its statement. -/
@[default_target]
lean_lib OntologyEXProofs where
  globs := #[.one `OntologyEXProofs, .submodules `OntologyEXProofs]

lean_exe conformance where
  root := `Conformance
