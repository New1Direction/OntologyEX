# Source-backed neighborhood navigation — control room

## Purpose

Ontologies must be navigable. A paginated graph can separate two endpoints
across different pages even when the source graph contains a direct
relationship between them. The new neighborhood API selects related
source-backed nodes by server-side identity, without downloading the whole
business graph to the browser.

## Read-only API

    GET /api/neighborhood?id=<existing-node-id>&depth=1
    GET /api/neighborhood?id=<existing-node-id>&depth=2

Both calls use the existing single-operator loopback-only HTTP server and
its Host/Origin protections. The endpoint requires an exact discovered ID
and a depth of 1 or 2. Invalid depth or missing IDs fail closed.

### Response limits

- Up to **90** nodes, including the selected center.
- Up to **300** directed source relationships among included nodes.
- Only source evidence needed for the returned nodes and edges.
- Bounded one/two-hop exploration over the existing in-memory graph.
- Direct incoming and outgoing edge counts for the center.
- Explicit counts of truncated candidate nodes and omitted internal edges.
- A boolean indicating whether the view is complete **within the
  available source graph and requested depth**, never an entire-business
  coverage claim.
- The matching status summary is computed from the same state evaluation.

## Crucial semantic rule

The **navigation traversal is bidirectional**, but the original graph
edges remain **directed**. For example:

    web --DEPENDS_ON--> api --DEPENDS_ON--> database

A user can enter the graph at the database and navigate toward its
declared consumers, but this does **not** imply that the database called
the API. An import edge shows import syntax, not execution.

Candidate source relations like STATIC_IMPORTS and
DECLARES_TABLE_CANDIDATE retain their explicit evidence levels.
CI status is not service uptime. None of these relations establish an
observed runtime call, a verified database query or a root cause.

Generic project-root DECLARES edges are displayed as one-hop evidence.
During deeper navigation, the project root is not used as an intermediary
to shortcut between hundreds of unrelated nodes. This prevents
apparently meaningful two-hop routes that exist only because every
source artifact belongs to the same project.

## Interface

Click a graph node or an incident item to focus its connected source
neighbors. Switch between 1-hop and 2-hop views. The selected center,
directional edge arrows and relationship types remain visible. A
Back to list button restores the paged, searchable source inventory.
Clicking another neighbor navigates again.

The source evidence detail is still retrieved independently and capped.
If an API fails, the UI marks the view unavailable instead of constructing
dependencies from labels or guessing production health.

The seven-panel adaptive control room, graph width preferences,
reduced-motion support, bounded SVG rendering and other functionality
from PR #22/#23 remain unchanged.

## Tests

- Python unit tests cover web/api/database source declarations, reverse
  navigation, one-hop vs two-hop differences, cycles, edge directions,
  deterministic ordering, high-degree graph truncation, no fabricated
  failures, rejected depths, and valid source evidence.
- Chrome tests use the actual 405-node read-only local Compose fixture,
  paging/search, on-demand detail, two-hop navigation and back-to-list.
- Actual repo.ing GitHub CI scans a pinned source graph, locates a
  source-backed API-to-module STATIC_IMPORTS relationship and validates
  its bounded neighborhood with UNKNOWN production health.

## Limits and next steps

1. The local server reconstructs adjacency per query from its bounded
   in-memory state. Larger, remotely hosted graphs need an indexed
   per-tenant adjacency store and explicit request/compute budgets.
2. The graph is a source-backed structural model, not the observed
   runtime dependency graph. Read-only authenticated OTel telemetry
   and database/deployment connectors are needed to establish execution.
3. High-degree neighborhoods are capped, prioritizing direct source
   and reported-risk relationships; omitted counts are always shown.
4. A neighborhood can include nodes outside the selected search lens
   because it represents real graph adjacency, not search membership.

All work remains draft, unmerged and undeployed. This change is stacked
on #23 and earlier OntologyEX discovery/layout PRs.
