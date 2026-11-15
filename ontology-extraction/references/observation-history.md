# Local observation history

The Python feed can optionally keep a local journal of the normalized observations it
publishes. This lets a person inspect more than the small rolling live feed and prepare
an unreviewed historical replay. Recording starts only when explicitly enabled; it
cannot recover older availability times or backfill samples from the live file.

## Start and inspect

In the commands below, `SKILL_ROOT` is the kit's `ontology-extraction` directory
(or the installed skill directory), and `REPO` is the repository being monitored.
Replace `REPO` and `feeds.json` with your own paths. With a reviewed feed
configuration, start the local control room and recorder:

```bash
python "$SKILL_ROOT/scripts/operate.py" up --repo REPO --feeds feeds.json --record-observations
```

The launcher prints the database path: `REPO/.ontologyex/observation-history/history.sqlite3`.
Recording uses the Python feed engine, including when `--engine auto` would otherwise
select Rust. Explicit `--engine rust` with recording is refused. Without the flag,
recording is off and the engine selection stays unchanged. `--no-history` controls
the separate command-center digest history; it does not disable observation recording.

```bash
python "$SKILL_ROOT/scripts/observation_history.py" status \
  --db REPO/.ontologyex/observation-history/history.sqlite3
```

Status shows component IDs, sources, first and last observed times, counts and recording
sessions. An unfinished session may be running or may have been interrupted: check it
before relying on its history. Omitted observations are rows in a newly published batch
that did not fit into the live file; they are counted and not presented as available.
Status reports at most 256 component/source groups and 1,000 recent sessions, with
truncation flags. Total observation, omitted and unfinished-session counts cover the
whole database.

For direct use without the launcher, `feeds.py run` or `feeds.py once` accepts
`--record-history PATH`; its parent directory must already be private, or be creatable
inside an existing directory. The launcher creates a private history directory for you.

## Prepare a replay

Use `status` to find a component and time range, then supply the real threshold selection
time and decision window. This example uses **fictional** values; replace them with the
actual component, timestamps and selection evidence for the case being reviewed:

```bash
python "$SKILL_ROOT/scripts/observation_history.py" export-case \
  --db REPO/.ontologyex/observation-history/history.sqlite3 \
  --name payments-review --component 'discovery:service:compose.yml:payments' \
  --metric p95_latency_ms --threshold 500 --selected-at '2026-01-01T00:00:00Z' \
  --start '2026-01-01T12:00:00Z' --end '2026-01-01T12:02:00Z' \
  --follow-up-end '2026-01-01T12:05:00Z' --step-seconds 60 \
  --out REPO/.ontologyex/payments-review.json

python "$SKILL_ROOT/scripts/history_replay.py" \
  --input REPO/.ontologyex/payments-review.json --format html \
  --out REPO/.ontologyex/payments-review.html
```

The output file must not exist. Export includes the selected metric's recorded rows
in the decision window and earlier rows still fresh at the start, up to the replay's
300-reading limit. It refuses ambiguous same-source, same-time variants, including
differences in metrics other than the selected one; review the originals before
choosing a different window. It does not silently truncate a case.

Every exported case starts with an **unreviewed** outcome, unknown existing-alert
coverage, and `"capture_coverage": {"state": "unknown", "evidence_ref": null}`.
Review incident or quiet-period evidence and existing alerts independently before
editing those fields. Unknown recording coverage prevents scoring the replayed rule,
even when it shows a simulated warning: missing readings might change that result.
Only if a person has reviewed evidence for the full decision window should they set
capture coverage to `{"state": "complete", "evidence_ref": "review:reference"}`;
the tool cannot independently verify that assertion. See
[historical signal replay](historical-signal-replay.md) for labels, threshold selection,
holdout dates, limits and interpretation.

## What the journal can and cannot establish

- `observed_at` is the timestamp of the normalized feed observation. The bundled
  feeds use collector/aggregation time; it is not necessarily the time an underlying
  event began. `available_at` is measured after the live file is published and read
  back successfully. It is a conservative local bound, not an exact first-read time
  or an authenticated source timestamp. Its accuracy depends on the local system clock.
  If a new reading's availability clock is earlier than the journal's latest recorded
  availability, recording stops for review rather than inventing an earlier history.
- Only rows published by the Python feed after recording starts are saved. Failed
  polls, missing source reports, raw spans, trace calls, historical topology, incident
  labels and existing alerts are not recorded. A closed session does not prove the
  source reported continuously; a crash between publication and the database commit
  can leave a gap, and interrupted sessions remain unfinished.
- Replay simulates a rule on the journaled readings available at each decision. The
  rolling live file has its own retention limit, so this is not proof that the same
  rule ran or that the live screen had exactly the same history. It does not prove
  an incident was predicted or establish future predictive accuracy.
- The database contains component IDs, source/status, timestamps, TTLs and normalized
  metric values. It does not intentionally store URLs, credentials or raw telemetry,
  but the retained identifiers and values can still be sensitive. On POSIX the history
  directory must be owner-only and the database file is created owner-only. Protect
  and back up the database as appropriate, and ensure it is excluded from commits;
  there is no automatic deletion or backup.
- Recording is limited to 100,000 distinct observations and 16,384 SQLite pages
  (normally 64 MiB with 4 KiB pages, plus temporary journal overhead). When the
  database is full or cannot be written, the feed exits with an error; `operate.py`
  stops the control room too. A successful batch is transactional; no retention is
  silently discarded to make room. Review failed or unfinished sessions and restore
  storage before restarting. At the row or page limit, stop the feed, protect and
  archive the old database, then start a fresh one; histories are not automatically
  merged. These safeguards do not guarantee durability against every filesystem or
  hardware failure.
