# Verified static imports — API-to-backend source topology

## Purpose

Move the Control Room beyond hundreds of isolated route and table nodes.
Only **direct source-level import relationships** are claimed. This is
NOT runtime call tracing, a verified path to the live database, a dependency
completeness guarantee, or evidence that an imported module actually executes.

## How it works

The GitHub adapter starts with its commit-pinned, hash-verified tree. It
selects up to 36 representative route files across the alphabetically
sorted set (a deterministic spread rather than only the first routes).
Each selected file is read through the existing Git blob SHA verification,
bounded to 256 KiB per file and 768 KiB total.

An intentionally narrow, anchored static-import recognizer identifies
single-line relative ES import statements. Relative paths are normalized
inside the tracked repository and resolved **only when the target exists
in the same pinned Git tree**. Targets create SOURCE_MODULE path-only nodes;
imports become STATIC_IMPORTS edges with exact source-file digest and line.
External packages are not mapped; computed/dynamic imports and multiline
statements are not interpreted. Import syntax is SOURCE_SYNTAX_CANDIDATE.

The result can show concrete links such as API route -> source module, with
the recorded direct source declaration. Clicking the node exposes the
source path and line. The browser never receives route body contents.

## Evidence distinctions

- API route: path-only existence via Git tree entry; code not downloaded
  unless that route was selected for import analysis.
- Imported target module: path-only existence via Git tree metadata.
- STATIC_IMPORTS: verified route source blob hash, exact matching source
  line, and an import specifier resolving to an existing tree path.
- No node or edge is a proved live service, data access, or actual API call.

## Coverage and safety

Scans max 36 routes, 512 import edges, 256 unique targets, 768 KiB total
route source bytes, 256 unresolved references; the rest are explicitly
reported as not inspected. No eval, subprocess, builds, or arbitrary
network paths. The only network reads use the existing fixed GitHub API
origin and immutable blob identities.

Unresolvable imports are presented as unresolved rather than invented.
HTTP health is UNKNOWN unless a separate authenticated operational source
exists. Build and CI status are separate from production health.

The narrow syntax recognizer can misread strings and comments in unusual
files. Therefore every import remains marked a candidate for AST-level
confirmation and should never drive autonomous remediation. Real runtime
relationships need OpenTelemetry spans, service graphs and sampled traces
from independently authenticated production instrumentation.

## Integration

Uses existing PR #19 API and schema node surface, and the private snapshot
cache from #18. Adds importer coverage to the database and API lens,
not a new graph store or monitoring backend.

## Validation

Unit tests use commit-pinned fake GitHub blob responses and an actual
source-backed graph. CI uses a real, read-only repo.ing source scan and
requires STATIC_IMPORTS edges without claiming production status.

## Next

Follow import references one additional bounded hop to find explicit
module -> schema imports. Separately build cloud-hosted authentication,
per-tenant indexing and actual OTel source status.
