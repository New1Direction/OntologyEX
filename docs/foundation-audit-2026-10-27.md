# OntologyEX foundation audit — 2026-10-27

## Decision

Do **not** rewrite the working Python ontology compiler or deploy draft
monitoring code to production. Land one reviewed, testable slice at a time.
The desired hosted business operating system is much larger than the
current implementation; only concrete evidence-backed capabilities are counted.

## Existing main branch

| Component | Grounded observation | Decision |
| --- | --- | --- |
| Four-layer ontology schema and validator | Exists, with source/test/task/app bindings and example tests | Preserve |
| Evidence compiler | Deterministic snapshots, hashes, explicit unknowns and reviewer handoff | Preserve; source hashes are not truth |
| Provenance sidecar | Source origin/group declarations with unknown independence | Preserve |
| Trace extraction | Bounded experimental draft only, locally exported GenAI spans | Do not treat as live collector or approved policy |
| Hosting, background jobs, database metadata, cloud resources | Not present on main | Build incremental services later |
| Cloud object-storage workspace, backups and deletion | Not present | Design only until security review |

## Existing open draft PRs

| PR | Contribution | Boundary |
| --- | --- | --- |
| #8 | Declared-reference change impact planning | Not runtime causal truth or full semantic closure |
| #9 | Version-scoped typed evidence graph | Describes authored concepts, not running instances |
| #10 | RDF evidence interoperability, built on #9 | RDF syntax isn't proof of logical correctness |
| #11 | Bounded local manifest discovery | Partial static system declarations only |
| #14 | Loopback-only control room, atop #11 | Accepts source-reported observations; no trusted production collector |
| #16 | Independent Rust graph reference core and Lean4 model proofs | Abstract proofs do not establish Rust/Lean refinement |
| #17 | GitHub commit-pinned remote manifest scanner, atop #14 | GitHub token is server env only; no hosted app OAuth or tenants |

All of these remain separate draft PRs, not merged. PR dependencies should
be landed in order where applicable: #11 → #14 → #17 and #9 → #10.
#8 and #16 can be reviewed separately after tests and security boundaries.

## Real-world repo.ing evidence

On GitHub Actions CI, the adapter read New1Direction/repo.ing via the
GitHub API without cloning it or executing its source. It pinned the tree
and checked downloaded Git blob hashes. The successful smoke run discovered:

- 11 allowlisted manifests
- 12 graph nodes
- 11 graph edges
- Production health: UNKNOWN

This is **not** proof that all databases, services or deployments were
discovered. The coverage contract explicitly reports those missing surfaces.
The CI job never connects to repo.ing production systems or modifies them.

## Security blockers before a hosted beta

1. Tenant authentication and repository authorization through a GitHub App
   installation, not a shared local environment token.
2. Per-tenant connector tokens, short lifetimes, encrypted secret storage,
   signed webhook validation, cross-tenant isolation and revocation tests.
3. Bounded remote jobs with durable IDs, cancellation, retries, quotas,
   backpressure, signed provenance events and deletion/retention control.
4. Database discovery by metadata-only credentials and strict read-only SQL
   allowlists. No customer row storage without explicit consent.
5. Trusted observability adapters with stable service IDs, clock handling,
   freshness, telemetry provenance, and SLI expectations.
6. Threat modeling for static artifact parsing, malicious repositories,
   prompt injection, path disclosure, and data export APIs.
7. Authenticated project-level graph API; lazy tiling and server-side
   graph slicing instead of transferring a company's entire graph.
8. Controlled outages and false-positive tests for causal explanation.
   A declared dependency is only possible impact, never a verified outage.
9. Remote object store, verified restore drills, deletion confirmation
   and tenant-isolated export are still not implemented.
10. No autonomous remediation or infrastructure writes before an external
    approval and auditable policy enforcement boundary is complete.

## Next bounded vertical slice

After merging the read-only discovery stack, add an authenticated GitHub App
installation + durable background job as the actual hosted one-button entry
point. Do not introduce a parallel ontology engine or Bend2 production
dependency. Use Rust for hosted graph/worker operations after validating
parity with existing deterministic Python source/evidence contracts.
Lean4 remains the model verification layer with explicit correspondence
tests; Bend2 remains a benchmark-only candidate.

### Acceptance gates

- Source permissions limited and revoked credentials immediately rejected.
- GitHub repo snapshot and version-bound edges map to stable tenant-scoped
  IDs; GitHub renames do not create a new company project.
- The web UI reports missing sources and unobserved health as UNKNOWN.
- Two different tenants cannot read or link each other's graph IDs.
- A repo.ing development outage can be mapped to measured evidence and
  possible dependents without unsupported causal claims.
- No local full clone, CI/source code execution or laptop-bound indexer.
- Storage, network and compute usage measured under a fixed test budget.
- Production deployment remains a separate explicit approval.
