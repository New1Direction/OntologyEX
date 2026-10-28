# Rust engine

OntologyEX runs on Python by default: the installed skill is plain scripts with no
build step. The Rust engine is an optional set of binaries that do the same jobs
faster and in less memory, and that **produce the same answers**, which CI checks
on every change.

| Binary | Replaces | Use it for |
|---|---|---|
| `ontologyex-feeds` | `feeds.py` | The live-feeds service: HTTP probes, Prometheus queries, and the OTLP/HTTP trace receiver. Same config, same observations file. |
| `ontologyex-discover` | `discover_repo.py` | Repository scanning. Byte-identical JSON. `control_room.py --view-engine rust` uses it. |
| `ontologyex-view` | graph views in `control_room.py` | Graph pages, neighborhoods, node detail, incidents, link suggestions (`--view-engine rust`). |
| `ontologyex-core` | — | The offline projection kernel (topology + observations → health). |

## Install

Prebuilt binaries for Linux (static, x86_64 and arm64) and macOS (arm64 and x86_64):

```bash
python ontology-extraction/scripts/fetch_engine.py            # latest engine-v* release
gh attestation verify ontology-extraction/bin/ontologyex-feeds --repo New1Direction/OntologyEX
```

`fetch_engine.py` downloads only from github.com over HTTPS, checks the archive
against the release's `SHA256SUMS`, and installs only the four expected binaries
into the skill's `bin/`, where the control room finds them. The checksum proves the
archive is the one listed in the release; the attestation proves GitHub Actions
built it from this repository. Nothing is run during install.

Or build them (Rust 1.85+):

```bash
cargo build --release --locked --manifest-path engine/Cargo.toml
```

The control room looks for binaries in `$ONTOLOGYEX_ENGINE_DIR`, then the skill's
`bin/`, then `engine/target/release`, then `engine/target/debug`.

## Run

```bash
ontologyex-feeds check --config feeds.json --repo /path/to/repo
ontologyex-feeds run   --config feeds.json      # or: once
ontologyex-discover --repo /path/to/repo
python ontology-extraction/scripts/control_room.py --repo /path/to/repo --view-engine rust ...
```

`ontologyex-feeds` reads the same `ONTOLOGYEX_FEEDS` file as `feeds.py` (see
[live feeds](live-feeds.md)) and writes the same observations and status files, so
you can switch between them at any time. It polls probes and Prometheus targets in
parallel (16 at a time) where `feeds.py` polls one by one.

## How much faster

Measured with `tests/bench_rust_engines.py` on a 4-core Linux container (release
builds; your numbers will differ):

| Job | Python | Rust | |
|---|---|---|---|
| Discovery of a 5,900-file repository with 125 manifests (CLI, startup included) | 1.30 s | 0.088 s | 14.8× faster |
| OTLP receiver, protobuf, 4 concurrent clients, 1,000 spans per request | 98,000 spans/s | 1,740,000 spans/s | 17.8× more |
| OTLP receiver resident memory under that load | 41.5 MiB | 8.3 MiB | 5× less |

The receiver test uses a Python client, which likely limits the Rust figure.

## How we know the answers are the same

Python stays the reference. CI runs these on every pull request that touches the
engine, the feeds, or discovery (`rust-proof-foundation.yml`), and the release
workflow runs them again on each native build:

- **Discovery** (`tests/rust_discover_parity.py`): 49 edge-case repositories plus
  1,000 seeded repositories of mutated manifests (8,000 compose files, workflows,
  `package.json`, `Cargo.toml`, `pyproject.toml`). The output must match byte for byte.
  The Rust YAML loader is a port of PyYAML's reader, scanner, and parser, so it
  accepts and rejects exactly what PyYAML does (tabs, `U+0085` line breaks, YAML 1.1
  scalars such as `1:30`).
- **Feeds** (`tests/rust_feeds_parity.py`): configuration validation over ~60 URL
  shapes and 600 seeded config mutations; OTLP decoding of protobuf and JSON with
  gzip and 600 byte-mutated bodies; Prometheus responses; 600 span-window replays;
  store merges byte for byte; `once` end to end; and the live receiver's status
  codes and emitted rows.
- **Kernels** (`tests/feeds_conformance.py`): classification (31,250 cases), store
  retention (4,095), and the p95 rank for every sample count up to 100,000, compared
  across Python, Rust, and Lean.

To make byte-for-byte parity possible, a few Python behaviors were tightened rather
than copied: feed URLs must be printable RFC 3986 text with a plain host and port;
OTLP/JSON and Prometheus bodies must be strict UTF-8 JSON with typed fields;
truncated gzip bodies are refused; YAML manifests may not use explicit tags and
nest at most 64 levels; JSON manifests at most 512; a TOML float that overflows is
an error instead of infinity. Each of these refuses input that was malformed anyway.

## What is proven

`proofs/lean/OntologyEXProofs.lean`, namespace `OntologyEX.Feeds`, checked in CI:

- `down_is_error`, `threshold_inclusive`, `no_rules_ok`: a down target is an error;
  a value equal to a threshold hits it; no thresholds means ok.
- `classify_monotone`: worse metrics never make a sample look healthier.
- `keep_at_most`, `keep_from_input`, `dropped_are_older`: the store keeps at most
  `keep` samples per component, never invents one, and never drops a sample newer
  than one it keeps.
- `p95_index_in_range`: the p95 always names one of the samples.

These are statements about the models. The conformance tables above tie the models
to the Python and Rust code over bounded domains; that is exhaustive testing, not a
refinement proof.

## Known differences

- `ontologyex-feeds` can bind the OTLP receiver to IPv6 (`"bind": "::1"`); `feeds.py`
  binds IPv4 only.
- Stored observation timestamps: the Rust store reads RFC 3339 forms
  (`2026-10-28T08:00:00Z`, `+02:00`, fractions, `HH:MM` without seconds). Python's
  `fromisoformat` also reads rarer ISO 8601 forms. Both feeds write `...Z`.
- Discovery: TOML nested 80 or more levels deep, a workflow `name` with an escaped
  lone surrogate, and non-UTF-8 manifest paths are refused or reported differently.
- Prebuilt binaries cover Linux and macOS. On Windows, use WSL or the Python tools.

## Releasing

Push a tag `engine-vX.Y.Z`. `release-engine.yml` builds the four targets, runs the
parity suites on each native build, and publishes the archives, `SHA256SUMS`, and a
build provenance attestation. Pull requests that touch the engine run the same
builds without publishing.
