# Native Rust-backed graph queries (opt-in, in-process)

## Why

OntologyEX is moving toward a **Rust-majority backend**, not artificially
altering GitHub Linguist percentages. First the standalone Rust query kernel
was added and differentially tested in #35. This change allows the existing
Python local Control Room to execute query projection **inside the Rust
library**, rather than spawning a Rust process per request.

## Architecture

Rust `ontologyex-core::view` owns the bounded algorithms for graph summary,
pagination, 1/2-hop neighborhoods and node detail. A small, safe PyO3
extension (`engine/python/`) accepts an at-most-16-MiB JSON view of the
existing Python state and returns its JSON result. Python remains the
discovery adapter/HTTP shell; no extra server is created.

Settings:

- Default `ONTOLOGYEX_VIEW_BACKEND=python`: unchanged current behavior.
- `ONTOLOGYEX_VIEW_BACKEND=rust`: opt in to the compiled Rust extension.
- Unknown value or missing requested extension: fail visibly rather than
  silently claim a Rust-backed system or guess an answer.

Build example (developer environment with Rust, maturin and Python):

    python -m pip install maturin
    maturin build --release --manifest-path engine/python/Cargo.toml
    python -m pip install engine/python/target/wheels/ontologyex_native-*.whl
    ONTOLOGYEX_VIEW_BACKEND=rust python ontology-extraction/scripts/control_room.py --repo /path/to/repo

The wheel uses PyO3's stable Python 3.11+ ABI. It has no separate HTTP
listener or background native daemon. Native mode still computes the
source-backed status using the existing Python collector, whose inputs
are not independently authenticated.

## Verification

`rust-native-view.yml` compiles an ABI3 wheel on both Python 3.11 and
3.13, installs it into the CI environment, compares a dozen-plus native
outputs against the Python contract on a 400+ node fixture, verifies
fail-closed invalid inputs and runs the actual Chrome control-room
acceptance with native mode enabled.

Tests verify behavior rather than assuming Rust always yields a speedup.
The current adapter serializes the complete in-memory state into JSON
for each native call, so marshaling overhead may outweigh acceleration
at small graph sizes. **Do not turn on by default until benchmarks of
realistic long-running workloads show a material benefit.**

## Safety

- Rust native functions do not access credentials, files or network.
- The existing Python HTTP server remains loopback-only/GET-only.
- Source metadata still exposes sensitive identifiers; a hosted SaaS
  needs authenticated tenant-scoped query and row/edge access controls.
- This is not an automated proof of correspondence with Lean 4.
- No application production deployment or live data is modified.

## Rust majority roadmap

1. Eliminate repeated full-graph serialization with a long-lived native
   indexed source-graph object that receives incremental revision updates.
2. Replace Python discovery/GitHub processing and durable state writes
   with least-privileged Rust connectors after evidence parity tests.
3. Build an authenticated, tenant-isolated Rust HTTP/worker service; keep
   Python only for optional import tooling and high-level adapter scripts.

Lean 4 remains the formal policy layer. Bend 2 is deferred until reproducible
Rust-vs-Bend pure-kernel correctness and performance tests demonstrate
an advantage. An old Lean 2 toolchain would not improve this architecture.
