# OntologyEX engine benchmark

Measured 2026-10-29T16:27:57Z on Intel(R) Xeon(R) Processor @ 2.80GHz (4 usable CPUs, 15.7 GiB), Linux-6.18.44-fc-v185-x86_64-with-glibc2.39; CPython 3.13.17; rustc 1.97.0 (2d8144b78 2026-07-07); release build; commit 1745e81907d8.

Time, cost and correctness are reported apart. Times are medians with the p10–p90 range (nearest rank) in brackets. A comparison is given only when both engines gave the same answers.

## Repository discovery

CLI to CLI, process start included, on a generated repository (synthetic). Input: files 5,900, manifests 125.

| | Python | Rust |
|---|---|---|
| Wall time | 1.54 s (1.38 s–1.66 s) | 87.6 ms (78.8 ms–99.6 ms) |
| CPU time | 1.52 s (1.36 s–1.64 s) | 85.5 ms (77.0 ms–99.0 ms) |
| Cores used | 0.99 | 0.98 |
| Peak memory | 24.4 MiB | 11.6 MiB |

**Answers: SAME.** Byte-identical output in every run (1,319,391 bytes, sha256 9cd6efd8d929).

Rust: 17.6× faster, 17.8× less CPU, 2.1× less memory.

## Entity context, every entity of the demo

As the control room computes it: Python in its own process, Rust one ontologyex-view process per answer; the demo bundle (synthetic). Input: entities 16.

| | Python | Rust |
|---|---|---|
| Wall time | 2.1 ms (1.9 ms–2.4 ms) | 81.6 ms (66.9 ms–86.8 ms) |
| CPU time | 2.1 ms (1.9 ms–2.2 ms) | 86.2 ms (68.2 ms–90.5 ms) |
| Cores used | 1.0 | 1.04 |
| Peak memory | not measured | not measured |

**Answers: SAME.** The same answer for all 16 entities, in every run.

Rust: 40.0× slower, 41.7× more CPU.

## OTLP receiver under load

OTLP/HTTP protobuf from Python client processes on the same machine, a fresh receiver per run (synthetic spans). Input: spans per request 1,000, clients 4, seconds 8.0.

| | Python | Rust |
|---|---|---|
| Spans per second | 66,941 (64,275–69,338) | 2,318,115 (2,278,457–2,336,525) |
| Receiver CPU, cores used | 0.99 | 1.78 |
| Spans per CPU-second | 67,588 | 1,288,264 |
| Peak memory | 45.9 MiB | 11.2 MiB |
| Client processes, cores used | 0.08 | 1.07 |
| Failed requests | 0 | 0 |

**Answers: SAME.** Every acknowledged span was counted, and the observation each engine emitted after the load equals the Python reference window computed on the same spans.

Rust: 34.6× the throughput, 19.1× the work per CPU-second, 4.1× less memory.

## Caveats

- None found.

## Conditions

| | |
|---|---|
| ontologyex-discover | release, 901,368 bytes, sha256 `69349610548e721c` |
| ontologyex-view | release, 1,322,904 bytes, sha256 `7620c7ed2dd585a4` |
| ontologyex-feeds | release, 3,857,616 bytes, sha256 `e3669005bf0907a3` |
| Engine crate version (engine/Cargo.toml) | 0.1.0 |
| Load average at start | [1.29, 0.81, 0.48] |
| Settings | repo_modules 165, runs 10, context_runs 10, receiver_runs 3, seconds 8.0, clients 4, quick False |

Reproduce: `python tests/bench_rust_engines.py --engine-dir engine/target/release` after `cargo build --release --locked --manifest-path engine/Cargo.toml --bins`. Numbers depend on the machine; the answers should not.
