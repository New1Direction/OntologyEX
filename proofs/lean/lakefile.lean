import Lake
open Lake DSL

package ontologyexProofs where

@[default_target]
lean_lib OntologyEXProofs where

lean_exe conformance where
  root := `Conformance
