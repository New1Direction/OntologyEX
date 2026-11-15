# Historical signal replay

This local tool compares OntologyEX's two-reading threshold signal with a team's
supplied existing alerts. It uses **when a reading happened** and **when it was
available to the system** as separate times. It also makes conflicting readings,
missing coverage and unreviewed outcomes visible instead of turning them into
hits or clean results. This workflow was informed by the distinction between
event time and recorded time in [Omnigraph's company-brain example](https://github.com/ModernRelay/omnigraph-cookbooks/blob/c13bac144fcf5692fab38b83fc09705d37789d6e/company-brain/README.md).

Try the clearly fictional payments examples:

```bash
python ontology-extraction/scripts/history_replay.py \
  --input examples/historical-replay/payments-history.json \
  --format html --out /tmp/ontologyex-history.html
```

Open the HTML file in a browser. It is a standalone report with no JavaScript or
network requests. For a machine-readable report, use `--format json` (the default)
and a new `--out` path, or omit `--out` to print it. Input files are read only;
output refuses to overwrite an existing file. No alerts are sent.

The five invented cases exercise a warning before an incident, a late reading,
a false alarm, conflicting readings and an outcome awaiting review. The late
reading is dated before the fictional incident but becomes available after the
decision window, so the replay cannot use it to claim an earlier warning. The
report compares the synthetic two-reading rule with synthetic existing alerts;
these cases are a demonstration, **not evidence of predictive performance**.

An opt-in [local observation history](observation-history.md) can prepare an
unreviewed case from newly published feed readings. It starts with unknown capture
coverage; recorded samples alone do not prove that no readings were missed.

| Outcome in the fictional cases | Replayed two-reading rule | Supplied existing alerts |
| --- | ---: | ---: |
| Hit before the labeled incident | 1 | 2 |
| Miss | 1 | 0 |
| False alarm | 1 | 0 |
| No alarm in a quiet case | 0 | 2 |
| Insufficient data | 1 | 0 |
| Outcome needs review | 1 | 1 |

## Supplying your own cases

Copy the [example JSON](../examples/historical-replay/payments-history.json) and
replace its contents with reviewed, appropriately scoped history. All timestamps
need an explicit timezone. The format is strict: unrecognized fields and missing
availability times are refused rather than silently treated as known data.

| Field | Meaning |
| --- | --- |
| `metric`, `threshold.above` | One supported metric and the threshold applied to all cases. The rule is strictly above, not equal to, that value. |
| `threshold.selected_at` | The actual time the threshold was selected. This is supplied by the dataset owner and is not independently verified. |
| `threshold.training_data_available_through` | The latest time that training data was available. Use `null` when the threshold was selected before every decision window. For a retrospectively selected threshold, supply a cutoff before each test window and no later than selection. This is an assertion about the training data, not an audit of what the person saw. |
| `window.start`, `window.end`, `step_seconds` | Inclusive decision points at the specified cadence. One case has at most 301 decision points. A warning is evaluated at these points, not continuously between them. |
| `window.follow_up_end` | The end of the period in which an independently reviewed incident would count for this case. It must be after the decision window. |
| `capture_coverage` (optional) | For recorded cases, `unknown` prevents the replayed rule from being scored even if a simulated warning is shown. A reviewer may assert `complete` with an evidence reference; that assertion is not independently verified. Cases without this field retain sampled-freshness behavior and do not prove continuous coverage. |
| `outcome` | `incident` with an `incident_at` after the decision window and within follow-up; `no_incident` for an assessed quiet period from window start through follow-up; or `unreviewed` when the outcome still needs a person. An unreviewed outcome may carry a candidate incident time, but is never scored. All require an `evidence_ref`. |
| `observations[].observed_at` | When the metric reading was made. |
| `observations[].available_at` | When the reading first became available to the monitored system, **not when a historical file was imported today**. It may be later than `observed_at`, but not earlier. |
| `observations[].ref`, `source`, `status`, `ttl_seconds`, `value` | A short evidence reference, source label, source-reported status, freshness lifetime and metric value. |
| `baseline.coverage` | `complete` only if the supplied existing-alert record covers the full decision window; otherwise `unknown`. This is a user assertion. |
| `baseline.alerts` | Existing alert references with `alerted_at` and the time each became `available_at`. A late alert is not credited before it was available. |

References and case names are short IDs or paths made from letters, digits,
`.`, `_`, `:`, `/` and `-`. Do not put credentials or customer details in them.
Supported sources are `manual_export`, `prometheus`, `otel`, `health_probe` and
`synthetic`; statuses are `ok`, `warning` or `error`. Metrics are
`queue_lag_seconds`, `db_pool_utilization_pct`, `error_rate_pct`,
`p95_latency_ms`, `job_lag_seconds` and `probe_latency_ms`. Freshness TTLs are
1–86,400 seconds.
The file is limited to 1 MiB, 20 cases and 300 observations or alerts per case.
Each decision window spans at most 30 days, with 1–86,400 seconds between
decisions and at most 301 decision points.

If the historical availability time is unknown, do not substitute the event time.
Obtain an appropriate collector or alert-delivery timestamp first; this tool
cannot establish the timing on its own. Similarly, mark an outcome `unreviewed`
until someone can support it. Labels, coverage and evidence references are
provided by the dataset owner and are not independently authenticated.

If a threshold was actually in place before the decision window, record its real
selection time; the training cutoff may be `null`. For a retrospective held-out
test, record when it was actually selected—even if that is today—and provide a
training-data **availability** cutoff earlier than the test window. Select and
freeze the threshold using only data available by that cutoff, without inspecting
the holdout. The tool checks the declared dates, but it cannot verify what the
person who selected the threshold saw or whether the cases were chosen fairly.

## How cases are scored

The threshold signal uses the same `control_room.build_state` as the live view:
two distinct, consecutive fresh readings above the threshold from one source.
A reading is visible at a decision only if both its event time and availability
time are no later than that decision. Duplicate deliveries are one reading;
conflicting records for the same source and instant are shown for review and
the ambiguous reading is not selected automatically.

An unreviewed outcome is `needs_review`. The two-reading rule is simulated at
each decision; its output does not establish that an alert was actually sent at
the time. A simulated warning before a labeled incident is a `hit`, and its lead
time is measured from the warning's decision time to the supplied incident time.
With no warning, telemetry must cover every decision
after the first warm-up point or the result is `insufficient_data`, not a miss
or a clean no-alarm. Existing alerts use their separately supplied coverage; an
unwarned case with unknown baseline coverage is also `insufficient_data`. A
warning may be counted even when later coverage is incomplete; the report shows
that limitation. A warning in an assessed no-incident period is a `false_alarm`.
Freshness is checked only at the supplied decision points using the supplied
TTL; it does not establish that data was continuously available between them.
Only existing alerts available inside the decision window are counted; the
report lists alerts outside it, including those that arrived before an incident
but after the decision window ended.

Counts are **per case**, not unique incidents. If windows for the same component
overlap through follow-up, the report names those pairs because the same incident
or alert could be counted more than once. Choose representative, non-overlapping
cases and quiet periods before drawing conclusions. The tool tests one component
and a single threshold per case; it does not reconstruct a historical graph,
calculate downstream failures, confirm causes, or estimate probabilities.
