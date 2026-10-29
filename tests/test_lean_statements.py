"""The trusted Lean statements stay pinned and apart from the proofs (tests/lean_statements.py)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import lean_statements as ls


class StatementTests(unittest.TestCase):
    def setUp(self):
        self.saved = (ls.LEAN, ls.MANIFEST, ls.CHANGES)
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ls.LEAN, self.tmp / "lean", ignore=shutil.ignore_patterns(".lake"))
        ls.LEAN, ls.MANIFEST, ls.CHANGES = (self.tmp / "lean", self.tmp / "lean/STATEMENTS.json",
                                            self.tmp / "lean/STATEMENTS.md")

    def tearDown(self):
        ls.LEAN, ls.MANIFEST, ls.CHANGES = self.saved
        shutil.rmtree(self.tmp)

    def test_the_checked_in_statements_are_pinned(self):
        ls.LEAN, ls.MANIFEST, ls.CHANGES = self.saved
        self.assertEqual(ls.problems_without_lean(), [])

    def test_a_changed_statement_needs_a_recorded_reason(self):
        spec = ls.LEAN / "OntologyEXSpec/Impact.lean"
        spec.write_text(spec.read_text().replace("w ≤ w'", "w ≤ w' + 1", 1))
        self.assertIn("changed since it was pinned", " ".join(ls.problems_without_lean()))
        self.assertEqual(ls.update("x"), 2)  # a reason must say why
        self.assertEqual(ls.update("Loosened the bound to show the pin at work."), 0)
        self.assertEqual(ls.problems_without_lean(), [])
        self.assertIn("Loosened the bound", ls.CHANGES.read_text())
        entry = json.loads(ls.MANIFEST.read_text())["files"]["OntologyEXSpec/Impact.lean"]
        self.assertEqual(entry["reason"], "Loosened the bound to show the pin at work.")

    def test_statements_do_not_prove_and_proofs_do_not_cheat(self):
        spec = ls.LEAN / "OntologyEXSpec/Impact.lean"
        spec.write_text(spec.read_text() + "\ntheorem easy : True := trivial\n")
        proof = ls.LEAN / "OntologyEXProofs/Impact.lean"
        proof.write_text(proof.read_text() + "\naxiom anything : False\n")
        problems = " ".join(ls.problems_without_lean())
        self.assertIn("must not contain `theorem`", problems)
        self.assertIn("`axiom` is not allowed in a proof", problems)

    def test_a_new_spec_file_must_be_pinned(self):
        (ls.LEAN / "OntologyEXSpec/Extra.lean").write_text("def X : Prop := True\n")
        self.assertIn("not pinned", " ".join(ls.problems_without_lean()))

    def test_the_audit_holds_each_theorem_to_its_statement(self):
        source = ls.audit_source(json.loads(ls.MANIFEST.read_text()))
        self.assertIn("import OntologyEXProofs.Impact", source)
        self.assertIn("example : OntologyEX.Spec.Impact.StrongestChain := OntologyEX.Proofs.Impact.strongest_correct",
                      source)
        self.assertIn("#print axioms OntologyEX.Proofs.Impact.name_free", source)


if __name__ == "__main__":
    unittest.main()
