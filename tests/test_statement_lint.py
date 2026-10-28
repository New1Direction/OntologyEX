"""Advisory statement wording checks: heuristic, non-blocking, no bundled ASD list."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import domain_skill as ds
import statement_lint as sl
from policy_check import DomainError

spec = importlib.util.spec_from_file_location(
    "ontologyex_lint_payments_fixture", ROOT / "examples/payments-domain/demo.py")
demo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = demo
spec.loader.exec_module(demo)


def codes(statement, approved=None, technical=frozenset()):
    return [w["code"] for w in sl.check(statement, approved, set(technical))]


class WordingTests(unittest.TestCase):
    def test_clear_rule_has_no_warnings(self):
        self.assertEqual(codes("The refund amount must not exceed the captured amount."), [])

    def test_ambiguous_helping_verbs(self):
        self.assertEqual(codes("A manager may approve the refund."), ["HELPING_VERB"])
        self.assertEqual(codes("The agent should log the change."), ["HELPING_VERB"])

    def test_semicolon_and_two_sentences(self):
        self.assertIn("SEMICOLON", codes("The flag must be true; the fixture does not check it."))
        self.assertIn("MORE_THAN_ONE_SENTENCE", codes("The flag must be true. The agent sets it."))

    def test_passive_voice(self):
        self.assertIn("PASSIVE_VOICE", codes("Only a captured payment can be refunded."))
        self.assertIn("PASSIVE_VOICE", codes("The request is approved by a manager."))
        self.assertNotIn("PASSIVE_VOICE", codes("The ticket is open."))

    def test_long_sentence(self):
        self.assertIn("SENTENCE_TOO_LONG", codes(" ".join(["word"] * 26) + "."))
        self.assertNotIn("SENTENCE_TOO_LONG", codes(" ".join(["word"] * 25) + "."))

    def test_identifiers_and_code_count_as_one_word(self):
        self.assertEqual(sl.words("Set `amount_minor > 0` for captured_minor."),
                         ["Set", "CODE", "for", "captured_minor."])

    def test_ambiguity_terms(self):
        self.assertIn("AND_OR", codes("Notify the owner and/or the manager."))
        self.assertIn("VAGUE_TERM", codes("Retry the call a reasonable number of times."))

    def test_contraction(self):
        self.assertIn("CONTRACTION", codes("The agent can't change the total."))

    def test_word_list_with_technical_names(self):
        approved = {"the", "must", "not", "be", "more", "than", "amount"}
        self.assertEqual(codes("The refund amount must not be more than the captured_minor.",
                               approved, {"refund"}), [])
        self.assertEqual(codes("The refund amount must not exceed it.", approved, {"refund"}),
                         ["NOT_IN_WORD_LIST", "NOT_IN_WORD_LIST"])


class TargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        source = ROOT / "examples/payments-domain/source"
        cls.workspace = root / "workspace"
        cls.skill = root / demo.NAME
        ds.prepare(source, ["requirements.md", "payments.py"], cls.workspace, demo.NAME, demo.GOAL)
        demo.populate(cls.workspace)
        ds.build(cls.workspace, cls.skill, source)
        cls.words = root / "words.md"
        cls.words.write_text("# Sample\n\nTHE (art)\nMUST (v)\nAMOUNT (n)\nGIVE (v) [GIVES, GAVE, GIVEN]\n")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_workspace_and_skill_agree_and_never_block(self):
        first, second = sl.lint(self.workspace), sl.lint(self.skill)
        self.assertEqual(first["warnings"], second["warnings"])
        self.assertFalse(first["blocking"])
        self.assertEqual(first["statements_checked"], 8)
        found = {(w["id"], w["code"]) for w in first["warnings"]}
        self.assertIn(("captured-only", "PASSIVE_VOICE"), found)
        self.assertIn(("authorization-required", "SEMICOLON"), found)
        self.assertEqual(first["word_list_check"], "NOT_RUN")

    def test_lint_does_not_modify_the_contract(self):
        before = (self.workspace / "60-contract.json").read_bytes()
        sl.lint(self.workspace, self.words)
        self.assertEqual((self.workspace / "60-contract.json").read_bytes(), before)

    def test_ontology_names_are_technical_words(self):
        result = sl.lint(self.workspace, self.words)
        unknown = {w["text"] for w in result["warnings"] if w["code"] == "NOT_IN_WORD_LIST"}
        self.assertEqual(result["word_list_check"], "RUN")
        self.assertNotIn("payment", unknown)  # class Payment
        self.assertNotIn("amounts", unknown)  # plural of an approved noun
        self.assertIn("exceed", unknown)

    def test_word_list_parser(self):
        self.assertEqual(sl.load_word_list(self.words),
                         {"the", "must", "amount", "amounts", "give", "gives", "gave", "given"})
        empty = Path(self.temp.name) / "empty.md"
        empty.write_text("no entries here\n")
        with self.assertRaises(DomainError):
            sl.load_word_list(empty)

    def test_main_reports_json(self):
        import contextlib
        import io
        out = io.StringIO()
        argv = sys.argv
        try:
            sys.argv = ["statement_lint.py", str(self.workspace)]
            with contextlib.redirect_stdout(out):
                code = sl.main()
        finally:
            sys.argv = argv
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "ADVISORY_WARNINGS")


if __name__ == "__main__":
    unittest.main()
