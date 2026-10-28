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

## Query the export with sparq (optional)

[sparq](https://github.com/sparq-org/sparq) is a Rust RDF store and SPARQL 1.1/1.2 engine
with Python bindings. It is an optional reader of the export, not an OntologyEX
dependency. sparq describes itself as an experimental, pre-1.0 engine; the version below
is the one these queries were tested with.

    python -m pip install 'sparq-rdf==0.1.6'
    python ontology-extraction/scripts/rdf_export.py /path/to/skill > graph.nt
    python examples/sparql/run.py graph.nt examples/sparql/review-blockers.rq

Example queries in `examples/sparql/`:

| File | Question |
|---|---|
| `review-blockers.rq` | Which unknowns, conflicts, inferred rules, and non-executable rules block each task? |
| `rules-without-requirement.rq` | Which rules cite no requirement span (behavior without stated intent)? |
| `rule-evidence-kinds.rq` | How many evidence kinds and files does each rule cite? |
| `depends-on-file.rq` | What cites a given source file, and so needs review when it changes? |
| `task-context.rq` | Which classes, relations, and code bindings does each task touch? |

`tests/test_rdf_export.py` runs every query against the compiled payments example when
`sparq` is installed, and skips that test otherwise. Leave sparq's RDFS/OWL reasoning off:
the export deliberately asserts no `rdfs:domain`, `rdfs:range`, or class hierarchy, so
reasoning adds nothing that the authors stated.

## Semantics

The exporter uses local, versioned URN identifiers. Every projected node is
asserted with a type in the ontologyex namespace and the local authored ID is
published as rdfs:label. The existing declared-reference edges use
ontologyex-specific predicates. A SUPPORTED_BY edge uses the W3C
prov:wasDerivedFrom predicate to point to an evidence **record**, which carries
the source file path, SHA-256, source span and declared evidence kind.

The contract's rules, unknowns and conflicts are exported as `kind:rule`,
`kind:unknown` and `kind:conflict` nodes. Each has an `attr:statement`, a
`CONCERNS_TASK` edge to its task and `prov:wasDerivedFrom` edges to its evidence.
A rule also has `attr:status` (`observed` or `inferred`) and `attr:executable`
(`xsd:boolean`: whether it has a typed check). Statements are authored
interpretations, not source quotations or entailed facts. The check expression
itself is not exported.

**Important:** A domain relation node's DOMAIN and RANGE edges are OntologyEX
metadata, **not** global rdfs:domain/rdfs:range axioms. No owl:sameAs, class
hierarchy, permission, policy or formal inference is created. This avoids
silently upgrading a source-bound declaration into a universal semantic rule.

No original source quotation, full file content, agent transcript, secret, or
tool argument is output. Source paths, graph labels and rule statements can
nonetheless reveal private architecture or policy; keep the file local and
access-controlled.

If graph_view.py finds unresolved references, RDF export **fails closed**
instead of silently emitting an incomplete subset. Inspect the complete
graph JSON, resolve the missing bindings and rebuild before exporting.

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
node rejection, size limits, failure to promote unreviewed outputs, contract
export with fail-closed task and evidence references, and (with sparq) the
example queries.
