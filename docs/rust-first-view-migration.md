# Rust-native graph queries: opt-in local control room

## First Rust-first migration slice

OntologyEX now has a compiled Rust graph view kernel (`engine/src/view.rs`)
and a bounded CLI (`ontologyex-view`). Until now, the working control-room
HTTP handler executed the matching view projections in Python.

This integration makes the existing local control room optionally execute
**summary, page, neighborhood and node-detail** projections in Rust. It
does not clone a second frontend or create a new network listener.

### Run

    cargo build --manifest-path engine/Cargo.toml --bin ontologyex-view
    python ontology-extraction/scripts/control_room.py \
      --repo /path/to/read-only-business-repo \
      --view-engine rust --port 8765

The default remains `--view-engine python` until this prototype's Rust
request-latency, binary availability and failure handling are appropriate
for general usage. `--view-engine rust` refuses to start if the executable
is missing, symlinked, too large or not executable.

### Execution boundary

Python still owns filesystem manifest discovery, GitHub token handling,
observation input validation and the loopback HTTP server. Rust receives
only the bounded in-memory source graph state serialized to JSON, and
computes the read-only graph projection.

- Fixed local binary path under `engine/target/debug/ontologyex-view`.
- No shell, remote binary resolution, or user-controlled executable path
  from the HTTP API.
- The subprocess **does not inherit the parent's environment**; GitHub
  tokens and other credential variables are intentionally excluded.
- Maximum 16 MiB graph input, 8 MiB output, 64 MiB binary, five-second
  execution timeout, at most two concurrent native requests.
- Native failures return source-unavailable / HTTP 503: **never silently
  substitute a Python result**. Invalid HTTP query syntax is still 400.
- Output validates expected response kind, authorization=false and
  bounded result sizes before it reaches a browser.
- Source trust semantics remain UNATTESTED; CI outcomes are not uptime;
  import/navigation relationships do not prove execution or causation.

### Second slice: incident evidence projection

Rust now also computes the read-only incident evidence record when
`--view-engine rust` is selected. The input is the **same validated
observation batch** used by the existing Python health projector, so
a native incident report never sees a different source timeline.

The native incident module (`engine/src/incident.rs`) uses a minimal RFC3339
parser dependency and enforces 4,096 observation rows; outputs stay bounded
to 32 timeline entries, 40 possibly exposed dependents, 40 relationships
and eight source revisions. It never claims that source revisions are
deployments, static links are executed calls, or that a root cause is known.

`tests/rust_incident_parity.py` asserts **exact equality** with the existing
Python incident projector on nine fixed-clock cases, including future,
stale, conflicting and long observation histories. Actual local HTTP
parity also covers the `/api/incident` endpoint. The Python implementation
remains available as a compatibility path; there is no silent fallback.

### Parity and CI

Existing `tests/rust_view_parity.py` compares Rust to Python for a
synthetic 120-service source graph across summaries, pages, source nodes
and one/two-hop neighborhoods. New `tests/rust_http_smoke.py` runs the
existing loopback server twice, once per backend, using the same
read-only local fixture, and verifies equivalent actual HTTP payloads
apart from expected request timestamps.

`tests/test_native_view_bridge.py` covers credential isolation, fail-closed
native exits/timeouts, malformed/oversized responses, symlinks, invalid
queries, and concurrency limits. The full Python suite plus the existing
Rust/Lean and adaptive Chrome tests remain required.

### What's still Python and why

The repository language share will not become majority Rust because of
this small bridge alone. Manifest/GitHub source adapters, state build,
observation parsing, incident report assembly and HTTP serving remain
Python until corresponding Rust implementations pass strict byte/semantic
parity against the existing test fixtures. Replacing them blindly would
risk breaking discovered identities, source evidence and safety rules.

Next: (1) long-lived native Rust query worker to remove per-request process
startup; (2) native parser for the strict discovery manifest allowlist
with controlled fixture parity; (3) a Rust-only authenticated HTTP plane
once per-tenant access and credential isolation are designed and tested.

## Lean and Bend

Keep **Lean 4** for source-independent policy theorems. Lean 2 is an old,
incompatible toolchain and is not recommended. Bend 2 is not a substitute
for an ordinary Rust API; consider it only after a bounded graph workload
proves a measurable advantage in benchmarks. No Bend runtime was added.

## Scope

Single-operator, loopback-only prototype. Does not deploy production
systems, install cloud infrastructure, authorize writes or claim safety
against a malicious binary running under the same operating-system user.
