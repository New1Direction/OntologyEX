"""Developer-authored acceptance tests. Do not export this file to trial agents.

These exercise actual library behavior, independently of the ontology/checker.
They are public tests, not a claim of hidden or independently audited evaluation.
"""
from datetime import datetime, timedelta


class Clock:
    def __init__(self, now=0):
        self.now = now
        self.step = 0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        result = self.now
        self.now += self.step
        return result


def run(get_many, library):
    def cache(ttl=10, maxsize=8, clock=None):
        clock = clock or Clock()
        return library.TTLCache(maxsize, ttl, timer=clock), clock

    def live_hit():
        c, _ = cache(); c['a'] = 1
        assert get_many(c, ['a']) == ({'a': 1}, [])

    def absent():
        c, _ = cache()
        assert get_many(c, ['x']) == ({}, ['x'])

    def expiry_equality():
        c, clock = cache(); c['a'] = 1; clock.now = 10
        assert get_many(c, ['a']) == ({}, ['a'])

    def fractional_clock():
        c, clock = cache(); c['a'] = 1; clock.now = 9.999
        assert get_many(c, ['a']) == ({'a': 1}, [])

    def falsy_values():
        c, _ = cache()
        values = dict(enumerate([None, False, 0, '', [], {}]))
        c.update(values)
        assert get_many(c, values) == (values, [])

    def miss_order_duplicates():
        c, _ = cache(); c['a'] = 1
        assert get_many(c, ['x', 'a', 'y', 'x', 'a']) == ({'a': 1}, ['x', 'y', 'x'])

    def generator():
        c, _ = cache(); c['a'] = 1
        assert get_many(c, (x for x in ['a', 'x'])) == ({'a': 1}, ['x'])

    def mixed_expiration():
        c, clock = cache(); c['a'] = 1; clock.now = 5; c['b'] = 2; clock.now = 10
        assert get_many(c, ['a', 'b']) == ({'b': 2}, ['a'])

    def read_recency():
        c, _ = cache(maxsize=2); c['a'] = 1; c['b'] = 2
        assert get_many(c, ['a']) == ({'a': 1}, [])
        c['c'] = 3
        assert 'a' in c and 'b' not in c and 'c' in c

    def no_eager_deletion():
        c, clock = cache(); c['a'] = 1; clock.now = 10
        assert get_many(c, ['a']) == ({}, ['a'])
        # Base Cache.__len__ inspects storage without triggering TTLCache.expire().
        assert library.Cache.__len__(c) == 1

    def unhashable_key():
        c, _ = cache()
        try:
            get_many(c, [[]])
        except TypeError:
            return
        raise AssertionError('unhashable key did not propagate TypeError')

    def other_exception():
        class Broken(library.TTLCache):
            def __getitem__(self, key):
                raise RuntimeError('underlying cache failure')
        try:
            get_many(Broken(2, 10), ['x'])
        except RuntimeError:
            return
        raise AssertionError('RuntimeError was swallowed')

    def no_ttl_refresh():
        c, clock = cache(); c['a'] = 1; clock.now = 9
        assert get_many(c, ['a']) == ({'a': 1}, [])
        clock.now = 10
        assert get_many(c, ['a']) == ({}, ['a'])

    def datetime_clock():
        class DateClock:
            now = datetime(2024, 1, 1)
            def __call__(self):
                return self.now
        clock = DateClock()
        c = library.TTLCache(2, timedelta(seconds=10), timer=clock); c['a'] = 1
        assert get_many(c, ['a']) == ({'a': 1}, [])
        clock.now += timedelta(seconds=10)
        assert get_many(c, ['a']) == ({}, ['a'])

    def per_read_clock():
        c, clock = cache(ttl=5); c['a'] = 1; c['b'] = 2; clock.step = 6
        assert get_many(c, ['a', 'b']) == ({'a': 1}, ['b'])

    def invalid_iterable():
        c, _ = cache()
        try:
            get_many(c, None)
        except TypeError:
            return
        raise AssertionError('invalid iterable did not propagate TypeError')

    def one_read_per_occurrence():
        class Counted(library.TTLCache):
            reads = None
            def __getitem__(self, key):
                self.reads.append(key)
                return super().__getitem__(key)
        c = Counted(2, 10); c['a'] = 1; c.reads = []
        assert get_many(c, ['a', 'x', 'a']) == ({'a': 1}, ['x'])
        assert c.reads == ['a', 'x', 'a']

    def empty_no_reads():
        c, clock = cache(); c['a'] = 1; clock.now = 10; calls = clock.calls
        assert get_many(c, []) == ({}, [])
        assert clock.calls == calls and library.Cache.__len__(c) == 1

    checks = (live_hit, absent, expiry_equality, fractional_clock, falsy_values,
              miss_order_duplicates, generator, mixed_expiration, read_recency,
              no_eager_deletion, unhashable_key, other_exception, no_ttl_refresh,
              datetime_clock, per_read_clock, invalid_iterable, one_read_per_occurrence, empty_no_reads)
    results = []
    for check in checks:
        try:
            check()
            results.append({'name': check.__name__, 'status': 'PASS'})
        except Exception as exc:
            results.append({'name': check.__name__, 'status': 'FAIL', 'error': f'{type(exc).__name__}: {exc}'})
    return {'passed': sum(r['status'] == 'PASS' for r in results), 'total': len(results), 'cases': results}
