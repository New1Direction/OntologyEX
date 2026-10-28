# Adaptive Control Room — mathematical layout v1

## Motivation and reference

Calculus of Interfaces (https://calculus-of-interfaces.vercel.app/) describes
a hand-built numeric UI: f(viewport,props) -> exact component geometry,
plus derivative, relaxed and interval evaluators. Its own documentation
states it does not automatically compile arbitrary React/HTML.

chenglou/handwired (https://github.com/chenglou/handwired) likewise
requires explicit numeric circuit builder programs. It is an interesting
research direction, not a necessary runtime dependency for OntologyEX.

**We borrowed the mathematical layout contract, not the circuit engine.**
No handwired source or dependency was imported; there is no claim of
formal verification of CSS or browser text layout.

## Current integration

The existing Python-served, dark Control Room (control_room.py) now serves
a tiny standalone browser/Node geometry module: `layout_engine.js`.
Seven physical screen panels are arranged by f(input):

- Source-backed dependency map (preserved graph and lens controls)
- Incidents and possible dependency risks (critical)
- System integrity and unknown/stale counts (critical)
- Selected source evidence and metrics
- Coverage and explicit blind spots
- Stored source revisions
- Operational boundary / safety disclosures

Critical panels always exist, have minimum dimensions, and move before the
map in a single-column/mobile view. The graph itself is never removed.
Map and incident click targets, search, and lens selection remain reachable.

The user-selected Focus graph preference persists only a harmless boolean
in localStorage; no credentials, graph data, or source files are persisted
in the browser. Calculated positions adapt to resize and graph density.
CSS transitions obey prefers-reduced-motion: reduce.

## Mathematical model

Input:

    width, height, connected entity density, panel min sizes,
    preferred sizes, items, critical priorities, pinned graph

Output:

    rect[id] = (x, y, width, height), contentHeight, column count,
    candidate scores and decisions, explicit constraint report

Candidate arrangements use one, two or three columns when minimum size
constraints permit. Deterministic, bounded candidate scoring minimizes
a weighted combination of vertical overflow, early access to critical
panels and inappropriate geometry. It never silently removes a panel.

Important: the score is a hand-authored **heuristic objective**, not a
globally optimal solution for arbitrary panel sets. It evaluates three
layout families, not a continuous global search.

The verifier checks minimum dimensions, missing panels, canvas bounds
and intersections. A separate browser verifier checks actual DOM
coordinates and whether overflowing content has a scroll mechanism.
These are measured assertions, **not mathematical proofs** across
all font sizes, zoom levels, and browser engines.

## Density

The previous SVG drew every visible graph node and grew by 76 pixels
for each node in its densest column. The current map draws a maximum
of 90 search-matched nodes at once, each on 64-pixel rows, with a
clear 'Showing X of Y' count and a search control for all entities.
Critical incidents are also shown in the separate prioritized list.

This is a bounded rendering strategy, not full scene virtualization.
A later GPU or canvas graph should use viewport-indexed graph tiles
and a keyboard-accessible node list for larger datasets.

## Testing and benchmarks

Run without frontend install or bundle:

    node --test tests/layout_engine.test.cjs
    node scripts/layout_benchmark.cjs --check
    python -m unittest discover -s tests -v

Browser smoke uses a temporary local Compose repository, starts the
existing loopback-only control room, and checks actual Chrome geometry
at six desktop/tablet/mobile sizes. It also injects a synthetic
400-node state to check the 90-node render cap, incident priority and
search; no external source or production credentials are involved.

    python -m pip install 'selenium==4.35.0' 'PyYAML==6.0.3'
    python tests/layout_browser_smoke.py

Dedicated .github/workflows/layout-engine.yml performs Node and Chrome
checks; the existing Python validator remains unchanged. The benchmark
is a timing sample of pure Node CPU execution, not the browser's paint
latency or an apples-to-apples speed comparison with browser CSS.

### Benchmark baseline math

At 401 nodes in a single original SVG column:

    original max(550, 401*76 + 135) = 30,611px
    new      max(350,  90*64 +  88) = 5,848px

The worst-case SVG-height budget is ~80.9% smaller. This does not imply
80.9% lower GPU memory, actual screen size or time to paint.

## Known limitations / next improvements

1. Replace prototype 90-node cap with a server-side indexed graph
   region query, tiles and accessible node explorer.
2. Incorporate measured text/content requirements and explicit user
   drag/resize locks without destabilizing panel priorities.
3. Add cross-browser screenshot/regression and performance budgets
   (LCP/INP/render FPS) with real repo.ing graph snapshots.

The design keeps discovered facts, inferred relationships, CI outcomes
and production health distinct. Source-only UNKNOWN stays UNKNOWN.
No automatic repair and no production deployment is included.

## Branch chain

This draft depends on PR #21, which depends on the unmerged discovery
stack. Review/retarget before merging; the local Control Room is
still a prototype, not a hosted authenticated SaaS.
