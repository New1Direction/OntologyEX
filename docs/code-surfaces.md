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

## Local repositories

For a local checkout, `ontology-extraction/scripts/code_surface.py` reads every source
file (bounded: 1 MiB per file, 4 MiB per schema file, 96 MiB in all) and recognizes
declarations by syntax across common frameworks: Next.js app and pages routers,
Remix / React Router file routes, Express-style routers, Flask, FastAPI, Django,
Go (net/http, chi, gin, echo), Spring, Rails and Laravel routes; GraphQL operations
and tRPC procedures; Prisma, SQL, Django, SQLAlchemy, Drizzle, TypeORM, Mongoose,
Sequelize, Rails and JPA tables; Celery, BullMQ, cron and Inngest jobs. The control
room draws routes, operations and tables under the app whose folder holds them (256
per kind, API before pages; a gap counts the rest), and `overview.py scan` lists them
for the agent. Each is a SOURCE_SYNTAX_CANDIDATE with its file and line, never a
verified endpoint or table: a route may be disabled, a table dropped by a later
migration, and anything built at runtime is not seen.

### Syntax trees

With the Rust engine installed (`fetch_engine.py`, or a local build), Python,
JavaScript/TypeScript and Go files are read from syntax trees (tree-sitter grammars compiled
into `ontologyex-discover`; nothing is downloaded or run) instead of patterns. `overview.py scan`
uses it when present, and so does the control room when discovery runs on Rust. What changes:

- **Comments, docstrings and strings are not code.** A route in a docstring example or a
  commented-out line is no longer counted, nor a table name inside a string.
- **Each router keeps its own prefix.** FastAPI `APIRouter(prefix=...)` and Flask
  `Blueprint(url_prefix=...)` per router (and `include_router` / `register_blueprint` with a
  prefix in the same file), Express `app.use("/api", router)` and Hono `app.route(...)`,
  gin and echo `Group`, chi `Route("/x", func(r chi.Router) {...})`, gorilla
  `PathPrefix(...).Subrouter()`.
- **More route shapes.** Calls and decorators spanning lines, Express `router.route("/x")
  .get().post()` and Hono chains, Fastify `route({method, url})`, gorilla `.Methods("POST")`,
  Go raw-string paths, and Next.js handlers re-exported as `export { handler as GET }`.

Other languages and files (Ruby, PHP, Java/Kotlin, GraphQL, Prisma, SQL, schedules) are still
read by patterns, and so is everything when the engine is missing or predates the `surface`
command; `code_surface.py` says which in `parser`. On the patterns' own test fixture both give
the same answers, which CI checks; elsewhere the trees are the more precise reading. Run
`python ontology-extraction/scripts/code_surface.py --repo REPO --engine python` for the
patterns alone.

## Scope limitations

In GitHub mode, only this explicit Next.js/Drizzle source shape is currently supported.
Do not infer router methods, SQL foreign keys, database migrations,
services, business processes, dependency paths or production health
from filename proximity or an unreviewed regex match.

Unsupported frameworks and incomplete/oversized schema files must
remain visible as a coverage gap. No tables or routes are invented to
fill those gaps. A read-only database metadata connector could later verify deployed tables.

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
