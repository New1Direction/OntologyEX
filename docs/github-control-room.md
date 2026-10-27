# GitHub → OntologyEX Control Room (first connected slice)

## Scope

This is the first remote **source-discovery** integration, not a hosted SaaS release.
It reuses the original offline manifest scanner from PR #11 and read-only control-room
UI from PR #14. It neither replaces the four-layer ontology compiler nor changes
its evidence/review/authorization contracts. No live infrastructure is touched.

## One-button prototype

Run with a public repository (unauthenticated GitHub API rate limits apply):

    python ontology-extraction/scripts/control_room.py --github New1Direction/repo.ing

Open the reported loopback URL and click **Map My Business**. The backend reads
the default branch HEAD, pins its Git commit and tree SHA, and retrieves only
allowlisted manifests via GitHub's read API. The browser sees source-linked
nodes and dependency declarations. No repository clone, full Git history,
source execution, build commands, local indexer or large customer file transfer.

For selected private repositories, provide a fine-grained *read-only* GitHub
token as the **ONTOLOGYEX_GITHUB_TOKEN** environment variable on the server
(repository Contents: read). Do not paste tokens into URL parameters, browser
forms, logs or files. GitHub Actions CI reads are not yet requested.

    ONTOLOGYEX_GITHUB_TOKEN=... python ontology-extraction/scripts/control_room.py \
      --github your-org/your-repository --github-refresh-seconds 300

For headless processing:

    python ontology-extraction/scripts/github_discovery.py --repo New1Direction/repo.ing > graph.json

    python ontology-extraction/scripts/control_room.py --github New1Direction/repo.ing --state-only > state.json

**Do not upload graph.json publicly:** file paths, service names and dependencies
can be sensitive even without secrets or raw code.

## What is verified

- Selected repository identity uses GitHub's permanent numeric repository ID,
  preventing cross-repo collision and accidental rename forks.
- Each manifest is tied to a commit and Git tree, and its downloaded bytes
  are compared against its Git blob SHA-1 plus a SHA-256 source fingerprint.
- Source-dependent edges are declarations only. A Compose depends_on edge
  does not prove a live network call, deployed relationship, or downstream outage.
- The inventory explicitly states what was not assessed, including database
  schema internals, cloud deployments, actual runtime health, users and money flows.
- A failed refresh after an earlier success keeps the last graph with a
  STALE_LAST_KNOWN flag; it does not masquerade as fresh.

**GitHub API responses are not an authenticated tenant service.** For private
workspaces, the production implementation requires GitHub App installation
identity, short-lived installation tokens, tenant authorization and auditable
consent. The local env-token prototype alone does not provide tenant isolation.

## Continuous updates without filling a laptop

The local server checks GitHub at a bounded interval (default five minutes,
range 60-3600 seconds), not every browser poll. The UI reuses the last
source-pinned graph; the backend caches it in memory and fetches refreshed
revisions only when needed. The active process must remain running.

**True unattended cloud monitoring is NOT shipped.** Its next phase needs
hosted workers, a durable event queue, GitHub webhooks and reconciliation
polling, a database for versions, per-tenant isolation, scoped graph APIs
and deletion/retention policy. Only after deployment can monitoring continue
while the user's browser and laptop are switched off.

## Security limits

- GitHub REST requests use only the fixed https://api.github.com host and GET.
- Redirects are blocked. No arbitrary URL hosts or path traversal are accepted.
- Large or truncated Git trees are refused with explicit coverage limits.
- At most 6000 source-tree entries, 128 selected manifests, 256 KiB per
  manifest, 2 MiB of manifest bytes and 8 MiB per GitHub API response.
- Temporary server-side selected files are stored in a mode-0700 directory
  and destroyed after parsing, not retained in a user's home directory.
- The scanner never runs project code, installs dependencies or reads .env.
- The browser receives graph descriptions, file paths, source checksums
  and source metadata — no GitHub token, raw manifest body or private key.
- Graph evidence is content-integrity evidence, not human semantic approval.
- Read-only local UI remains loopback-bound and validates Host/Origin.

## Observational status

All nodes remain UNKNOWN unless separately configured, fresh telemetry
observations exist. GitHub repository access alone **never** marks a service
healthy or failed. A report from GitHub Actions could establish CI-run status,
but it would not establish production database or API health.

Use existing control_room.py --observations for a local sample, but treat
these source-reported rows as unauthenticated. A future observability connector
must validate collector identities and map signals to the same component IDs.

## Acceptance tests

    python -m unittest tests.test_github_discovery -v
    python -m unittest discover -s tests -v

The offline test uses a fake GitHub API with realistic blob SHA validation
and the actual existing scanner/control-room state. Tests require no
credential, network connection or production infrastructure. A real public
repo.ing read-only scan can then be run manually using the command above.

PR dependencies: #11 (scanner) → #14 (local control room) → this GitHub bridge.
PRs #9/#10/#8 (typed graph, RDF, impacts) and #16 (Rust/Lean) remain
independent drafts; integrating them is separate review work.
