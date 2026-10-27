# RDF interchange bridge (experimental)

OntologyEX can export the typed evidence graph from a **verified compiled skill**
as deterministic RDF 1.1 N-Triples. This enables a separate semantic reasoner
or verifier, such as [Open Ontologies](https://github.com/fabio-rovai/open-ontologies),
to inspect exactly the authored facts without pulling another framework into
OntologyEX itself.

## Usage

First compile an OntologyEX domain skill through the existing reviewed
source-to-skill workflow. Then:

    python ontology-extraction/scripts/rdf_export.py /path/to/skill > graph.nt

To join **different** root-goal scopes without inferring cross-project links:

    python ontology-extraction/scripts/rdf_export.py /path/to/skill-one /path/to/skill-two > combined.nt

When an independently installed compatible Open Ontologies binary is available,
you can separately try:

    open-ontologies validate graph.nt

The N-Triples interoperability bridge is designed for this exchange; it has
**not** been validated against every upstream release or every reasoning
profile. Installing and running Open Ontologies is separate and optional.
This repository does not download or invoke its executable.

## Semantics

The exporter uses local, versioned URN identifiers. Every projected node is
asserted with a type in the ontologyex namespace and the local authored ID is
published as rdfs:label. The existing declared-reference edges use
ontologyex-specific predicates. A SUPPORTED_BY edge uses the W3C
prov:wasDerivedFrom predicate to point to an evidence **record**, which carries
the source file path, SHA-256, source span and declared evidence kind.

**Important:** A domain relation node's DOMAIN and RANGE edges are OntologyEX
metadata, **not** global rdfs:domain/rdfs:range axioms. No owl:sameAs, class
hierarchy, permission, policy or formal inference is created. This avoids
silently upgrading a source-bound declaration into a universal semantic rule.

No original source quotation, full file content, agent transcript, secret, or
tool argument is output. Source paths and graph labels can nonetheless reveal
private architecture; keep the file local and access-controlled.

Unresolved references in graph_view.py are **not** RDF assertions. Inspect
the full graph JSON for them before interpreting any reasoner output.

## What a successful check means

Successful RDF parsing means **syntax accepted**, not semantic correctness.
Open Ontologies can offer additional reasoning/checking workflows, but their
results must have their actual provenance, proof/checker status and rule
assumptions recorded independently. A successful parser exit must never be
reported as ontology consistency, verified proof or authorization.

The underlying bundle remains UNREVIEWED_CANDIDATE. There are no production
writes, active-version changes, approvals, telemetry observations or automatic
agent actions.

## Tests

    python -m unittest tests.test_rdf_export -v

The tests cover deterministic sorting, URN identity, evidence metadata,
escaping, unknown vocabulary rejection, dangling-edge rejection, duplicate
node rejection, size limits, and failure to promote unreviewed outputs.
