# Route and database schema discovery — source-bound preview

## Problem

The first GitHub scanner can describe manifests and Compose dependencies,
but an API-rich application such as repo.ing shows only a handful of nodes.
An operational system map needs to expose its declared API surface and
database schema with honest evidence grades, without executing app code.

## This extension

Using the already pinned GitHub commit and tree, the scanner additionally:

- Adds a route node for each regular app/api/**/route.js, route.ts,
  route.mjs, route.jsx or route.tsx path, up to a fixed 256-path limit.
- Does NOT download or execute route handlers. These nodes mean only
  **a route file exists**. Exported HTTP verbs, handlers, dependencies and
  whether that route runs in production all remain **UNKNOWN**.
- Optionally reads only src/db/schema.mjs if that file fits the existing
  256 KiB source budget, verifies its Git blob SHA, and recognizes bounded
  top-level Drizzle pgTable string-literal declarations.
- Emits table names as SOURCE_SYNTAX_CANDIDATE with the declaration's
  source line. This does NOT establish that the table exists in production,
  that migrations were applied, or that its runtime permissions are safe.
- Tags path-only route evidence as GITHUB_REPOSITORY_TREE_ENTRY:
  its SHA-256 is a hash of the tree *metadata*, NOT the file contents.
- Tags inspected schema evidence as GITHUB_COMMIT_PINNED_BLOB with a
  verified full-file SHA-256. No raw source or customer rows reach the graph.
- Keeps all operational health UNKNOWN without trustworthy telemetry.
- Adds API and Database graph lenses so the browser need not show every
  node simultaneously.

## Scope limitations

Only this explicit Next.js/Drizzle source shape is currently supported.
Do not infer router methods, SQL foreign keys, database migrations,
services, business processes, dependency paths or production health
from filename proximity or an unreviewed regex match.

Unsupported frameworks and incomplete/oversized schema files must
remain visible as a coverage gap. No tables or routes are invented to
fill those gaps. A future parser can add AST-derived semantics and an
actual read-only database metadata connector to verify deployed tables.

With large businesses, a future hosted API must send only graph tiles
relevant to the active lens, not the complete graph to the browser.
This prototype still serves a bounded graph JSON from a local backend.

## Verification

Offline tests cover path metadata identity, unchanged source bytes not
downloaded for routes, schema matching and line evidence, corruption
gaps, stable identities, graph integrity, cache persistence, and no
fabricated runtime status. A separate read-only repo.ing smoke test
requires real API route paths and at least one schema candidate.

These are unreviewed source facts/candidates. They are not a complete
ontology of the real deployed business or a formal proof.

## Stacked dependency

PR #11 local manifests → #14 local control room → #17 Github →
#18 durable cache → this PR. No production deployment or merge.
