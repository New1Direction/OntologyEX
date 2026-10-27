# OntologyEX Control Room — local operational slice (experimental)

This is the **first working link** between source-backed topology and fresh, externally
provided runtime observations. It is a local demonstration, not yet a hosted
one-click business digital twin or a deployed automatic collector.

## What it shows

- Project, manifest, workflow and service nodes from the bounded repository
  scanner (PR #11).
- Explicit Docker Compose dependency links (consumer -> dependency).
- Independent, normalized observation samples keyed to exact discovered node IDs.
- **ok / warning / error / stale / unknown** states with source and timestamps.
- Potential downstream dependency impacts from a currently failed node, without
  asserting that downstream components actually failed.
- Explicit **leading indicators** if a user-configured metric threshold is
  breached in *two consecutive, fresh samples*. This does not predict an outage.
- Interactive dark schematic with click-to-inspect evidence, blind spots, and
  links; automatically refreshes its local data every five seconds.

All observations are **source-reported, not independently attested**. A successful
health sample is one successful sample, not an availability guarantee. Blank
observations remain UNKNOWN rather than being painted green.

## Run

Requires Python 3.11+ and the existing pinned PyYAML requirement.

From an OntologyEX checkout:

    python ontology-extraction/scripts/control_room.py --repo /path/to/project

Open the printed http://127.0.0.1:8765 address. It is loopback-bound and
GET-only, with restrictive CSP and no write endpoint. The app runs while
the local Python process is active.

Without observations, **all components are UNKNOWN**. To attach real
observations from your own externally configured read-only collector:

    python ontology-extraction/scripts/control_room.py \
      --repo /path/to/project \
      --observations /path/to/normalized-health.jsonl \
      --thresholds /path/to/thresholds.json

The web client polls GET /api/state every five seconds. The server refreshes
the read-only topology at most once per 15 seconds, but reloads observations
every request. There is no database, external HTTP client, background crawler,
automatic healing, or source code execution.

For headless consumption:

    python ontology-extraction/scripts/control_room.py \
      --repo /path/to/project --state-only

This emits JSON, which can later be used by a hosted UI or other read-only
clients.

## Observation contract

Each newline-separated sample is a *normalized* object:

~~~json
{"schema_version":1,"component_id":"discovery:service:compose.yml:db","observed_at":"2026-10-27T16:00:00Z","status":"error","source":"synthetic","expires_after_seconds":60,"metrics":{"queue_lag_seconds":150}}
~~~

Allowed statuses: ok, warning, error.

Allowed source labels: otel, health_probe, manual_export, synthetic.

Allowed metric names (numeric only): queue_lag_seconds,
db_pool_utilization_pct, error_rate_pct, p95_latency_ms, job_lag_seconds.

The source field labels a **producer claim**, not a trusted authentication
mechanism. Do not treat a row carrying source=otel as verified OTLP.
No raw trace spans, prompts, logs, tokens, endpoints, addresses or credentials
are allowed in a normalized sample. This prototype does not itself parse,
collect or authenticate OTLP data.

To configure per-metric leading-warning thresholds:

~~~json
{"schema_version":1,"warning_above":{"queue_lag_seconds":120,"db_pool_utilization_pct":85}}
~~~

Threshold values are domain and workload specific. No guessed thresholds
are installed by default. The scorer requires two consecutive fresh readings
of a configured metric above threshold from the same source label; otherwise
an ok sample remains ok. An error sample stays error.

A sample older than its own expires_after_seconds is marked STALE. That
component is not treated as actively healthy *or actively failed*. A status
on a node does not automatically change statuses on consumers. Instead,
failure impact is reported in potentially_impacted_by through declared links.

## What this does not do

- It does **not** discover production topology from OpenTelemetry or cloud APIs.
- It does **not** validate the identity of a source or telemetry exporter.
- It does **not** confirm root cause or predict future outages.
- It does **not** reflect real database tables, query plans, process CPU,
  business logic, team responsibilities or customer transactions unless
  those are separately modeled and instrumented.
- It does **not** permit production remediation, deployment or wallet activity.

For real service graphs, use an existing trusted OpenTelemetry collector and
Grafana Tempo/Alloy service-graph metrics; avoid building a second trace system.
This local UI is an ontology/evidence/impact complement, not a replacement
for your APM or observability backend.

## Safety and tests

    python -m unittest tests.test_control_room -v

Safety boundaries: 1 MiB observation JSONL, at most 4096 rows, 8 KiB per row;
fixed allowlist of metrics and fields, no arbitrary free text, timestamp
and TTL validation, symlinked input rejection, malformed JSON/NaN and duplicate
key refusal. Only 127.0.0.1 is allowed for the local web server; no POST route.
The UI constructs graph elements via DOM textContent, not untrusted innerHTML.
Unknown resources and future-dated readings become explicit gaps.

The accepted observational schema is intentionally narrow; adapters for
actual OTLP, Prometheus, databases and cloud services must be separately built,
tested and granted read-only permission.

## Route to a trusted SaaS

1. Instrument services with OpenTelemetry, collect SLI signals centrally,
   and sign/authenticate ingestion with tenant-specific connector identities.
2. Correlate runtime spans and database metrics with source-backed service
   identities, environment, release version, and observation timestamps.
3. Persist bounded versions/events with retention, tenant isolation and an
   auditable trust policy.
4. Distinguish measured failure, dependency exposure, inference, and
   counterfactual simulation in APIs and the UI.
5. Run held-out fault-injection exercises: database unavailable, stalled
   indexer, excessive queue lag, partial tracer outage, credential revocation,
   false-positive prediction suppression.
6. Add alert delivery and operator-approved remediation only after those tests.
