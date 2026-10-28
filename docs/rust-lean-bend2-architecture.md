# OntologyEX — hosted one-button business digital twin

**End state:** connect approved business data sources, press "Map my business",
and let a hosted service build a source-linked, reviewable graph. Remote workers
keep it updated while the user's laptop is only a viewport.

## Responsibilities

| Layer | Default | Status |
|---|---|---|
| Graph identity, observations, bounded impact | Rust | Initial offline core under engine/ |
| Domain model and evidence compiler | Existing OntologyEX Python | Preserve |
| Model-policy invariants | Lean 4 | Formal package under proofs/lean |
| Live telemetry ingestion (probes, Prometheus, OTLP) | Rust `ontologyex-feeds` | Shipped; parity-checked against `feeds.py` ([Rust engine](rust-engine.md)) |
| Repository discovery | Rust `ontologyex-discover` | Shipped; byte-identical to `discover_repo.py` |
| Durable remote jobs | Rust + bounded worker scheduler | Planned |
| Pure parallel graph acceleration | Bend2 | Experimental; benchmark gate |
| Persistent workspace data and snapshots | Hosted database + object storage | Planned |
| Observability | OpenTelemetry collectors and signals | Planned |
| Visualization | Hierarchical, virtualized, lazy graph slices | Planned |

A Lean theorem about an abstract model is **not** a proof that Rust
or a deployed cloud service implements it. Keep Rust behavior tests and
cross-language conformance tests, and prove refinement before claiming
executable correctness.

## The one-button flow

1. User chooses a business/workspace and consents to minimal read-only
   connectors (GitHub first, then deployments, database metadata, docs).
2. Backend creates a durable scan job; the browser renders progress.
3. Hosted workers read a bounded source inventory without executing code.
4. Evidence gets immutable source identity, collection/observation time,
   source hash, scope and freshness.
5. Optional agent extraction proposes ontology candidates. They are
   reviewable, not automatically authoritative.
6. The first useful **partial** graph renders quickly: backend, frontend,
   workers, schemas, dependencies, tasks, and source gaps.
7. Read-only webhook/CDC/poll workers update affected graph portions.
8. The control room shows health signals, incidents, historical changes
   and potential dependency impacts. Unobserved is always unknown.
9. Remediation remains separate, approval-gated and audited.

## Cloud-side performance

- Remote queue and per-tenant CPU, RAM, network and token quotas.
- Source-level incremental updates and snapshots; don't rescan everything.
- Metadata-first database discovery. Never ingest customer rows by default.
- Lazy graph tiling: only a visible region goes to the browser.
- Reconnect and background processing continue after closing the tab.
- Storage caps, retention/deletion policy, revocation and tenant isolation.
- Credentials and private inputs never enter graph exports or web bundles.
- Stale offline UI uses time-stamped last-known data; never fake live green.

## What the first Rust core does

The bounded, deterministic Rust crate validates a typed graph, maps reported
observations to components at a chosen clock time, explicitly represents
unknown/stale/conflicting health, records future and unmapped observations,
finds potential dependent exposures even with cycles, and keeps execution
authorization false. It is a reference component, not a hosted scanner,
authenticated observation collector, cloud service, or full business map.

## Lean 4 rules

Prove high-priority model properties: absence cannot become healthy;
stale/conflicting reports cannot be marked healthy; potential impact cannot
rewrite another component's observed state; and monitoring cannot authorize
execution. These rules are reflected in Rust tests, but source-level
correspondence is not yet formally proven.

## Bend2 admission gate

Bend2 (BendRT) is a new parallel CPU/GPU language. Use the Rust core as
reference and Rust concurrency as the default. Bend2 may accelerate **pure**
graph reachability, deduplication or aggregation, but only when:

- Exact equivalence passes cyclic, adversarial, sparse, dense and large cases.
- Benchmarks include cold/warm runtime, compile overhead, memory, serialization
  and CPU/GPU transfer.
- There is a material advantage over optimized Rust on target hosted workloads.
- Runtime isolation, stable fallback, toolchain pinning and deterministic
  outputs are demonstrated.

No automatic Bend2 installation or experimental GPU code is part of this PR.

**Update (2026-10-28):** Bend 2 moved to `bendlang/bend` and now includes a prover
(`LAWS.bend` / `PROOF.bend`). OntologyEX uses that prover, not the runtime, to check
business laws in CI at a pinned commit. See
[business-verification.md](business-verification.md). Runtime use still has to pass
the admission gate above.

## Real hosted acceptance

- A source-scoped scan can map one selected repo, one deployment and one
  database schema with evidence.
- Controlled failure surfaces incident evidence, possible dependents, unknown
  health and freshness.
- Repeated jobs are idempotent, cancellable, quota-bounded and recoverable.
- Browsers never download the entire company graph.
- Source provenance, authorization boundaries, tenancy, and deletion tested.
- Before production claims: CI, threat model, incident drills and external
  security review.

Architectural inspiration is from WorldMonitor, without copying its
AGPL-licensed code.
