# Evidence and replays: can the conclusions be trusted?

The command center ranks what needs attention. Two things make that trustworthy during an
incident: every conclusion names what it rests on, and the way conclusions are reached is
measured against what really happened.

## Every finding names its evidence

Each item on `/command` (and in `/api/command`, `what_needs_attention`) carries `evidence`:

| Field | Meaning |
|---|---|
| `kind` | `observation` (a health sample), `file` (lines in the repository or a skill), `decision` (what a person confirmed), `record` (an aggregate of the business's own records, an audit), `absence` (looked for and not there) |
| `ref` | which one: `component@time`, `path:lines`, a decision, a record |
| `source`, `component`, `environment` | where it came from: the feed (`otel`, `health_probe`, `prometheus`...), the system, the environment the control room watches (`--environment production`) |
| `collected_at`, `age_seconds` | when it was observed, and how long ago that is |
| `state` | `COMPLETE`, `PARTIAL` (part of what it rests on is behind, such as a failing pull), `STALE` (past its freshness window), `MISSING` (nothing held) |
| `role` | `basis`: what the conclusion rests on; `context`: what it is about (the file that declares the system, the skill lines that define the action) |

and `contradicted_by`, what would show it wrong ("a fresh ok sample from payments newer than
11:58:00Z").

**A conclusion whose basis is all expired or missing is not a conclusion.** It reads `UNKNOWN`,
with `unknown_because` ("its evidence has expired"). The command center also states a **verdict**
on the systems, `FAULT`, `DEGRADED`, `HEALTHY` or `INCONCLUSIVE`, with the share of watched
systems that have fresh evidence. Watched: every system that ever reported health, and every
system a business item relies on. Missing evidence is never read as healthy: with no fault seen
and any watched system silent or expired, the verdict is `INCONCLUSIVE` and names them. Causes
come with the ones that have no failing system upstream first.

## Replays

`replay.py` keeps what the control room knew at a moment, and replays it later:

```bash
python ontology-extraction/scripts/replay.py capture --repo REPO --name payments-outage \
  --observations REPO/.ontologyex/health.jsonl --skill SKILL --links LINKS.json --environment production
# ... later, once it is known what happened, edit REPO/.ontologyex/replays/payments-outage/case.json:
#   "expect": {"verdict": "FAULT", "cause": ["discovery:service:compose.yml:payments"], "within": 3}
python ontology-extraction/scripts/replay.py run REPO/.ontologyex/replays/*
```

`capture` saves the discovered topology (not the repository), the health observations of the
window before the moment (`--at`, default now; `--window-hours`, default 24), the skills, links,
events, dependencies and rules, and the operations ledger: aggregates only, never an export. Run it
during an incident, or right after, so the topology is the one of that moment.

`run` rebuilds each case through the control room's own assembly (`--bundle-only` with the saved
topology and `--now` at the moment) from a copy, and checks:

- **verdict**: the one expected; never `HEALTHY` when evidence is missing;
- **cause**: the expected cause among the first `within` causes the ranking names;
- **citations**: every top conclusion cites evidence the case holds (coverage, 100% expected);
- **reads only**: the case is unchanged; no repository is read; nothing is written.

It exits 1 when a case fails, so it can run in CI. A case without `expect` is still replayed and
measured, not scored. The repository is not in a case, so confirmations are not re-checked against
it in a replay (shown as not re-checked).

The corpus in `tests/replays/` (synthetic, built from the demo by `tests/replays/build.py`) holds
seven situations at one moment: an error, an error with its upstream cause, a degraded queue, all
healthy, one silent service, every sample expired, and no evidence at all. CI replays it.
