# Source-reported incident evidence — local investigation

## Intent

The control room should help investigate what was observed without
pretending static source imports establish runtime calls or root causes.

## Endpoint and UI

    GET /api/incident?id=<existing-component-id>

The read-only loopback endpoint binds **one validated observation list**
to the same source graph and evaluation time used to compute current
status. It produces an evidence-first report for a selected component.

The selected-component panel gains an explicit **Investigate evidence**
button. The UI displays the observed timeline, source, TTL/freshness,
allowlisted metrics, declared possible dependents, snapshot-version
context and next safe diagnostic suggestion. It uses textContent, not
HTML rendering of untrusted paths or source records.

## Trust semantics

- `UNATTESTED_LOCAL_INPUT`: health samples are syntactically validated,
  not independently authenticated or proven true.
- A missing sample means UNKNOWN. An expired sample means STALE.
- Same-timestamp conflicting observations fail closed in the existing
  health projector; the timeline still shows the individual source claims.
- `DEPENDS_ON` edges identify *possible* downstream exposure only.
- Source snapshot history reports GitHub commit-scanned versions,
  **not deployment events or proof of when a failure began**.
- `root_cause=UNDETERMINED` always; never fabricate causal history.
- No repair, action authorization, credential access or network call
  exists in the incident module.

## Bounded inputs and output

The control room continues to validate JSONL observations against
its strict source, status, metric and TTL allowlists. This view returns
at most 32 timeline records, 40 possible dependents, 40 direct edges
and eight source version references. It records omitted counts.
Timeline events after evaluation time are not included.

## Tests

- Python fixtures: healthy/unknown/failure/stale, source trust,
  bounded timelines, deterministic ordering, future samples ignored,
  no invented dependency outages and no root-cause authorization.
- Chrome smoke: synthetic failure in the 405-node local Compose
  graph, on-demand incident evidence, explicit source and trust labels.
- Existing real repo.ing CI smoke remains source-only and UNKNOWN;
  a green GitHub Actions workflow never implies production service health.

## Next

Add a genuinely authenticated OpenTelemetry collector or provider
adapter that binds signed/approved deployment/service identities
to source graph nodes. Correlate traces, deployments and incidents
only when their evidence timestamps, collection scopes and identities
have been independently verified. Then build tests that inject
controlled failures and evaluate false-positive/false-causality rates.

Still a local developer prototype and an unmerged draft. No production
deployment or live infrastructure modifications.
