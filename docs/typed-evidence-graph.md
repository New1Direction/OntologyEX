# Typed evidence graph (experimental, read-only)

OntologyEX's four-layer compiler remains authoritative for what a domain-skill
candidate declares. This module converts a **verified compiled skill** into a
deterministically ordered, typed graph so a viewer can explore declared objects,
tasks, application bindings, and exact source locations without reading a prose
report or inventing cross-system dependencies.

This work borrows the **explicit entity/relationship/version model** idea from
[Helix Foundry](https://github.com/HelixDB/helix-foundry). It introduces no
HelixDB service, no connector, no model agent, no persistence layer and no
production write access.

## Commands

From the OntologyEX checkout, using existing compiled skill directories:

    python ontology-extraction/scripts/graph_view.py export /path/to/skill
    python ontology-extraction/scripts/graph_view.py diff /path/to/old-skill /path/to/new-skill
    python ontology-extraction/scripts/graph_view.py compose /path/to/first-skill /path/to/second-skill

All three commands print one JSON object to stdout, exit nonzero on failure,
and perform **no writes**. Pipe the output into an access-controlled local
viewer when needed. Do not publish a graph containing private source paths.

### Stable identity

Each node ID is the tuple (root goal ID, node kind, local authored ID), rendered
as goal:kind:local_id. Nodes keep this identity across candidate versions.
Root-goal namespaces prevent two projects' coincidentally identical names
from being silently identified as one object.

The graph contains the following node kinds:

- upper_anchor: selected upper-ontology anchors
- domain_class: L1 business/domain types
- interface: cross-cutting capabilities/compositions
- relation: a **declaration** of a relation, not an observed instance edge
- task: L2 actions
- application_binding: L3 concrete app-model bindings
- evidence: source reference ID plus selected file path, full-file SHA-256, and
  exact source line range; **no raw quotes or file contents**

Declared-reference edge kinds are SUPPORTED_BY, ANCHORED_TO, IMPLEMENTS,
APPLIES_TO, DOMAIN, RANGE, CONSUMES, PRODUCES, ACTOR_ROLE,
DECOMPOSES_TO, BINDS_TO, USED_BY_TASK.

A missing referenced target creates an entry in the **unresolved** collection,
not a fake target or evidence edge. This matters for actor-role identifiers
that the existing scaffold treats as warnings rather than fatal errors.

### Versions and multiple projects

The export includes the immutable bundle ID. The diff subcommand requires two
bundles for the **same root goal and skill name**. It reports added, removed
and modified graph records plus unresolved references before and after.

A graph diff is not a substitute for domain_skill.py compare: source changes,
natural-language meaning, policy changes and unseen integration behavior may
affect tasks without changing projected graph edges. Consult both outputs.

Compose accepts 1-16 distinct root goals and refuses multiple versions of the
same goal rather than silently selecting one. It does **not infer dependencies
between distinct scopes**. A separate reviewed mapping and an actual runtime
adapter are required for production component dependencies.

### Review and execution boundaries

The projection reports:

- review_status: UNREVIEWED_CANDIDATE
- integrity: BUNDLE_HASHES_VERIFIED_NOT_AUTHENTICATED
- runtime_observations: NOT_INCLUDED
- semantic_entailment: NOT_EVALUATED
- execution_authorized: false

Hashes prove a bundle's internal consistency, **not** authorship or semantic
correctness. Nothing in this graph grants approval, updates the active schema,
or executes tools. There is no fictitious active-version pointer: a later
release flow must authenticate external reviewers and enforce authorization
outside OntologyEX.

This module only reads verified frozen outputs. It neither changes
60-contract.json nor merges experimental trace observations into policy.

## Testing

    python -m unittest tests.test_graph_view -v

Tests cover typed edges, evidence lookups, deterministic output, stable IDs
across versions, cross-project isolation, missing references, duplicate-node
rejection, graph diffs, and actual compiled-bundle verification and tamper
rejection. This is not a held-out evaluation of agent accuracy or proof of
production operational visibility.

## Next implementation boundary

A later Control Room adapter may bind trusted **observed component instances**
to these stable ontology types. Its separate data model must capture
environment, observation time, ingestion time, freshness/expiry, evidence source,
health status, and temporal dependency intervals. Static type relations must
never be presented as observed calls, healthy services or production incidents.
