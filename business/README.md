# Business X-Ray — beyond infrastructure maps

OntologyEX Business X-Ray is an experimental, evidence-aware **business
anatomy** viewer and compilation format. Instead of asking only which services
exist, it captures the system from the user's and operator's point of view:

- **Purpose and actors:** who uses the product, and what outcome they need
- **Value proposition:** why users should care, and what they receive
- **Journeys:** ordered actions connecting users and business outcomes
- **Money flows:** who pays whom, and what is gated or unknown
- **Systems:** code and infrastructure involved in delivering those outcomes
- **Breakpoints:** hypothesized failure modes, relevant process steps and the
  missing metrics that would be needed to observe them
- **Unknowns and improvements:** unresolved evidence gaps and recommended work

The outputs are *NOT* automated correctness proofs, causal failure graphs,
verified revenue, complete business models, or runtime monitoring.

## Try it

Open [index.html](index.html) to browse three **public-only** worked projects
(repo.ing, Ohiyo, OntologyEX). It is a single self-contained HTML file,
no external scripts, no network requests, no accounts. Use the business tabs,
switch journeys, click any stage or risk to see its source links, or simulate
failure of a step to highlight *subsequent stages in that modeled journey*.
A simulation does **not** verify that those services fail in production.

## Compile a complete local view

Run the compiler from the repository root with Python 3.11+:

~~~
python business/build_blueprints.py \
  business/examples/repoing.json \
  business/examples/ohiyo.json \
  business/examples/ontologyex.json \
  --out /tmp/ontologyex-business-xray.html \
  --json-out /tmp/ontologyex-business-xray.json
~~~

The compiler accepts any one or more authored JSON blueprints of the same
schema. The HTML template is co-located with the Python compiler, so
no web server or NPM installation is needed. Existing outputs are not
overwritten. A separate Atlas snapshot can be passed through
the optional --inventory flag for *inventory coverage counts*,
not to infer or certify business claims.

Public demos are editor-authored reviews of selected source documentation.
There is currently **no automatic semantic extraction of a whole company
from GitHub**. Building a complete company ontology requires connectors
and a bounded source-to-claim proposal workflow with reviewer acceptance.

## Author a blueprint

The three examples under [examples/](examples/) are JSON schema v1 records.
Each project must include:

- id, name, purpose, stage
- evidence: exact GitHub repository, 40-character commit, relative path, and
  1-based source line range
- audiences, value, money_flows, journeys, systems, risks, unknowns,
  improvements — each a list of typed objects with stable IDs
- journeys contain a sequence of steps, each with an ID, name, description,
  evidence pointers, state (described, deployed_claim, gated, planned, unknown)
  and optional risk_ids
- money flows can be empty where monetization isn't documented

Every item records a **basis**: observed_in_source, derived_from_source,
operator_declared, proposed, or unknown. Proposals never automatically become
facts. Critical risk cards use proposed status because a hypothetical
failure mode is not an observed incident.

The compiler rejects duplicate IDs and evidence keys, missing source IDs,
unsupported evidence basis, unsupported state labels, malformed commit
identities, path traversal, missing linked risk IDs, oversized JSON, and
untrusted non-finite constants. It does not download source files or prove
that the pinned source text *semantically* supports a claim. That review
belongs to a separate verification step.

Source documents are untrusted inputs; do not embed credentials, secrets,
private personally identifying data or full code dumps in a blueprint.
Keep proprietary project blueprints local. The public repository includes
only public-project examples.

## Why this is useful before production monitoring

For each project, ask:

- Which end-to-end user outcomes are actually modeled?
- Which business steps have cited evidence?
- Which revenue streams are described versus gated?
- Which downstream journey stages become *potentially* affected by a failure?
- Where are we missing live measurements, source coverage, or financial truth?
- Which improvement is supported by which concrete workflow risk?

The first deliverable is not a claim that every business has been mapped.
It is an **auditable draft of its operating model** and a precise checklist
for completing the model.

## Follow-up implementation boundaries

1. Add automatic *candidate* extraction from read-only GitHub/docs/DB schemas
   with exact evidence, contradicting records and explicit unknowns.
2. Add a source-review UI; candidates must never silently become policy.
3. Add typed implementations of users, payments, data and service states,
   so journeys link to actual code/db bindings.
4. Add timestamps, live probes and confirmed incidents with freshness; prevent
   stale or absent telemetry from being labeled healthy.
5. Add evidence-based optimization experiments: proposed change → independent
   verification → measured outcome → reviewed ontology revision.
6. Add multi-tenant scoped access, consent, data retention and deletion,
   authenticated review, and isolated production-action approval.

Avoid shipping a giant generic graph with no useful questions or outcomes.
**The product is understanding, diagnosis, and improvement—not the graph itself.**

## Checks

~~~
python -m unittest discover -s tests -v
~~~

Existing OntologyEX compilation, source-evidence and execution-authorization
guards remain untouched. This experimental business layer adds no execution
authority and does not replace the four-layer compiler.
