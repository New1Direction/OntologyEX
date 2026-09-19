"""Replay a developer-authored model of pinned upstream sources; NOT an extractor.

The evidence helper, not this recipe, copies hashes and exact quotations. Interpretations
remain reviewable. Native timer types and library behavior are not forced into integer checks.
"""
from pathlib import Path

import evidence
import yaml
from policy_check import MAX_TOTAL, canonical, parse_json, safe_file


def author(workspace: Path) -> None:
    source = 'src/cachetools/__init__.py'
    spans = [('E-cache', source, 45, 59), ('E-values', source, 69, 100),
             ('E-timer', source, 463, 488), ('E-refresh', source, 490, 503),
             ('E-expire', source, 537, 557), ('E-recency', source, 559, 582),
             ('E-task', 'TASK.md', 3, 17)]
    for eid, path, start, end in spans:
        item = evidence.record(workspace, path, start, end, eid,
                               'requirement' if path == 'TASK.md' else 'implementation')
        evidence.add(workspace, item)
    classes = [
        {'id': 'CachedEntry', 'definition': 'A key/value association retained by a cache and subject to its lookup semantics.', 'upper': 'Thing', 'source': 'E-cache'},
        {'id': 'CacheKey', 'definition': 'A hashable lookup identifier for an entry.', 'upper': 'Thing', 'source': 'E-values'},
        {'id': 'CachedValue', 'definition': 'The stored object returned by a successful lookup, including falsy objects.', 'upper': 'Thing', 'source': 'E-values'},
        {'id': 'BatchResult', 'definition': 'The requested application adapter result: live hits and ordered missing keys.', 'upper': 'Thing', 'source': 'E-task'},
    ]
    rules = [
        ('strict-expiry', 'LookupLiveEntry', 'TTLCache considers an entry expired when its timer is not strictly less than its expiration; equality is expired.', ['E-timer']),
        ('missing-is-keyerror', 'LookupLiveEntry', 'The default cache missing handler raises KeyError; a custom subclass could change missing behavior.', ['E-values']),
        ('public-read', 'ReadBatch', 'Read each requested key once per occurrence through cache[key]; use KeyError to record a miss.', ['E-task', 'E-timer']),
        ('falsy-hit', 'ReadBatch', 'Preserve None, False, zero, and other falsy cached values as hits rather than testing truthiness.', ['E-task', 'E-values']),
        ('order-and-errors', 'ReadBatch', 'Preserve ordered duplicate misses, consume a finite iterable once, and propagate errors other than KeyError.', ['E-task']),
        ('do-not-change-clock', 'ReadBatch', 'Preserve per-read timer behavior, including noninteger/custom timer values; do not freeze a batch or consult a separate clock.', ['E-task', 'E-timer']),
        ('no-refresh-or-expire', 'ReadBatch', 'Do not refresh TTL or eagerly expire/delete entries; let public reads preserve normal library recency effects.', ['E-task', 'E-refresh', 'E-expire', 'E-recency']),
    ]
    contract = parse_json(safe_file(workspace, '60-contract.json', MAX_TOTAL))
    contract['inputs'] = {'LookupLiveEntry': {}, 'ReadBatch': {}, 'ChooseApplicationTTL': {}}
    contract['rules'] = [{'id': rid, 'task': task, 'statement': statement,
                          'status': 'observed', 'evidence': refs, 'check': None}
                         for rid, task, statement, refs in rules]
    contract['unknowns'] = [{'id': 'application-ttl', 'task': 'ChooseApplicationTTL',
                             'statement': 'The consuming application must choose an appropriate TTL; the selected sources do not decide it.', 'evidence': ['E-task']}]
    contract['conflicts'] = []
    (workspace / '60-contract.json').write_bytes(canonical(contract))
    tasks = []
    for name, source_id, inputs, outputs in [
        ('LookupLiveEntry', 'E-timer', ['CacheKey', 'CachedEntry'], ['CachedValue']),
        ('ReadBatch', 'E-task', ['CacheKey', 'CachedEntry'], ['BatchResult']),
        ('ChooseApplicationTTL', 'E-task', ['CachedEntry'], []),
    ]:
        tasks.append({'id': name, 'inputs': inputs, 'outputs': outputs,
                      'preconditions': [r['id'] for r in contract['rules'] if r['task'] == name],
                      'source': source_id})
    layers = {
        '10-upper.yaml': {'source_ontology': 'schema.org', 'version': 'unversioned published Thing IRI; minimal selection only',
                          'anchors': [{'id': 'Thing', 'iri': 'https://schema.org/Thing'}]},
        '20-domain.yaml': {'classes': classes, 'relations': [
            {'id': 'entry_key', 'domain': 'CachedEntry', 'range': 'CacheKey', 'cardinality': 'one-to-one', 'source': 'E-values'},
            {'id': 'entry_value', 'domain': 'CachedEntry', 'range': 'CachedValue', 'cardinality': 'many-to-one', 'source': 'E-values'}]},
        '30-task.yaml': {'tasks': tasks},
        '40-application.yaml': {'system': 'cachetools-7.1.4 / planned application adapter', 'concepts': [
            {'id': 'TTLCache.__getitem__', 'kind': 'code_function', 'binds': 'CachedEntry', 'used_by_tasks': ['LookupLiveEntry', 'ReadBatch'], 'source': 'E-timer'},
            {'id': 'Cache.__missing__', 'kind': 'code_function', 'binds': 'CacheKey', 'used_by_tasks': ['LookupLiveEntry', 'ReadBatch'], 'source': 'E-values'},
            {'id': 'planned.adapter.get_many', 'kind': 'planned_function', 'binds': 'BatchResult', 'used_by_tasks': ['ReadBatch'], 'source': 'E-task'}]},
    }
    for path, value in layers.items():
        (workspace / path).write_text(yaml.safe_dump(value, sort_keys=False), encoding='utf-8')
    (workspace / '00-scope.md').write_text(
        '# Scope\n\nModel public TTL cache lookup semantics and the requested batch adapter.\n'
        'The adapter is a new application requirement, not an existing upstream function.\n'
        'Exclude persistence, authorization, thread safety, and choosing a business TTL.\n\n'
        'Questions: Is equality expired? Are falsy values hits? Does a read refresh TTL?\n'
        'Which public lookup preserves recency? Which rules need behavioral rather than integer checks?\n', encoding='utf-8')
