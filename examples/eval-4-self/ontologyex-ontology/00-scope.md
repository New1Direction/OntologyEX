# Scope — ontologyex

## Target
**Retrofit mode.** The OntologyEX repository itself — the "Agent Ontology Kit": a
portable, runtime-agnostic skill that extracts four-layer ontologies from a target and
binds them to a consumer. We model the *field the kit operates in* (ontology / knowledge
engineering) and the concrete artifacts this repo ships.

Sources are the repo's own files: `AGENT_SKILL.md`, `ontology-extraction/SKILL.md`,
`ontology-extraction/scripts/scaffold.py`, `ontology-extraction/references/*`, and the
worked evals under `examples/`.

## Consumer
**Agent tools (MCP) + review docs.** The kit is operated by an AI agent, so the
highest-leverage binding is an MCP tool surface derived from the L2 workflow tasks (run
the kit as callable tools). A Mermaid diagram is emitted for human review. Output format
follows `references/output-formats.md`.

## Boundary
**In:** the four-layer method, its middle-out workflow steps, the scaffold/validate/mappings
tooling, the canonical file artifacts (`00`–`50`), the reference catalogs, and the eval
bundles treated as *instances* of an Ontology.
**Out:** the marketing/presentation layer (`index.html`, `docs/launch.html`,
`docs/guide.html`); the internal *domain content* of the worked examples (Stripe payments,
RealWorld, prediction markets) — those are modeled by their own ontologies, not this one.

## Competency questions

### L1 (domain)
- [x] What are the four layers an Ontology is composed of? → `has_layer` over `OntologyLayer`
- [x] What does an OntologyClass anchor to, and where does its evidence come from? → `anchors_to` UpperOntology, `cites` EvidenceSource
- [x] What distinguishes a Relation from a bare class list (taxonomy vs ontology)? → Relation with domain/range/cardinality
- [x] What is the difference between an OntologyTask (modeled activity) and an OntologyClass (noun)?
- [x] What is the subject an Ontology is about, and does it differ in research vs retrofit mode? → `models_target` ExtractionTarget

### L2 (task)
- [x] What is the precondition for emitting a ConsumerBinding? → ValidateOntology must pass first
- [x] Which workflow step selects (never invents) the UpperOntology? → AnchorUpper
- [x] What does the umbrella ExtractOntology task decompose into, and which steps are leaf tasks?
- [x] Which task produces Relations as well as OntologyClasses? → BuildDomainLayer

### L3 (application)
- [x] Which repo artifact realizes the structural validator, and which task uses it? → `scaffold.validate` ← ValidateOntology
- [x] Which file template binds the CompetencyQuestion concept? → `file.00-scope`
- [x] Which shipped artifact is an emitted ConsumerBinding instance? → `example.mcp-tools-json`
- [x] Which artifacts bind to no domain concept (dead schema / out-of-scope)? → see validator orphan report
