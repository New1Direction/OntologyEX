# Live observability feeds

`feeds.py` turns real telemetry into the control room's observation rows. It runs as
its own process and writes one bounded JSONL file, which the control room reads with
`--observations`. The control room itself still makes no network calls.

| Feed | How it gets data | What it reports |
|---|---|---|
| **OTLP traces** (push) | Applications, or an OpenTelemetry Collector, export traces to `http://HOST:4318/v1/traces`. Protobuf (the SDK default) or JSON, optional gzip, optional bearer token. | Per `service.name`: error rate and p95 latency of **server** spans over a sliding window. Source `otel`. |
| **Prometheus** (pull) | Instant queries you write, against one Prometheus base URL. Optional bearer token from an environment variable. | `up`, `error_rate_pct`, `p95_latency_ms` per component. Source `prometheus`. |
| **HTTP probes** (pull) | GET an allowlisted health URL. Redirects are not followed. | Up or down from the status code, plus `probe_latency_ms`. Source `health_probe`. |

## Quick start: the live demo

```bash
python -m pip install 'opentelemetry-sdk==1.44.0' 'opentelemetry-exporter-otlp-proto-http==1.44.0'
python examples/operating-picture/live.py
# open http://127.0.0.1:8765/business
```

This starts small local services, probes them, and sends real spans through the
OpenTelemetry SDK's standard exporter. `payments` answers 503 and fails about 30% of
its requests, so IssueRefund shows **AT RISK** and ApproveException **POSSIBLY
IMPACTED**, from live data. Without the SDK it runs with probes only.

## Faster: the Rust feeds service

`ontologyex-feeds` is a drop-in replacement for `feeds.py`: the same config file,
the same observations and status files, about 18× the trace throughput in a fifth
of the memory, and probes polled in parallel. CI checks it against `feeds.py` on
every change. Install it with `fetch_engine.py` or build it with cargo; see
[Rust engine](rust-engine.md).

```bash
ontologyex-feeds check --config feeds.json --repo /path/to/repo
ontologyex-feeds run --config feeds.json
```

## Your own systems

1. Copy `examples/feeds/feeds.example.json` and set the components. Each `component`
   is a node ID from the system map (`control_room.py --state-only`). Check them:

   ```bash
   python ontology-extraction/scripts/feeds.py check --config feeds.json --repo /path/to/repo
   ```

2. Put secrets in environment variables, never in the file:

   ```bash
   export ONTOLOGYEX_OTLP_TOKEN=...        # required if the receiver listens beyond loopback
   export ONTOLOGYEX_PROMETHEUS_TOKEN=...  # if Prometheus needs one
   python ontology-extraction/scripts/feeds.py run --config feeds.json
   ```

   `feeds.py once --config feeds.json` runs the probes and queries once (for cron or CI).

3. Start the control room on the feed's output:

   ```bash
   python ontology-extraction/scripts/control_room.py --repo /path/to/repo \
     --observations observations.jsonl --skill SKILL --links links.json
   ```

   The business page shows a **live feeds** chip ("5 live feeds", or "1 of 5 feeds
   failing"), read from `observations.status.json` beside the output file.

### Sending traces

Straight from an application with the standard SDK environment variables:

```bash
export OTEL_SERVICE_NAME=payments
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20${ONTOLOGYEX_OTLP_TOKEN}"
```

Or through an existing Collector: `examples/feeds/otelcol.yaml` adds a pipeline that
forwards server spans with the `otlphttp` exporter. `service.name` must match a key in
`otlp.services`; other services are counted in the status file as unmapped and ignored.

### Writing Prometheus queries

Each query must return **exactly one** series or a scalar; aggregate with `min`, `sum`,
or `max`. A query that returns zero or several series, fails, or returns NaN produces
no sample, and the feed is marked failing in the status file.

## Rules

- **Status.** A value at or above an `error` threshold is `error`; at or above a
  `warning` threshold is `warning`; otherwise `ok`. Probes: an unexpected status code,
  timeout, or refused connection is `error`. Prometheus: `up` below 1 is `error`.
- **Freshness.** Each sample expires after three intervals (at least 60 seconds). A
  feed that goes quiet turns its component **STALE**, then **UNKNOWN**, never ok.
- **Low traffic.** OTLP needs at least `min_requests` server spans in the window;
  otherwise it reports nothing rather than guessing.
- **Several sources for one component** combine by severity: each source's newest
  sample counts while fresh, and the worst fresh status is shown. An ok from one feed
  cannot hide an error from another, and line order never matters. A source that
  contradicts itself at one timestamp fails closed to UNKNOWN. This rule is
  implemented in Python and the Rust core, proven in Lean (`OntologyEX.Fusion`), and
  cross-checked by `tests/conformance.py`.
- **Storage.** The newest `keep_per_component` samples per component (default 8) are
  kept, and the file is rewritten atomically and checked with the control room's own
  parser.

## Safety

- Outbound requests go only to hosts in `allowed_hosts`, over http or https, with no
  credentials in URLs, no redirects, no proxies, a timeout, and a 1 MB response cap.
- The OTLP receiver binds to loopback by default. A non-loopback bind requires a bearer
  token. Bodies are capped at 4 MB (16 MB after gzip). Only `/v1/traces` is accepted.
- Only span kind, status code, duration, and `service.name` are read. Span names,
  attributes, events, and payloads are not stored.
- Telemetry is **source-reported**: a 200 from a health check is one answer, not proof
  of health. Nothing here changes or repairs a system.

## Tests

```bash
python -m unittest tests.test_feeds -v
```

These run against real local HTTP servers: a health endpoint, a Prometheus-shaped API,
and the OTLP receiver. With the OpenTelemetry SDK installed, one test exports spans
through the real OTLP/HTTP exporter (CI installs it).
