# Application task: batch lookup over a TTL cache

This is an OntologyEX-authored application requirement, not an upstream cachetools API.
Implement `get_many(cache, keys)` in `adapter.py` using the pinned cachetools TTLCache.
Return `(hits, misses)`: a dictionary of live values and a list of missing keys.
Read each requested key through the cache's public subscription interface, once per occurrence.
A missing or expired key belongs in misses; preserve request order and duplicate misses.
Preserve falsy cached values, including None, False, zero, empty strings, and empty containers.
Consume a finite iterable of keys once; hits for repeated keys use normal dictionary semantics.
Catch only KeyError for a miss. Propagate other errors, including unhashable keys.
Do not eagerly expire/delete entries, refresh TTLs, freeze batch time, or use private internals.
Preserve the library's per-read expiration checks and normal read-recency effects.
Do not assume clock values are integer seconds or read a wall clock instead of the cache timer.
An empty iterable returns an empty hits dictionary and misses list without reading the cache.
The adapter adds no authorization, persistence, atomicity, or thread-safety guarantees.
A consuming application must decide its appropriate TTL; that business decision is out of scope.
Do not modify the upstream library or the independent acceptance tests.
