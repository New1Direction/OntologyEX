#!/usr/bin/env python3
"""Grade one reviewed adapter with the existing acceptance cases in a bounded child process.

This is NOT an OS sandbox or a hostile-submission grader. Use a disposable, externally
isolated environment without credentials. Nothing runs without explicit acknowledgment.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys

import case
import domain_skill as ds
import onboard
from policy_check import DomainError, MAX_TOTAL, canonical, digest, keys, parse_json, require, safe_file, verify_bundle

CASE_NAMES = ('live_hit', 'absent', 'expiry_equality', 'fractional_clock', 'falsy_values',
              'miss_order_duplicates', 'generator', 'mixed_expiration', 'read_recency',
              'no_eager_deletion', 'unhashable_key', 'other_exception', 'no_ttl_refresh',
              'datetime_clock', 'per_read_clock', 'invalid_iterable', 'one_read_per_occurrence', 'empty_no_reads')
LOG_LIMIT = 65536
WORKER = r'''import importlib.util
import json
import os
from pathlib import Path
import resource
import sys

# Hard limits apply before importing the submission. They are not filesystem/network isolation.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
# RLIMIT_AS is unavailable or behaves differently on some supported POSIX hosts.
if sys.platform.startswith('linux'):
    resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))
root = Path(__file__).parent

def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path,
        submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

try:
    library = load('cachetools', root / 'sources/src/cachetools/__init__.py', True)
    acceptance = load('_ontologyex_acceptance', root / 'acceptance.py')
    adapter = load('_ontologyex_submission', root / 'adapter.py')
    result = {'status': 'GRADED', 'tests': acceptance.run(adapter.get_many, library)}
except BaseException as exc:
    result = {'status': 'SUBMISSION_ERROR', 'error': f'{type(exc).__name__}: {str(exc)[:2000]}'}
(root / 'result.json').write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
'''


def validate_tests(result: dict) -> dict:
    keys(result, {'passed', 'total', 'cases'}, 'test results')
    require(type(result['total']) is int and result['total'] == len(CASE_NAMES), 'incomplete case count')
    require(type(result['cases']) is list and len(result['cases']) == len(CASE_NAMES), 'incomplete cases')
    require([r.get('name') if type(r) is dict else None for r in result['cases']] == list(CASE_NAMES), 'unexpected case identities/order')
    for item in result['cases']:
        require(item.get('status') in ('PASS', 'FAIL'), 'invalid case status')
        keys(item, {'name', 'status'} | ({'error'} if 'error' in item else set()), 'case result')
        if 'error' in item:
            require(type(item['error']) is str and len(item['error']) <= LOG_LIMIT, 'invalid case diagnostic')
    count = sum(item['status'] == 'PASS' for item in result['cases'])
    require(type(result['passed']) is int and result['passed'] == count, 'incorrect pass count')
    return result


def grade(source: Path, adapter: Path, out: Path, acknowledged: bool, timeout: int = 15) -> dict:
    require(acknowledged is True, 'review the Python submission and pass --acknowledge-code-execution inside a disposable environment')
    require(os.name == 'posix', 'bounded grader requires POSIX (Linux/macOS); use a disposable Linux environment on Windows')
    require(type(timeout) is int and 1 <= timeout <= 30, 'timeout must be 1-30 seconds')
    source = onboard.local_root(source).resolve()
    adapter = adapter.absolute()
    onboard.local_root(adapter.parent)
    require(adapter.suffix == '.py', 'submission must be a .py file')
    data = safe_file(adapter.parent, adapter.name)
    require(b'\0' not in data and bool(data.decode('utf-8').strip()), 'submission must be nonempty UTF-8 Python')
    out = out.absolute()
    require('..' not in out.parts and not any(p.is_symlink() for p in (out, *out.parents)),
            'grading output cannot use traversal or symlinks')
    require(not out.is_relative_to(source), 'grading files must be outside the agent source project')
    require(not out.exists() and not out.is_symlink(), 'grade output exists; never overwrite or silently replay an attempt')
    sources, pin = case.source_files()
    for path, original in sources.items():
        require(safe_file(source, path) == original, f'grading source differs from pin: {path}')
    candidate_id = None
    context_path = source / 'PILOT-CONTEXT.json'
    if context_path.exists() or context_path.is_symlink():
        context = parse_json(safe_file(source, 'PILOT-CONTEXT.json'))
        keys(context, {'schema_version', 'pilot_id', 'candidate_bundle_id', 'task',
                       'review_acknowledged_for_local_pilot', 'reviewer_identity', 'model_semantics',
                       'native_host_activation', 'execution_authorized'}, 'pilot context')
        require(type(context['schema_version']) is int and context['schema_version'] == 1, 'invalid context schema')
        require(context['review_acknowledged_for_local_pilot'] is True and context['execution_authorized'] is False,
                'invalid local-pilot review boundary')
        bundle = verify_bundle(source / 'domain-skill')
        require(bundle['bundle_id'] == context['candidate_bundle_id'], 'implementation context does not match the skill')
        model = parse_json(safe_file(source / 'domain-skill', 'model.json', MAX_TOTAL))
        require(context['task'] in model['inputs'], 'context names unknown task')
        require(model['source_files'] == {p: {'sha256': digest(b), 'bytes': len(b)} for p, b in sources.items()},
                'candidate uses a different source set')
        candidate_id = bundle['bundle_id']
    tests = safe_file(case.HERE, 'acceptance.py')
    # Retain snapshots before execution. There is no import of adapter.py in the parent process.
    files = {'input/adapter.py': data, 'input/acceptance.py': tests}
    files.update({f'input/sources/{p}': b for p, b in sources.items()})
    files['request.json'] = canonical({'schema_version': 1, 'status': 'RESERVED',
        'adapter_sha256': digest(data), 'acceptance_sha256': digest(tests),
        'source_files': {p: digest(b) for p, b in sorted(sources.items())},
        'upstream_commit': pin['commit'], 'timeout_seconds': timeout,
        'code_execution_acknowledged': True, 'candidate_bundle_id': candidate_id, 'os_sandbox': False})
    ds.write_files(out, files)
    # Execute disposable copies, not the immutable input snapshots or author's original file.
    payload = {p.removeprefix('input/'): b for p, b in files.items() if p.startswith('input/')}
    payload['worker.py'] = WORKER.encode()
    ds.write_files(out / 'execution', payload)
    work = out / 'execution'
    env = {'PATH': os.defpath, 'HOME': str(work), 'TMPDIR': str(work), 'TMP': str(work),
           'TEMP': str(work), 'LANG': 'C.UTF-8'}
    execution_status, returncode = 'FINISHED', None
    with (out / 'stdout.log').open('xb') as stdout, (out / 'stderr.log').open('xb') as stderr:
        process = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(work / 'worker.py')],
                                   cwd=work, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            execution_status = 'TIMEOUT'
        finally:
            # Clean up cooperative descendants too. Hostile children can escape process groups;
            # a real OS sandbox is the operator's responsibility.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            returncode = process.returncode
    outcome = None
    error = None
    if execution_status == 'TIMEOUT':
        state = 'TIMEOUT'
    elif returncode != 0:
        state = 'PROCESS_ERROR'
    else:
        try:
            result = parse_json(safe_file(work, 'result.json', LOG_LIMIT))
            require(type(result) is dict, 'invalid grader envelope')
            if result.get('status') == 'SUBMISSION_ERROR':
                keys(result, {'status', 'error'}, 'submission error')
                require(type(result['error']) is str, 'invalid submission error')
                state, error = 'SUBMISSION_ERROR', result['error'][:2000]
            else:
                keys(result, {'status', 'tests'}, 'grader envelope')
                require(result['status'] == 'GRADED', 'unexpected grader status')
                outcome = validate_tests(result['tests'])
                state = 'TESTS_PASS' if outcome['passed'] == outcome['total'] else 'TESTS_FAIL'
        except (DomainError, OSError, ValueError, TypeError, KeyError) as exc:
            state, error = 'INVALID_GRADER_OUTPUT', str(exc)[:2000]
    # If immutable snapshots changed, discard any success result. Not an anti-tamper sandbox.
    try:
        require(all(safe_file(out, p, MAX_TOTAL) == b for p, b in files.items()), 'retained input changed during execution')
    except (DomainError, OSError) as exc:
        state, error, outcome = 'INPUT_CHANGED', str(exc)[:2000], None
    summary = {'schema_version': 1, 'status': state, 'tests': outcome, 'error': error,
               'candidate_bundle_id': candidate_id,
               'adapter_sha256': digest(data), 'acceptance_sha256': digest(tests),
               'source_files': {p: digest(b) for p, b in sorted(sources.items())},
               'upstream_commit': pin['commit'], 'timeout_seconds': timeout,
               'process_returncode': returncode,
               'native_host_activation': 'NOT_VERIFIED', 'agent_comparison': 'NOT_RUN',
               'semantic_review': 'NOT_CERTIFIED', 'execution_authorized': False,
               'isolation': 'Child process, minimal environment, POSIX resource limits. NOT an OS sandbox.',
               'evidence_limit': 'Public developer-authored tests, not secret or independently audited. Submitted code and grader share a process; review before execution.'}
    onboard.save_new(out / 'summary.json', summary)
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-project', type=Path, required=True)
    p.add_argument('--adapter', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--timeout', type=int, default=15)
    p.add_argument('--acknowledge-code-execution', action='store_true')
    args = p.parse_args()
    try:
        result = grade(args.source_project, args.adapter, args.out, args.acknowledge_code_execution, args.timeout)
        print(canonical(result).decode(), end='')
        return 0 if result['status'] == 'TESTS_PASS' else 2
    except (DomainError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError) as exc:
        print(canonical({'status': 'ERROR', 'error': str(exc)}).decode(), end='')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
