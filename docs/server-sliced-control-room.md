# Server-sliced Control Room — browser-light source graph

## Objective

The adaptive Control Room must not download an entire business graph merely
to display 90 nodes. The existing `build_state()` still owns source-backed
topology and status server-side. Its new HTTP projection layer returns bounded
data only for the active search/lens/selection.

## API contract (local prototype)

| Endpoint | Response | Limits |
| --- | --- | --- |
| `GET /api/state` | Small health/coverage summary and up to 18 alerts | No entire nodes, edges, or raw source-evidence inventory |
| `GET /api/view?lens=all&q=&offset=0` | Atomic status summary plus selected lens/search page with internal links and needed evidence | <=90 nodes, <=300 edges, bounded page offset |
| `GET /api/node?id=...` | One verified source-backed node with linked evidence | <=40 adjacent edges; explicit omitted count |

Supported lenses: all, api, database, services. Search is ASCII-only and
limited to 90 characters. Offset is an integer from 0 to 10000.
Invalid/unknown parameters fail closed with HTTP 400, unavailable source
with HTTP 503. URLs never accept arbitrary hosts, paths, SQL, or code.

Results always include counts and omitted-edge indicators rather than
pretending that a partial graph is complete. Critical incidents remain
in the status summary, including incidents outside the current graph page.
Selecting one retrieves its actual bounded evidence and relations.

HTTP is GET-only and bound to loopback. CSP, Host and Origin checks
from the existing control room remain in place. No hosted OAuth or
multi-tenant security guarantee is implied. Private graph labels, file
paths and relations are sensitive even without source-code bodies.

## Client behavior

One screen uses the preexisting adaptive mathematical layout engine.
Map My Business loads one atomic summary-and-graph page response, avoiding source-version mismatch between separate requests.
Search/lens changes execute on the server. Out-of-order replies cannot
overwrite newer selections. Previous/Next controls page through results.
Requesting detail never requires the full graph.

A second defensive 90-node limit protects SVG rendering even if a faulty
upstream source exceeds the declared page budget. The UI still reports
UNKNOWN or stale health when source or telemetry is missing. No
remediation or infrastructure write paths are available.

## Real repo.ing evidence

GitHub Actions read-only discovery of the public repo.ing commit reported
401 nodes, 863 edges, 11 source manifests and production health UNKNOWN.
In the same CI run, byte counts of compact JSON responses were:

| Payload | UTF-8 bytes |
| --- | ---: |
| Previous entire computed control-room graph state | 659,045 |
| New atomic summary and first 90-node graph page | 85,932 (pre-atomic split-response measurement; CI reruns with the atomic envelope) |
| Reduction in this initial response comparison | 86.96% |

This is a measured **initial payload reduction**, not a claim of
86.96% faster browser paint, lower total traffic across an entire
session, reduced server memory, or lower ingestion CPU.
Later pages and node inspection incur additional bounded requests.

## Verification

- Python regression suite on Python 3.11 and 3.13.
- Pure projection regression tests: no full graph in summary, bounded
  pagination, deterministic filtering, search, source evidence integrity,
  missing component refusal, unknown health and edge truncation.
- Real headless Chrome: six mobile/tablet/desktop viewports, keyboard
  and graph controls retained, 405-node local Compose fixture,
  reported synthetic failure, paginated searching, and on-demand evidence.
- Previously verified resizing, collapse, reduced-motion preferences and
  mathematical layout tests run unchanged.
- Actual read-only repo.ing CI scan tests response-byte reduction.

## Remaining limitations

1. The local Python process still holds all graph nodes and recomputes
   status on each API request. Large multi-tenant businesses will need
   a remote indexed graph store, background materialization and bounded
   per-tenant query budgets.
2. A page currently contains only edges with both endpoint nodes inside
   that same page. Node detail exposes other bounded relations, but a
   future **neighborhood lens** should request adjacent nodes together.
3. The local source scanner and observations are not authenticated
   hosted data; live runtime data and tenant isolation are not shipped.
4. Browser checks target headless Chrome; Firefox/WebKit and real mobile
   screen readers still need independent tests.

## Dependency / merge gate

PR #23 is stacked on #22 and the previously unmerged discovery
branches. Keep as a draft until the dependency chain is reviewed,
rebased, and CI rerun against main. This is not a production deployment.
