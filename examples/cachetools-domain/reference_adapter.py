"""Developer reference implementation; NOT a fresh-agent benchmark result.

Read only through the public mapping protocol so TTL and recency stay with the cache.
No truthiness test: a present None/False/zero is still a hit. Only KeyError is a miss.
"""
from collections.abc import Iterable, Mapping
from typing import TypeVar

K = TypeVar('K')
V = TypeVar('V')


def get_many(cache: Mapping[K, V], keys: Iterable[K]) -> tuple[dict[K, V], list[K]]:
    """Return live hits and ordered misses for a finite iterable, once per key occurrence."""
    hits: dict[K, V] = {}
    misses: list[K] = []
    for key in keys:
        try:
            value = cache[key]
        except KeyError:
            misses.append(key)
        else:
            hits[key] = value
    return hits, misses
