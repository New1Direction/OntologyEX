# OntologyEX Rust-first migration — functional graph view core

## Decision

OntologyEX's long-term core is Rust. Python remains as a read-only
adapter/compatibility layer while we replace its graph query, health
projection, ingestion and hosted worker paths with validated Rust.

**Do not optimize GitHub's language percentage by adding unused code.**
The migration must replace working functionality after measured parity,
security and performance regression gates.

## This PR

Adds `engine/src/view.rs`, a Rust implementation of the existing bounded
source-backed control-room graph view contract:

- summary: compact operational counts, limited alerts and coverage;
- page: searched/lensed 90-node pages, at most 300 internal edges;
- neighborhood: deterministic 1/2-hop adjacency, original edge direction,
  truncated candidate/edge counts and evidence;
- node detail: at most 40 direct edges and matching source evidence;
- fail-closed input, node ID, edge and authority validation.

`ontologyex-view` is a bounded **offline CLI** that reads a previously
constructed local source graph as JSON on stdin, then prints a single
result. It uses no listening socket, credentials, filesystem access,
cloud provider, compilation of customer code or infrastructure mutations.

### Verification

`cargo test --manifest-path engine/Cargo.toml --all-targets`

`cargo build --manifest-path engine/Cargo.toml --bin ontologyex-view`

`python tests/rust_view_parity.py`

The last command compares Rust outputs against the Python implementation
for multiple summaries, paginated searches, neighborhoods, node details,
and rejected invalid/unauthorized inputs over the same manifest-derived
test graph and untrusted synthetic observation. CI compiles Rust first.

## Important limitations

**This PR does not switch the running Python HTTP endpoint to Rust yet.**
That would require an in-process FFI/PyO3 bridge or a durable Rust
backend service with bounded authenticated request routing, startup and
shutdown semantics, error/recovery handling and benchmarked latency.
A per-request process spawning approach would likely be wasteful and
has intentionally not been adopted.

The Rust View input is generic JSON carrying an untrusted local
source-backed graph. Validation checks counts, identities, dangling
edges and unauthorized execution flags; it is not an authenticated
source or a tenant access-control boundary. We must keep any future
hosted worker behind tenant-specific authentication/authorization.

## Roadmap to Rust majority

1. **Prove view parity and benchmark** serialization plus full query
   latency on increasingly dense source graph fixtures. Expose the
   Rust view library through a persistent, authenticated backend or
   bounded FFI only when it is advantageous and correct.
2. **Port discovery adapters and GitHub provider processing** to Rust
   with byte-for-byte evidence checks, source permissions, quotas and
   revoked-token denial tests. Leave Python as an optional developer
   import/legacy adapter until verified.
3. **Move hosted ingestion, durable graph state, and API serving** to
   Rust with tenant-scoped jobs, encrypted credential storage,
   read-only defaults and auditable export/delete workflows.

## Lean and Bend

Use **Lean 4** for selected policy and integrity theorems. Lean 2 is
an older, unsupported target for this architecture and would introduce
unnecessary proof-framework fragmentation. Existing Lean 4 proofs
are abstract policy theorems; they do not prove Rust refinement.

Treat **Bend 2** as experimental. Evaluate a pure, closed graph
kernel under controlled Rust/Bend correctness and performance
benchmarks before any adoption. Compilation overhead, GPU transfer,
input size, reproducibility and worst-case CPU/memory cost must be
reported; don't add it as a required runtime dependency now.

Neither formally verified end-to-end production operation nor
parallel speedups are claimed by this PR.
