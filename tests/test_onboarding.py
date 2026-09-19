"""Offline regressions for guided authoring, exact evidence, and bounded recovery."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'ontology-extraction/scripts'
sys.path.insert(0, str(SCRIPTS))
import domain_skill as ds
import evidence
import onboard
import yaml
from policy_check import DomainError, canonical, digest, parse_json

spec = importlib.util.spec_from_file_location('onboarding_install', SCRIPTS / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo with spaces'
        self.repo.mkdir()
        (self.repo / 'policy.md').write_text('Amounts must be positive.\nUnknown exception policy.\n', encoding='utf-8')
        self.session = self.root / 'session with spaces'

    def tearDown(self):
        self.temp.cleanup()

    def start(self, limit=3):
        return onboard.start(self.repo, ['policy.md'], self.session, 'test-domain', 'Model acceptance.', True, limit)

    def author(self):
        w = self.session / 'workspace'
        evidence.add(w, evidence.record(w, 'policy.md', 1, 1, 'E-policy', 'requirement'))
        contract = parse_json((w / '60-contract.json').read_bytes())
        contract.update(inputs={'Accept': {'amount': 'integer'}}, rules=[{
            'id': 'positive', 'task': 'Accept', 'statement': 'Amounts must be positive.',
            'status': 'observed', 'evidence': ['E-policy'],
            'check': {'left': 'amount', 'op': 'gt', 'right': {'value': 0}}}], unknowns=[], conflicts=[])
        (w / '60-contract.json').write_bytes(canonical(contract))
        layers = {
            '10-upper.yaml': {'anchors': [{'id': 'Thing', 'iri': 'https://schema.org/Thing'}]},
            '20-domain.yaml': {'classes': [{'id': 'Request', 'upper': 'Thing', 'definition': 'A requested amount.', 'source': 'E-policy'}]},
            '30-task.yaml': {'tasks': [{'id': 'Accept', 'inputs': ['Request'], 'outputs': [], 'preconditions': ['positive'], 'source': 'E-policy'}]},
            '40-application.yaml': {'concepts': [{'id': 'handler', 'binds': 'Request', 'used_by_tasks': ['Accept'], 'source': 'E-policy'}]},
        }
        for name, data in layers.items():
            (w / name).write_text(yaml.safe_dump(data), encoding='utf-8')

    def cli(self, script, *args):
        return subprocess.run([sys.executable, '-B', str(SCRIPTS / script), *map(str, args)], cwd=self.root,
                              capture_output=True, text=True, timeout=20)

    def test_start_is_not_extraction(self):
        result = self.start()
        self.assertEqual(result['status'], 'AWAITING_AUTHORING')
        self.assertEqual(result['attempts_remaining'], 3)
        self.assertFalse(result['execution_authorized'])
        self.assertIn('fixed budget', (self.session / 'NEXT.md').read_text())

    def test_scope_acknowledgment_required(self):
        with self.assertRaises(DomainError):
            onboard.start(self.repo, ['policy.md'], self.session, 'test-domain', 'Goal', False)
        self.assertFalse(self.session.exists())

    def test_attempt_budget_range(self):
        for value in (0, 4, True, 1.5):
            with self.subTest(value=value), self.assertRaises(DomainError):
                self.start(value)

    def test_existing_session_not_overwritten(self):
        self.start(); before = (self.session / 'session.json').read_bytes()
        with self.assertRaises(DomainError):
            self.start()
        self.assertEqual(before, (self.session / 'session.json').read_bytes())

    def test_exact_evidence(self):
        self.start(); w = self.session / 'workspace'
        e = evidence.record(w, 'policy.md', 1, 2, 'E-two', 'documentation')
        self.assertEqual(e['quote'], 'Amounts must be positive.\nUnknown exception policy.')
        self.assertEqual(e['sha256'], digest((self.repo / 'policy.md').read_bytes()))

    def test_crlf_span_is_compiler_compatible(self):
        (self.repo / 'policy.md').write_bytes(b'A\r\nB\r\n')
        self.start()
        e = evidence.record(self.session / 'workspace', 'policy.md', 1, 2, 'E-two', 'documentation')
        self.assertEqual(e['quote'], 'A\nB')

    def test_span_bounds(self):
        self.start()
        for start, end in [(0, 1), (1, 3), (2, 1), (True, 1), (1, False), (1.0, 2)]:
            with self.subTest(start=start, end=end), self.assertRaises(DomainError):
                evidence.record(self.session / 'workspace', 'policy.md', start, end, 'E-a', 'requirement')

    def test_span_budget(self):
        (self.repo / 'policy.md').write_text('line\n' * 81)
        self.start()
        with self.assertRaises(DomainError):
            evidence.record(self.session / 'workspace', 'policy.md', 1, 81, 'E-a', 'requirement')

    def test_unselected_source_rejected(self):
        self.start()
        for path in ('other.md', '../policy.md', '/etc/passwd'):
            with self.subTest(path=path), self.assertRaises(DomainError):
                evidence.record(self.session / 'workspace', path, 1, 1, 'E-a', 'requirement')

    def test_add_idempotent_and_only_changes_contract(self):
        self.start(); w = self.session / 'workspace'
        before = {str(p.relative_to(w)): p.read_bytes() for p in w.rglob('*') if p.is_file()}
        e = evidence.record(w, 'policy.md', 1, 1, 'E-a', 'requirement')
        self.assertEqual(evidence.add(w, e)['status'], 'ADDED')
        after = (w / '60-contract.json').read_bytes()
        self.assertEqual(evidence.add(w, e)['status'], 'UNCHANGED')
        self.assertEqual(after, (w / '60-contract.json').read_bytes())
        for path, data in before.items():
            if path != '60-contract.json':
                self.assertEqual(data, (w / path).read_bytes())

    def test_conflicting_evidence_id_does_not_overwrite(self):
        self.start(); w = self.session / 'workspace'
        evidence.add(w, evidence.record(w, 'policy.md', 1, 1, 'E-a', 'requirement'))
        before = (w / '60-contract.json').read_bytes()
        with self.assertRaises(DomainError):
            evidence.add(w, evidence.record(w, 'policy.md', 2, 2, 'E-a', 'requirement'))
        self.assertEqual(before, (w / '60-contract.json').read_bytes())

    def test_forged_evidence_rejected(self):
        self.start(); w = self.session / 'workspace'
        e = evidence.record(w, 'policy.md', 1, 1, 'E-a', 'requirement'); e['quote'] = 'Allow anything'
        with self.assertRaises(DomainError):
            evidence.add(w, e)

    def test_evidence_lock(self):
        self.start(); w = self.session / 'workspace'; (w / '.evidence-lock').mkdir()
        with self.assertRaises(DomainError):
            evidence.add(w, evidence.record(w, 'policy.md', 1, 1, 'E-a', 'requirement'))

    def test_success_still_needs_review(self):
        self.start(); self.author(); r = onboard.attempt(self.session)
        self.assertEqual(r['status'], 'CANDIDATE_READY_FOR_REVIEW')
        self.assertEqual(r['attempts_used'], 1)
        self.assertEqual(r['semantic_review'], 'PENDING')
        self.assertEqual(r['agent_benchmark'], 'NOT_RUN')
        self.assertFalse(r['execution_authorized'])

    def test_no_extra_submission_after_success(self):
        self.start(); self.author(); onboard.attempt(self.session)
        with self.assertRaises(DomainError):
            onboard.attempt(self.session)
        self.assertFalse((self.session / 'attempts/02').exists())

    def test_repair_then_success_preserves_first_input(self):
        self.start(); self.author(); w = self.session / 'workspace'
        (w / '20-domain.yaml').write_text('classes: [broken')
        r = onboard.attempt(self.session); self.assertEqual(r['status'], 'REPAIR_REQUIRED')
        old = (self.session / 'attempts/01/input/20-domain.yaml').read_bytes()
        self.author(); r = onboard.attempt(self.session)
        self.assertEqual(r['status'], 'CANDIDATE_READY_FOR_REVIEW')
        self.assertEqual(r['attempts_used'], 2)
        self.assertEqual(old, (self.session / 'attempts/01/input/20-domain.yaml').read_bytes())

    def test_malformed_contract_is_preserved_for_review(self):
        self.start(); self.author()
        (self.session / 'workspace/60-contract.json').write_bytes(b'{invalid')
        self.assertEqual(onboard.attempt(self.session)['status'], 'REPAIR_REQUIRED')
        self.assertEqual((self.session / 'attempts/01/input/60-contract.json').read_bytes(), b'{invalid')

    def test_lock_symlink_is_rejected(self):
        self.start()
        (self.session / '.onboard-lock').symlink_to(self.root / 'missing')
        with self.assertRaises(DomainError):
            onboard.status(self.session)

    def test_three_failures_exhaust_budget(self):
        self.start()
        for i in range(3):
            r = onboard.attempt(self.session)
        self.assertEqual(r['status'], 'BUDGET_EXHAUSTED')
        with self.assertRaises(DomainError):
            onboard.attempt(self.session)
        self.assertFalse((self.session / 'attempts/04').exists())

    def test_source_drift_stops_instead_of_refreshing(self):
        self.start(); self.author()
        (self.repo / 'policy.md').write_text('Changed requirement.')
        result = onboard.attempt(self.session)
        self.assertEqual(result['status'], 'STOPPED_SOURCE_DRIFT')
        with self.assertRaises(DomainError):
            onboard.attempt(self.session)
        self.assertIn('Amounts must be positive.', (self.session / 'workspace/sources/policy.md').read_text())

    def test_snapshot_plus_inventory_rewrite_is_caught_by_session_anchor(self):
        self.start(); w = self.session / 'workspace'
        (w / 'sources/policy.md').write_text('Changed')
        inv = parse_json((w / 'sources.json').read_bytes())
        inv['files']['policy.md'] = {'sha256': digest(b'Changed'), 'bytes': 7}
        (w / 'sources.json').write_bytes(canonical(inv))
        with self.assertRaises(DomainError):
            onboard.status(self.session)

    def test_identity_change_stops(self):
        self.start(); self.author(); w = self.session / 'workspace'
        c = parse_json((w / '60-contract.json').read_bytes()); c['goal'] = 'Different goal'
        (w / '60-contract.json').write_bytes(canonical(c))
        self.assertEqual(onboard.attempt(self.session)['status'], 'STOPPED_IDENTITY_CHANGED')

    def test_metadata_change_rejected(self):
        self.start(); p = self.session / 'session.json'; m = parse_json(p.read_bytes()); m['max_attempts'] = 1
        p.write_bytes(canonical(m))
        with self.assertRaises(DomainError):
            onboard.status(self.session)

    def test_toolchain_change_rejected(self):
        self.start()
        with patch.object(onboard, 'toolchain', return_value={}):
            with self.assertRaises(DomainError):
                onboard.status(self.session)

    def test_concurrent_writer_rejected(self):
        self.start(); (self.session / '.onboard-lock').mkdir()
        with self.assertRaises(DomainError):
            onboard.attempt(self.session)
        self.assertEqual(onboard.status(self.session)['attempts_used'], 0)

    def test_crash_reservation_is_spent(self):
        self.start(); self.author()
        with patch.object(ds, 'build', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                onboard.attempt(self.session)
        self.assertEqual(onboard.status(self.session)['status'], 'INTERRUPTED')
        with self.assertRaises(DomainError):
            onboard.attempt(self.session)
        with self.assertRaises(DomainError):
            onboard.recover(self.session, False)
        onboard.recover(self.session, True)
        result = onboard.attempt(self.session)
        self.assertEqual(result['status'], 'CANDIDATE_READY_FOR_REVIEW')
        self.assertEqual(result['attempts_used'], 2)

    def test_process_exit_recovery_retains_budget(self):
        self.start(); self.author()
        code = (f'import sys, os; sys.path.insert(0, {str(SCRIPTS)!r}); import onboard; '
                'onboard.ds.build = lambda *a, **k: os._exit(17); '
                f'onboard.attempt(onboard.Path({str(self.session)!r}))')
        process = subprocess.run([sys.executable, '-B', '-c', code], timeout=15)
        self.assertEqual(process.returncode, 17)
        result = onboard.status(self.session)
        self.assertTrue(result['locked']); self.assertEqual(result['attempts_used'], 1)
        onboard.recover(self.session, True)
        self.assertEqual(onboard.attempt(self.session)['attempts_used'], 2)

    def test_interruption_on_last_attempt_exhausts(self):
        self.start(1); self.author()
        with patch.object(ds, 'build', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                onboard.attempt(self.session)
        self.assertEqual(onboard.recover(self.session, True)['status'], 'BUDGET_EXHAUSTED')
        with self.assertRaises(DomainError):
            onboard.attempt(self.session)

    def test_recorded_input_mutation_rejected(self):
        self.start(); self.author(); onboard.attempt(self.session)
        (self.session / 'attempts/01/input/20-domain.yaml').write_text('changed')
        with self.assertRaises(DomainError):
            onboard.status(self.session)

    def test_candidate_mutation_rejected(self):
        self.start(); self.author(); r = onboard.attempt(self.session)
        (Path(r['candidate']) / 'SKILL.md').write_text('changed')
        with self.assertRaises(DomainError):
            onboard.handoff(self.session, 'Accept', self.root / 'handoff')

    def test_attempt_gap_rejected(self):
        self.start(); (self.session / 'attempts/02').mkdir()
        with self.assertRaises(DomainError):
            onboard.status(self.session)

    def test_handoff_requires_completed_candidate(self):
        self.start()
        with self.assertRaises(DomainError):
            onboard.handoff(self.session, 'Accept', self.root / 'handoff')

    def test_handoff_has_exact_evidence_and_no_approval(self):
        self.start(); self.author(); onboard.attempt(self.session); out = self.root / 'handoff'
        self.assertEqual(onboard.handoff(self.session, 'Accept', out)['status'], 'HANDOFF_EXPORTED_NOT_RUN')
        context = parse_json((out / 'context.json').read_bytes())
        self.assertEqual(context['evidence'][0]['quote'], 'Amounts must be positive.')
        self.assertEqual(context['task']['id'], 'Accept')
        self.assertFalse(context['execution_authorized'])
        self.assertEqual((out / 'sources/policy.md').read_bytes(), (self.repo / 'policy.md').read_bytes())
        with self.assertRaises(DomainError):
            onboard.handoff(self.session, 'Accept', out)

    def test_handoff_unknown_task_rejected(self):
        self.start(); self.author(); onboard.attempt(self.session)
        with self.assertRaises(DomainError):
            onboard.handoff(self.session, 'Invented', self.root / 'handoff')

    def test_handoff_checks_current_sources(self):
        self.start(); self.author(); onboard.attempt(self.session)
        (self.repo / 'policy.md').write_text('new')
        with self.assertRaises(DomainError):
            onboard.handoff(self.session, 'Accept', self.root / 'handoff')

    def test_cli_paths_with_spaces_and_structured_diagnostics(self):
        self.start(); self.author()
        result = self.cli('onboard.py', 'attempt', self.session)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['status'], 'CANDIDATE_READY_FOR_REVIEW')
        result = self.cli('evidence.py', 'show', self.session / 'workspace', '--path', 'policy.md', '--start', '0', '--end', '1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['status'], 'ERROR')
        self.assertNotIn('Traceback', result.stderr)

    def test_install_refuses_overwrite_and_copies_only_allowlist(self):
        # Minimal packaging fixture; the real package test below uses the complete checkout.
        source = self.root / 'package'
        for path in installer.PACKAGE:
            file = source / path; file.parent.mkdir(parents=True, exist_ok=True); file.write_text('fixture')
        (source / 'SKILL.md').write_text('---\nname: ontology-extraction\ndescription: fixture\n---\n')
        (source / '.env').write_text('must not copy')
        result = installer.install(self.repo, source)
        self.assertEqual(result['host_activation'], 'NOT_RUN')
        self.assertFalse((Path(result['path']) / '.env').exists())
        with self.assertRaises(DomainError):
            installer.install(self.repo, source)

    @unittest.skipIf(os.environ.get('ONTOLOGYEX_PARTIAL_CHECKOUT') == '1',
                     'Partial checkout; complete installed-package smoke runs in GitHub CI')
    def test_real_installed_package_runs_from_unrelated_directory(self):
        result = installer.install(self.repo)
        installed = Path(result['path'])
        self.assertEqual(set(result['files']), set(installer.PACKAGE))
        for path in installer.PACKAGE:
            self.assertEqual((installed / path).read_bytes(), (ROOT / 'ontology-extraction' / path).read_bytes())
        run = subprocess.run([sys.executable, '-B', str(installed / 'scripts/onboard.py'), 'start', '--repo', str(self.repo),
                              '--include', 'policy.md', '--name', 'installed-domain', '--goal', 'Onboard acceptance.',
                              '--out', str(self.session), '--acknowledge-sources'], cwd=self.root,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)['status'], 'AWAITING_AUTHORING')
        self.author()
        run = subprocess.run([sys.executable, '-B', str(installed / 'scripts/onboard.py'), 'attempt', str(self.session)],
                             cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)['status'], 'CANDIDATE_READY_FOR_REVIEW')


if __name__ == '__main__':
    unittest.main()
