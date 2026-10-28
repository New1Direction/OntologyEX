# One-click business discovery: Phase 0 (experimental)

**Product goal:** connect a source, click "Build my business graph", and see a
useful, source-linked view of the systems and processes the product can
actually substantiate. The full experience will need consented connectors,
real observations, reviewed semantic discovery and a durable job lifecycle.

This PR deliberately implements the **first offline source inventory**,
not the entire business graph. It is the read-only, fast and deterministic
preview that must precede AI synthesis and live monitoring.

## Run now

From an OntologyEX checkout (Python 3.11+ and the existing pinned PyYAML):

    python ontology-extraction/scripts/discover_repo.py --repo /path/to/your/repository > preview.json

Result: deterministic JSON with project, source_manifest, workflow and
declared_service nodes, and source-backed DECLARES and DEPENDS_ON edges.
Service dependencies come **only** from Docker Compose depends_on declarations;
they are NOT proof of network calls, deployed topology or runtime behavior.
Every source carries its relative path and SHA-256 digest. The preview
does not include file content, environment values or source code.

The scan recognizes package.json, pyproject.toml, Cargo.toml, go.mod,
Dockerfile, compose.yml/.yaml, docker-compose.yml/.yaml, and root
.github/workflows YAML files in a bounded monorepo walk.

Root- and subproject-level source manifests are discovered; the scanner
intentionally does NOT interpret whole codebases, natural-language policy,
dependency packages or business semantics. A package.json node means that
manifest exists, not that a service is deployed or healthy.

## Coverage and unknowns are first-class

The output is marked:

- kind: UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW
- status: PARTIAL_SOURCE_DECLARATION_MAP
- runtime_health: UNKNOWN
- human_review: REQUIRED
- execution_authorized: false

Coverage shows inspected file count, manifest candidates, bytes read,
file evidence, and skipped symlinks. Parse errors, source size limits
and references to undefined services remain visible under gaps/unresolved.
Not-assessed areas include people, permissions, business processes,
financial flows, production resources and non-manifest source code.

**Unknown is not healthy and observed manifests are not independently
authenticated.** Don't use this preview to grant access, authorize an agent,
choose an operational remediation or claim a complete business map.

## Safety

- Offline, no network requests, no agent calls, no subprocess invocation.
- Does not read .env, private key files or arbitrary source files.
- Traversal skips symlinks, dependencies, build outputs and hidden folders
  (except root .github workflows).
- Bounds scanned files (6000), candidate manifests (128), bytes per file
  (256 KiB), total bytes read (2 MiB), directory depth (6).
- YAML anchors/aliases and duplicate mapping keys are rejected.
- The source snapshot is not a sandbox, and a hostile process changing the
  filesystem concurrently could still cause a time-of-check/time-of-use
  race. Run on a disposable snapshot for untrusted repositories.

This is an independent original implementation, **not** copied from
WorldMonitor (AGPL-3.0-only). Its influence is product architecture:
fast deterministic signal extraction, explicit source gaps, and
degraded-but-honest initial results.

## Next phases (not shipped here)

1. **Connect & consent:** GitHub OAuth App, scoped repository selection,
   source inventory, permission overview and reversible consent.
2. **Asynchronous discovery:** durable job stages, source checksums, progress,
   failure recovery, cancellation, per-connector backpressure and budgets.
3. **Domain inference:** separate model-authored candidate nouns, actions
   and relationships, paired with exact selected evidence and independent
   structural checks. Never auto-promote candidates.
4. **Business crosswalk:** connect code paths to APIs, database tables,
   tickets, documents, workflows and responsible teams with explicit
   mapping confidence and reviewer history.
5. **Monitor & visualize:** scoped graph layouts and layered lenses,
   telemetry collection, health freshness, incident chronology, counterfactual
   impacts, and a visible list of missing sources.
6. **Agent surfaces:** read-only MCP and CLI for querying evidence and
   explaining relationships; production write-backs under separate
   authenticated approvals.

The single-click UX should render the first source-backed preview as soon
as available, with subsequent enrichment and coverage-state updates.
It must not block rendering while an LLM tries to infer the company.

## Testing

    python -m unittest tests.test_discover_repo -v

The tests cover Docker Compose declared dependencies, missing targets,
bounded scanning, monorepo discovery, output determinism, relative
evidence fingerprints, symlink exclusion, secret exclusion, malformed
JSON/YAML, alias/duplicate refusal, and limits.

No model benchmark or multi-source production scan is claimed.
