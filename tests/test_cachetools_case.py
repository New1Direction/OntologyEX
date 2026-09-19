"""Real pinned-codebase mechanics, not fresh model evaluation."""
from pathlib import Path
import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / 'examples/cachetools-domain'
sys.path.insert(0, str(CASE))
spec = importlib.util.spec_from_file_location('cachetools_onboarding_case', CASE / 'case.py')
case = importlib.util.module_from_spec(spec)
spec.loader.exec_module(case)
from policy_check import DomainError, parse_json, evaluate


class CachetoolsCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.temp.name) / 'case'
        cls.summary = case.run_case(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_behavioral_checks_pass(self):
        self.assertEqual(self.summary['behavioral_checks']['passed'], 18)
        self.assertEqual(self.summary['behavioral_checks']['total'], 18)
        self.assertTrue(self.summary['upstream_bytes_verified'])

    def test_no_fabricated_agent_success(self):
        self.assertEqual(self.summary['agent_comparison'], 'NOT_RUN')
        self.assertEqual(self.summary['native_host_activation'], 'NOT_RUN')
        self.assertEqual(self.summary['authoring'], 'DEVELOPER_AUTHORED_MODEL_REPLAY')
        self.assertEqual(self.summary['solution'], 'DEVELOPER_REFERENCE_ADAPTER')

    def test_complex_rules_are_not_forced_into_integer_checks(self):
        model = parse_json((Path(self.summary['candidate']) / 'model.json').read_bytes())
        self.assertTrue(all(r['check'] is None for r in model['rules']))
        self.assertEqual(evaluate(model, 'ReadBatch', {})['status'], 'NEEDS_REVIEW')

    def test_trials_have_identical_source_information(self):
        manifests = []
        for variant in ('raw', 'markdown', 'domain-skill'):
            root = self.out / 'trials' / variant
            manifests.append({p.relative_to(root / 'sources').as_posix(): p.read_bytes()
                              for p in (root / 'sources').rglob('*') if p.is_file()})
            paths = [str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()]
            self.assertFalse(any('acceptance.py' in p or 'reference_adapter.py' in p or
                                 'model_recipe.py' in p or 'summary.json' in p for p in paths))
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(manifests[1], manifests[2])

    def test_pin_mismatch_rejected(self):
        with patch.object(case, 'digest', return_value='not-the-pinned-digest'):
            with self.assertRaises(DomainError):
                case.source_files()

    def test_acceptance_detects_a_truthiness_mutant(self):
        library = case.load_pinned_library(self.out / 'source')
        def mutant(cache, keys):
            hits, misses = {}, []
            for key in keys:
                value = cache.get(key)
                if value:
                    hits[key] = value
                else:
                    misses.append(key)
            return hits, misses
        result = case.acceptance.run(mutant, library)
        self.assertLess(result['passed'], result['total'])
        self.assertEqual(next(r['status'] for r in result['cases'] if r['name'] == 'falsy_values'), 'FAIL')

    def test_case_cannot_overwrite_existing_outputs(self):
        with self.assertRaises(DomainError):
            case.run_case(self.out)


if __name__ == '__main__':
    unittest.main()
