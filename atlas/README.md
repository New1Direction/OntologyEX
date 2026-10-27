# OntologyEX Project Atlas — usable preview

A source-backed portfolio map you can open right now. This is a **read-only
software inventory**, not a production health monitor or proof of causal impact.

## Try it (zero setup)

Open [index.html](index.html) in a modern browser. The page includes a **public-only
example** drawn from public repositories. Select a project, switch between
Systems / Operations / Dependencies, click nodes for pinned GitHub source
locations, and use "Simulate declared dependency loss" to trace
**declared** reverse dependencies (never runtime causality).

The page is self-contained, with no CDN and no service account. Imports and
exports stay on your device. Its GitHub "Add repo" button uses anonymous,
read-only GitHub API calls and supports **public repositories only**.
Rate limits and repository visibility may prevent an in-browser scan.
**Never paste a GitHub token into the browser.**

## Your private repos in the same viewer

From a checkout of OntologyEX:

1. Sign into GitHub CLI once if needed: \`gh auth login\`.
2. Run this read-only scanner with the exact repositories you want included:

\`\`\`bash
python atlas/scan_github.py OWNER/REPO OWNER/PRIVATE_REPO --out my-atlas.json
\`\`\`

3. Open \`atlas/index.html\`, click **Import**, and select \`my-atlas.json\`.

The scanner also accepts a token from \`GH_TOKEN\` or \`GITHUB_TOKEN\` if you
have configured it in your shell. It sends the token only to
\`https://api.github.com\`, does not print/store it, and writes the snapshot
with user-only permissions (\`0600\` on POSIX), refusing overwrites. No
repository code is executed. PyYAML is optional but required to parse Docker
Compose manifests; the top-level repository dependency pin for OntologyEX
already includes it.

All scan and graph data remains **local** unless you explicitly choose
to share/export it. A private snapshot must **not** be committed or uploaded
to the public OntologyEX repository, pasted into an issue, or hosted on
public GitHub Pages.

### What the scanner actually finds

- Root and monorepo manifests, scripts and a selected set of dependencies
- Top-level source modules and GitHub Actions workflow files
- Cargo crate declarations and local crate hints
- Docker Compose service definitions and explicit startup dependencies
- Pinned Git commit identity, source hashes, and missing-source/parse gaps

It does not discover arbitrary private business documents, verify whether
services are deployed, inspect API responses, run workers, read secrets,
prove call graphs, infer employment structures, or determine financial
solvency. Unknown is **not** healthy or failed.

Every inspected project uses a pinned commit SHA to construct GitHub evidence
links. The Atlas graph separates CONTAINS/DECLARES relationships from
explicit DEPENDS_ON and package dependency links. A hypothetical failure
traversal is not a substitute for runtime tracing, failure injection,
or reviewed incident evidence.

## Current limits and next steps

- Browser preview graph is intentionally bounded and offers no active-version
  governance, user accounts, persistent cloud storage, or background monitors.
- Private repository discovery requires the local CLI (no browser OAuth
  backend and no token pasted into static code).
- To reach a live train-control-style dashboard, add independently timestamped
  OpenTelemetry/service health inputs, correlate to stable component IDs, and
  attach checkable incident evidence. Keep unknown and stale observation states.
- Do not enable autonomous production remediation without an explicit
  authorization/approval boundary and tested recovery procedure.

The Atlas is designed to be a useful first layer **before** those additional
connectors exist. It does not replace the OntologyEX four-layer compiler or
the evidence-based skill review process. Source-side ontology candidates remain
unreviewed until explicitly accepted elsewhere.

## Testing

\`\`\`bash
python -m unittest discover -s tests -p 'test_atlas_scanner.py' -v
\`\`\`

Smoke-tested in Chromium (overview, per-project operations, evidence links,
dependency simulation) and Python unit tests. The scanner itself only uses
Python 3.11+ standard-library modules, with optional PyYAML for Compose.
