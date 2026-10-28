# Second-hop source lineage — module imports and schema declarations

## What changed

OntologyEX now explores **one additional bounded import hop** after its
source-pinned API route scan. The displayed claim is precise:

API route (file) --STATIC_IMPORTS--> backend module (file)
  --STATIC_IMPORTS--> schema module (file)
  --DECLARES_TABLE_CANDIDATE--> database table declaration (source syntax)

The first two kinds of arrows mean a static relative import appears in
source code whose bytes matched the pinned Git object. The final arrow
means a simple Drizzle pgTable declaration was recognized in
`src/db/schema.mjs`. It does **not** mean that backend code read, wrote,
or queried any of the tables; the table may not exist in the live DB.

## Bounded discovery contract

- Select at most 36 API route files from the pinned tree.
- Traverse **one more hop** from at most 48 modules directly imported
  by those routes. 32 selections favor frequently imported modules; up
  to 16 additional selections cover `src/` business modules.
- Read up to 96 KiB per module and 2 MiB total for second-hop discovery.
- Add up to 384 second-hop static import edges and a total of 256 unique
  source-module nodes across both phases.
- Resolve only same-repository relative paths which actually exist in the
  pinned Git tree. Do not execute source code, npm scripts, build steps,
  network imports, dynamic code, SQL or application functions.
- Preserve per-edge source-file hash and source line, including unresolved
  references and skipped/unsupported input counts.
- Imported target files remain *tree metadata* nodes unless their source
  bytes were actually read for a source-backed edge.
- On a failed/oversized read, emit a named coverage gap and retain UNKNOWN
  rather than pretending to have completed the import graph.

## What is not proven

Static imports are necessary evidence for many dependency paths, but
do not establish execution paths, HTTP verbs, SQL queries, production
connectivity or operational health. The limited import recognizer is not
a full JavaScript parser and may miss dynamic/multiline imports or match
unusual comment layouts. Every parsed import remains an unreviewed
`SOURCE_SYNTAX_CANDIDATE`; downstream automated repair is prohibited.

Source files may include sensitive secrets. The scanner processes
bounded bytes on the backend, emitting only paths, SHA hashes, line
numbers and candidate claims; no raw code bodies are sent to the UI.
Private repository deployments still require tenant-authorized
GitHub App credentials and isolated workers before hosted release.

## Verification

Offline fixtures test a real two-hop chain, byte-corruption refusal,
missing imports, bounded module reads, stable source identity,
persistence and that no runtime usage claims are created.
CI additionally scans the public repo.ing source via GitHub's read API
with blob verification. The CI evidence concerns static source only;
actual production database health remains UNKNOWN.

## Next engineering direction

To establish specific table usage, use an AST parser to identify
Drizzle query-building operations and tie them to declared imports.
Treat those as source-code candidates until confirmed with
authenticated OpenTelemetry traces and database telemetry.

## Stacked history

Built on drafts #11 → #14 → #17 → #18 → #19 → #20.
No branch is merged or deployed automatically.
