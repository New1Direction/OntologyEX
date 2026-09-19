"""Operator workflow regressions. Synthetic records/replays are NOT native-agent trials."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / 'examples/cachetools-domain'
sys.path.insert(0, str(CASE))
import pilot
import grade_adapter as grader
import case
import onboard
from policy_check import DomainError, canonical, digest, parse_json, verify_bundle


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve macOS /var symlink for the explicit local-root policy.
        self.root = Path(self.temp.name).resolve()
        self.pilot = self.root / 'pilot with spaces'
        pilot.prepare(self.pilot)
        self.project = self.pilot / 'project'
        self.session = self.project / pilot.SESSION

    def start(self, author=False):
        onboard.start(self.project, list(pilot.SOURCE_PATHS), self.session,
                      'cachetools-native', 'Test replay, not native authoring.', True)
        if author:
            case.model_recipe.author(self.session / 'workspace')
            return onboard.attempt(self.session)

    def record(self):
        value = pilot.record_template()
        value.update(authoring='developer-replay', host='synthetic-test', host_version='test',
                     model='no-model-called', session_id='fixture-session',
                     started_at='2026-01-01T12:00:00+00:00', ended_at='2026-01-01T12:01:00+00:00',
                     permissions='test-only replay, no host execution')
        p, t = self.root / 'record.json', self.root / 'transcript.txt'
        p.write_bytes(canonical(value))
        t.write_text('Synthetic test transcript. No native agent ran.\n')
        return p, t, value

    def collect(self):
        p, t, _ = self.record()
        return pilot.collect(self.pilot, p, t, self.root / 'packet')

    def test_prepare_exact_source_and_package_boundary(self):
        expected = set(pilot.SOURCE_PATHS) | {'.claude/skills/ontology-extraction/' + p for p in pilot.installer.PACKAGE}
        actual = {p.relative_to(self.project).as_posix() for p in self.project.rglob('*') if p.is_file()}
        self.assertEqual(actual, expected)
        self.assertFalse((self.project / 'adapter.py').exists())
        self.assertFalse(self.session.exists())
        self.assertNotIn('acceptance.py', '\n'.join(actual))
        self.assertNotIn('model_recipe.py', '\n'.join(actual))

    def test_prepared_template_and_prompt_are_honest(self):
        template = parse_json((self.pilot / 'operator/run-template.json').read_bytes())
        self.assertEqual(template['authoring'], 'NOT_RUN')
        self.assertIsNone(template['usage']['cost_usd'])
        self.assertIn('Stop for semantic', (self.pilot / 'operator/ONBOARDING-PROMPT.txt').read_text())

    def test_no_recipe_or_library_execution_during_prepare(self):
        with patch.object(case.model_recipe, 'author', side_effect=AssertionError('recipe called')), \
             patch.object(case, 'load_pinned_library', side_effect=AssertionError('library executed')):
            result = pilot.prepare(self.root / 'another')
        self.assertEqual(result['native_host_activation'], 'NOT_RUN')

    def test_existing_pilot_not_overwritten(self):
        before = (self.pilot / 'operator/pilot.json').read_bytes()
        with self.assertRaises(DomainError): pilot.prepare(self.pilot)
        self.assertEqual((self.pilot / 'operator/pilot.json').read_bytes(), before)

    def test_missing_host_is_not_failed_model_trial(self):
        with patch.object(pilot.shutil, 'which', return_value=None):
            result = pilot.status(self.pilot)
        self.assertEqual(result['host_binary'], 'NOT_FOUND')
        self.assertEqual(result['onboarding_state'], 'NOT_STARTED')
        self.assertEqual(result['attempts_used'], 0)

    def test_found_host_does_not_mean_authenticated_or_activated(self):
        with patch.object(pilot.shutil, 'which', return_value='/fake/claude'):
            result = pilot.status(self.pilot)
        self.assertEqual(result['host_binary'], 'FOUND')
        self.assertEqual(result['authentication'], 'NOT_CHECKED')
        self.assertEqual(result['native_host_activation'], 'NOT_VERIFIED')

    def test_source_change_blocks_normal_status(self):
        (self.project / 'TASK.md').write_text('changed')
        with self.assertRaises(DomainError): pilot.status(self.pilot)

    def test_tool_change_blocks_status(self):
        (self.project / '.claude/skills/ontology-extraction/scripts/onboard.py').write_text('changed')
        with self.assertRaises(DomainError): pilot.status(self.pilot)

    def test_prompt_change_blocks_status(self):
        (self.pilot / 'operator/ONBOARDING-PROMPT.txt').write_text('changed')
        with self.assertRaises(DomainError): pilot.status(self.pilot)

    def test_metadata_change_blocks_status(self):
        p = self.pilot / 'operator/pilot.json'
        data = parse_json(p.read_bytes()); data['upstream_commit'] = 'changed'
        p.write_bytes(canonical(data))
        with self.assertRaises(DomainError): pilot.status(self.pilot)

    def test_smaller_source_session_rejected(self):
        onboard.start(self.project, ['TASK.md'], self.session, 'cachetools-native', 'test', True)
        with self.assertRaises(DomainError): pilot.status(self.pilot)

    def test_symlink_source_rejected(self):
        target = self.project / 'TASK.md'; data = target.read_bytes(); target.unlink()
        alternate = self.root / 'task'; alternate.write_bytes(data); target.symlink_to(alternate)
        with self.assertRaises(DomainError): pilot.status(self.pilot)

    def test_traversal_and_symlink_output_rejected(self):
        with self.assertRaises(DomainError): pilot.prepare(self.root / 'outside/../project')
        link = self.root / 'link'; link.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(DomainError): pilot.prepare(link / 'new')

    def test_collect_without_model_claims_retains_unknown_usage(self):
        result = self.collect()
        self.assertEqual(result['onboarding_state'], 'NOT_STARTED')
        self.assertEqual(result['operator_reported_authoring'], 'developer-replay')
        self.assertEqual(result['native_host_activation'], 'NOT_INDEPENDENTLY_VERIFIED')
        self.assertEqual(result['agent_comparison'], 'NOT_RUN')
        self.assertIsNone(result['usage']['cost_usd'])
        self.assertFalse(result['execution_authorized'])

    def test_collect_does_not_promote_claimed_native_run(self):
        p, t, value = self.record(); value['authoring'] = 'native-agent'; p.write_bytes(canonical(value))
        result = pilot.collect(self.pilot, p, t, self.root / 'packet')
        self.assertEqual(result['native_host_activation'], 'NOT_INDEPENDENTLY_VERIFIED')

    def test_collect_completed_candidate_is_verifiable(self):
        state = self.start(author=True)
        result = self.collect()
        bundle = verify_bundle(self.root / 'packet/candidate')
        self.assertEqual(result['candidate_bundle_id'], bundle['bundle_id'])
        self.assertEqual(state['attempts_used'], 1)
        self.assertEqual(result['semantic_review'], 'PENDING')
        self.assertTrue((self.root / 'packet/attempts/01/input/60-contract.json').is_file())

    def test_collect_failed_attempt_retains_invalid_authoring(self):
        self.start()
        (self.session / 'workspace/60-contract.json').write_text('{broken')
        onboard.attempt(self.session)
        result = self.collect()
        self.assertEqual(result['onboarding_state'], 'REPAIR_REQUIRED')
        self.assertEqual((self.root / 'packet/attempts/01/input/60-contract.json').read_text(), '{broken')
        self.assertIsNone(result['candidate_bundle_id'])

    def test_collect_interrupted_attempt(self):
        self.start(); (self.session / 'attempts/01').mkdir()
        result = self.collect()
        self.assertEqual(result['onboarding_state'], 'INTERRUPTED')
        self.assertEqual(result['attempts_used'], 1)

    def test_collect_source_drift_preserves_failure(self):
        self.start(); (self.project / 'TASK.md').write_text('changed')
        onboard.attempt(self.session)
        result = self.collect()
        self.assertEqual(result['onboarding_state'], 'STOPPED_SOURCE_DRIFT')
        self.assertEqual(result['changed_current_sources'], ['TASK.md'])

    def test_collect_missing_current_source_preserves_failure(self):
        (self.project / 'TASK.md').unlink()
        result = self.collect()
        self.assertEqual(result['changed_current_sources'], ['TASK.md'])

    def test_collect_manifest_matches_all_packet_files(self):
        self.collect(); root = self.root / 'packet'
        manifest = parse_json((root / 'packet-manifest.json').read_bytes())
        actual = {p.relative_to(root).as_posix(): digest(p.read_bytes()) for p in root.rglob('*')
                  if p.is_file() and p.name != 'packet-manifest.json'}
        self.assertEqual(actual, manifest['files'])
        self.assertEqual(manifest['packet_id'], digest(canonical(actual)))

    def test_collect_never_overwrites(self):
        self.collect()
        with self.assertRaises(DomainError): self.collect()

    def test_collect_requires_real_metadata_and_nonempty_transcript(self):
        p, t, _ = self.record(); p.write_bytes(canonical(pilot.record_template()))
        with self.assertRaises(DomainError): pilot.collect(self.pilot, p, t, self.root / 'packet')
        p, t, _ = self.record(); t.write_text(' ')
        with self.assertRaises(DomainError): pilot.collect(self.pilot, p, t, self.root / 'packet')

    def test_collect_refuses_operator_packet_inside_project(self):
        p, t, _ = self.record()
        with self.assertRaises(DomainError): pilot.collect(self.pilot, p, t, self.project / 'leak')

    def test_record_validation(self):
        _, _, good = self.record()
        variants = [('ended_at', '2025-01-01T00:00:00Z'), ('started_at', '2026-01-01T00:00:00'),
                    ('schema_version', True), ('model', ''), ('interventions', 'none')]
        for field, value in variants:
            with self.subTest(field=field):
                record = deepcopy(good); record[field] = value
                with self.assertRaises(DomainError): pilot.validate_record(record)
        for field, value in [('input_tokens', True), ('output_tokens', -1), ('cost_usd', float('nan'))]:
            record = deepcopy(good); record['usage'][field] = value
            with self.assertRaises(DomainError): pilot.validate_record(record)

    def test_mismatched_record_attempt_budget_rejected(self):
        self.start(); p, t, value = self.record(); value['budget']['max_compile_submissions'] = 1
        p.write_bytes(canonical(value))
        with self.assertRaises(DomainError): pilot.collect(self.pilot, p, t, self.root / 'packet')

    def test_implementation_requires_acknowledgment_and_actual_task(self):
        self.start(author=True)
        for task, ack in [('ReadBatch', False), ('Invented', True)]:
            with self.assertRaises(DomainError): pilot.implementation(self.pilot, task, self.root / 'impl', ack)

    def test_implementation_has_identical_sources_and_no_author_history(self):
        state = self.start(author=True)
        result = pilot.implementation(self.pilot, 'ReadBatch', self.root / 'impl', True)
        for p in pilot.SOURCE_PATHS:
            self.assertEqual((self.root / 'impl' / p).read_bytes(), (self.project / p).read_bytes())
        self.assertEqual(result['candidate_bundle_id'], verify_bundle(Path(state['candidate']))['bundle_id'])
        self.assertEqual(result['candidate_bundle_id'], verify_bundle(self.root / 'impl/domain-skill')['bundle_id'])
        paths = [p.name for p in (self.root / 'impl').rglob('*') if p.is_file()]
        for forbidden in ('adapter.py', 'acceptance.py', 'model_recipe.py', 'reference_adapter.py', 'session.json', 'transcript.txt'):
            self.assertNotIn(forbidden, paths)

    def test_implementation_source_drift_or_unfinished_blocked(self):
        with self.assertRaises(DomainError): pilot.implementation(self.pilot, 'ReadBatch', self.root / 'impl', True)
        self.start(author=True); (self.project / 'TASK.md').write_text('changed')
        with self.assertRaises(DomainError): pilot.implementation(self.pilot, 'ReadBatch', self.root / 'impl', True)

    def test_implementation_grade_is_bound_to_candidate(self):
        state = self.start(author=True)
        pilot.implementation(self.pilot, 'ReadBatch', self.root / 'impl', True)
        submitted = self.root / 'impl/adapter.py'
        submitted.write_bytes((CASE / 'reference_adapter.py').read_bytes())
        result = grader.grade(self.root / 'impl', submitted, self.root / 'grade', True)
        self.assertEqual(result['status'], 'TESTS_PASS')
        self.assertEqual(result['candidate_bundle_id'], verify_bundle(Path(state['candidate']))['bundle_id'])

    def test_implementation_grade_rejects_mismatched_candidate(self):
        self.start(author=True)
        pilot.implementation(self.pilot, 'ReadBatch', self.root / 'impl', True)
        p = self.root / 'impl/PILOT-CONTEXT.json'
        context = parse_json(p.read_bytes()); context['candidate_bundle_id'] = '0' * 64
        p.write_bytes(canonical(context))
        submitted = self.root / 'impl/adapter.py'
        submitted.write_bytes((CASE / 'reference_adapter.py').read_bytes())
        with self.assertRaises(DomainError): grader.grade(self.root / 'impl', submitted, self.root / 'grade', True)
        self.assertFalse((self.root / 'grade').exists())

    def test_aliased_pilot_path_cannot_leak_operator_packet(self):
        (self.root / 'extra').mkdir()
        p, t, _ = self.record()
        aliased = self.root / 'extra' / '..' / self.pilot.name
        with self.assertRaises(DomainError): pilot.collect(aliased, p, t, self.project / 'leak')

    def test_cli_from_unrelated_directory(self):
        for args in [('status', self.pilot), ('prepare', '--out', self.root / 'another pilot')]:
            result = subprocess.run([sys.executable, '-B', str(CASE / 'pilot.py'), *map(str, args)],
                cwd=self.root, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('status', json.loads(result.stdout))


@unittest.skipUnless(os.name == 'posix', 'bounded grader supports POSIX only')
class GraderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'source project'
        sources, _ = case.source_files(); case.ds.write_files(self.project, sources)
        self.adapter = self.root / 'adapter.py'
        self.adapter.write_bytes((CASE / 'reference_adapter.py').read_bytes())
        self.output = self.root / 'grade result'

    def run_grade(self, code=None, timeout=15):
        if code is not None: self.adapter.write_text(code)
        return grader.grade(self.project, self.adapter, self.output, True, timeout)

    def test_reference_passes_all_existing_cases(self):
        result = self.run_grade()
        self.assertEqual(result['status'], 'TESTS_PASS')
        self.assertEqual(result['tests']['passed'], 18)
        self.assertEqual(result['agent_comparison'], 'NOT_RUN')
        self.assertEqual(result['adapter_sha256'], digest(self.adapter.read_bytes()))
        self.assertEqual((self.output / 'input/adapter.py').read_bytes(), self.adapter.read_bytes())

    def test_wrong_adapter_is_failure(self):
        result = self.run_grade('def get_many(cache, keys):\n    return {}, []\n')
        self.assertEqual(result['status'], 'TESTS_FAIL')
        self.assertLess(result['tests']['passed'], 18)

    def test_submission_import_exception_is_retained(self):
        result = self.run_grade('raise RuntimeError("bad submission")\n')
        self.assertEqual(result['status'], 'SUBMISSION_ERROR')
        self.assertTrue((self.output / 'summary.json').is_file())
        self.assertIn('bad submission', result['error'])

    def test_syntax_failure_is_not_input_success(self):
        result = self.run_grade('def bad syntax')
        self.assertEqual(result['status'], 'SUBMISSION_ERROR')

    def test_timeout_is_bounded_and_recorded(self):
        result = self.run_grade('while True: pass\n', timeout=1)
        self.assertEqual(result['status'], 'TIMEOUT')
        self.assertIsNone(result['tests'])
        self.assertEqual(result['process_returncode'], -9)

    def test_exit_without_result_is_failure(self):
        result = self.run_grade('import os; os._exit(0)\n')
        self.assertEqual(result['status'], 'INVALID_GRADER_OUTPUT')

    def test_parent_secrets_not_in_child_environment(self):
        code = 'import os\nassert "ONTOLOGYEX_TEST_SECRET" not in os.environ\n' + self.adapter.read_text()
        with patch.dict(os.environ, {'ONTOLOGYEX_TEST_SECRET': 'should-not-leave-parent'}):
            result = self.run_grade(code)
        self.assertEqual(result['status'], 'TESTS_PASS')

    def test_parent_python_optimization_cannot_disable_acceptance_asserts(self):
        with patch.dict(os.environ, {'PYTHONOPTIMIZE': '1'}):
            result = self.run_grade('def get_many(cache, keys): return {}, []')
        self.assertEqual(result['status'], 'TESTS_FAIL')

    def test_no_execution_without_ack(self):
        self.adapter.write_text('raise RuntimeError("must not execute")')
        with self.assertRaises(DomainError): grader.grade(self.project, self.adapter, self.output, False)
        self.assertFalse(self.output.exists())

    def test_source_tampering_rejected_before_execution(self):
        (self.project / 'TASK.md').write_text('changed')
        with self.assertRaises(DomainError): self.run_grade()
        self.assertFalse(self.output.exists())

    def test_symlink_submission_rejected(self):
        link = self.root / 'link.py'; link.symlink_to(self.adapter)
        with self.assertRaises(DomainError): grader.grade(self.project, link, self.output, True)

    def test_output_inside_agent_project_rejected(self):
        with self.assertRaises(DomainError): grader.grade(self.project, self.adapter, self.project / 'grader', True)

    def test_duplicate_run_directory_not_replayed(self):
        self.run_grade()
        with self.assertRaises(DomainError): self.run_grade()

    def test_timeout_range(self):
        for value in (0, 31, True, 1.5):
            with self.subTest(value=value), self.assertRaises(DomainError): self.run_grade(timeout=value)

    def test_case_result_schema_rejects_missing_and_forged_counts(self):
        bad = {'passed': 0, 'total': 0, 'cases': []}
        with self.assertRaises(DomainError): grader.validate_tests(bad)
        good = {'passed': 18, 'total': 18, 'cases': [{'name': n, 'status': 'PASS'} for n in grader.CASE_NAMES]}
        for key, value in [('passed', True), ('passed', 17), ('total', 0), ('cases', [None] * 18)]:
            with self.subTest(key=key):
                data = deepcopy(good); data[key] = value
                with self.assertRaises(DomainError): grader.validate_tests(data)

    def test_input_snapshot_change_discards_success(self):
        code = 'from pathlib import Path\n(Path(__file__).parents[1] / "input/adapter.py").write_text("tampered")\n' + self.adapter.read_text()
        result = self.run_grade(code)
        self.assertEqual(result['status'], 'INPUT_CHANGED')
        self.assertIsNone(result['tests'])

    def test_log_output_is_bounded(self):
        result = self.run_grade('print("x" * 200000)\n')
        self.assertNotEqual(result['status'], 'TESTS_PASS')
        self.assertLessEqual((self.output / 'stdout.log').stat().st_size, grader.LOG_LIMIT)

    def test_cli_exit_codes(self):
        for index, (ack, content, expected) in enumerate([
            (False, self.adapter.read_text(), 1), (True, self.adapter.read_text(), 0),
            (True, 'def get_many(cache, keys): return {}, []', 2)]):
            self.adapter.write_text(content)
            command = [sys.executable, '-B', str(CASE / 'grade_adapter.py'), '--source-project', str(self.project),
                       '--adapter', str(self.adapter), '--out', str(self.root / f'run{index}')]
            if ack: command.append('--acknowledge-code-execution')
            result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
            self.assertIn('status', json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
