# Historical signal replay

Use this local workflow when a user supplies historical metric readings, incident
labels and existing alerts. It compares the simulated two-reading threshold rule
with those alerts. Each reading needs both its observation time and the time it became
available to the monitored system. If availability is unknown, do not substitute
the observation time or claim the replay proves what was known then. A normalized
observation's timestamp is not necessarily the start time of the underlying event.

Ask the user to prepare a dataset in `REPO/.ontologyex/`; read only what they
authorize and keep client records and generated reports there. Run:

```bash
python "$SKILL_ROOT/scripts/history_replay.py" \
  --input REPO/.ontologyex/history.json --format html --out REPO/.ontologyex/history-report.html
```

Omit `--out` for JSON on stdout; output refuses to overwrite an existing file.
The report is a standalone offline HTML file. It sends no alerts. The tool does
not authenticate the supplied data, reconstruct the historical business graph,
or prove that it can predict future failures. Never infer a confirmed outcome
from `unreviewed`, or a clean no-alarm from `insufficient_data`.

To retain new feed readings locally and prepare an unreviewed case, see
[local observation history](observation-history.md). Its export sets recording
coverage to unknown. This prevents scoring the rule until a reviewer asserts complete
capture coverage with an evidence reference, even if a simulated warning is shown.
Cases that do not supply `capture_coverage` retain the original replay behavior:
freshness is assessed at decision points, not as proof of continuous capture.

The following is a complete **fictional** example. Replace every field with
appropriately scoped, reviewed records for a real case; never put credentials
or customer details in references. Times must include a timezone.

```json
{
  "schema_version": 1,
  "kind": "ONTOLOGYEX_HISTORICAL_SIGNAL_DATASET",
  "name": "synthetic-example",
  "metric": "p95_latency_ms",
  "threshold": {"above": 500, "selected_at": "2026-01-01T00:00:00Z", "training_data_available_through": null},
  "cases": [{
    "id": "fictional-incident",
    "component": "discovery:service:compose.yml:payments",
    "window": {"start": "2026-01-01T12:00:00Z", "end": "2026-01-01T12:02:00Z", "follow_up_end": "2026-01-01T12:05:00Z", "step_seconds": 60},
    "outcome": {"state": "incident", "incident_at": "2026-01-01T12:03:00Z", "evidence_ref": "fixture:incident"},
    "baseline": {"coverage": "unknown", "alerts": []},
    "observations": [
      {"ref": "fixture:first", "observed_at": "2026-01-01T12:00:00Z", "available_at": "2026-01-01T12:00:00Z", "source": "synthetic", "status": "ok", "ttl_seconds": 180, "value": 650},
      {"ref": "fixture:second", "observed_at": "2026-01-01T12:01:00Z", "available_at": "2026-01-01T12:01:00Z", "source": "synthetic", "status": "ok", "ttl_seconds": 180, "value": 720}
    ]
  }]
}
```

`no_incident` means an assessed quiet period from the start through follow-up;
`unreviewed` can include a candidate incident time but is never scored. Set
baseline coverage to `complete` only when the alert record covers the whole
decision window. Every existing alert has `ref`, `alerted_at` and `available_at`.
The allowed source labels are `manual_export`, `prometheus`, `otel`,
`health_probe` and `synthetic`; statuses are `ok`, `warning` or `error`. Metrics
are `queue_lag_seconds`, `db_pool_utilization_pct`, `error_rate_pct`,
`p95_latency_ms`, `job_lag_seconds` and `probe_latency_ms`. `ttl_seconds` is
1–86,400. Each decision window has at most 301 points over at most 30 days.
Unknown fields, unavailable timestamps, oversized inputs and existing output
files are refused.

For a rule selected after a historical decision window, set `selected_at` to the
actual selection time and `training_data_available_through` to a cutoff before
the decision window and no later than selection. Freeze the rule using only
training data available by that cutoff, without inspecting the holdout. The tool
checks the supplied dates; it cannot verify what the selector saw or whether
the cases were selected fairly. Counts are per case, and overlapping cases can
count one incident more than once. Freshness uses the supplied TTL at sampled
decisions; it does not prove continuous data coverage between them.
