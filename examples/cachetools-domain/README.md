# Real-source case: cachetools TTL lookups

**Pinned upstream code → source-linked domain model → portable skill → batch-lookup task.**

This study uses `tkem/cachetools`, commit `48284d73d0a8834c9c50f8d41bb99e6f93b2dfed`
(tag `v7.1.4`). The case reads the installed distribution without importing it, verifies the
selected source and license bytes against SHA-256 and Git blob hashes, and then snapshots them.
The pin and upstream attribution are in [upstream.json](upstream.json). No live fetch is performed.

The task in [TASK.md](TASK.md) is an **application requirement authored for this study**, not
an upstream feature request or claim that cachetools has a `get_many` API.

## Run

```bash
python -m pip install -r examples/cachetools-domain/requirements-case.txt
python examples/cachetools-domain/case.py --out build/cachetools-case
```

Dependencies are pinned. The runtime is offline after installation. A wrong version or source
hash stops the case. The program executes only the explicitly pinned upstream package and the
reviewable developer reference adapter during the behavioral test; it never executes arbitrary
repository sources during extraction.

Inspect `summary.json`, `session/attempts/01/result.json`, the candidate's report.html,
`handoff/context.json`, and the three `trials/` directories. Outputs are never overwritten.
Use a fresh `--out` for another run. The compiler, checker, and original examples are unchanged.

## What this demonstrates

The model identifies TTL lookup's strict boundary (`timer() < expires`), missing-key behavior,
normal recency changes, the distinction between reading and refreshing TTL, and the adapter's
falsy-value/error-handling requirements. Source excerpts are produced with the evidence helper,
not manually fabricated quotation hashes. The handoff names the relevant implementation sites.

Native cache timer values can be fractional or datetime-like. The typed domain checker does
not represent all those semantics. The corresponding rules remain **manual review checks**,
not approximated integer predicates. A successful compile still has `NEEDS_REVIEW` diagnostics
for this task. Separate executable tests exercise actual behavior.

The reference adapter passes 18 acceptance cases: live/absent values; exact and fractional expiry;
falsy values; duplicate ordered misses; generator inputs; mixed expirations; read recency;
no eager deletion; unhashable keys; unrelated exceptions; no TTL refresh; datetime timers;
per-read timer advancement; invalid iterables; one read per occurrence; and empty requests.
A regression test deliberately introduces a truthiness bug and confirms the tests reject it.
That mutation test is not a deliberately weakened model baseline.

## Evidence limits

`model_recipe.py` and `reference_adapter.py` are developer-authored, reviewable artifacts.
The recipe was written against real pinned source during implementation; running it again is a
replay, not fresh automatic extraction. `acceptance.py` was authored separately from the model
and checks library/code behavior, but it is public and not independently audited or secret.
This is an integration case, not a randomized or blinded model-performance experiment.

The case must report:

```text
agent_comparison: NOT_RUN
native_host_activation: NOT_RUN
semantic_review: PENDING
execution_authorized: false
```

Do not claim an improvement percentage, a successful independent fresh-agent run, or a production
safety guarantee from these results. Native host activation and novice-user onboarding are still
acceptance work to be performed with the actual host.

## Fresh-agent experiment inputs

`trials/raw`, `trials/markdown`, and `trials/domain-skill` contain identical underlying source
information and the same task prompt. The latter two add, respectively, a concise developer-written
guide and the compiled skill. None includes the reference adapter, model recipe, acceptance tests,
or the recorded reference results. The operator protocol is outside those trial directories.

Before running: pin one model version, equal tool permissions, token/time budgets, and three
independent repetitions per variant. Record every attempt, input/output hashes, transcript,
preparation cost, task cost, and grader results. Treat total preparation-plus-execution cost as
well as downstream task success as outcomes. Do not hide failed runs.

Copy ONLY the selected trial directory into an isolated execution environment. Do not give the
agent the full study or parent checkout. Folder separation is not an access-control boundary.
Keep grading outside the proposing agent. Use a separate review process for semantics and security.
Never automatically install or promote a candidate because an acceptance test passed.

## Upstream references

- https://github.com/tkem/cachetools/tree/48284d73d0a8834c9c50f8d41bb99e6f93b2dfed
- https://github.com/tkem/cachetools/blob/48284d73d0a8834c9c50f8d41bb99e6f93b2dfed/src/cachetools/__init__.py
- https://github.com/tkem/cachetools/blob/48284d73d0a8834c9c50f8d41bb99e6f93b2dfed/LICENSE

Selected upstream source and its license accompany every generated source snapshot and trial.
