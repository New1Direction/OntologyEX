# Agent Ontology Skill

This is a portable skill for AI agents. It is not tied to any one agent runtime.

It can be used by Claude Code, Codex, Cursor, custom agent runners, or any capable AI agent that can read instructions, inspect sources, create files, and run the optional validator script.

Use it when a user asks an AI agent to extract, design, retrofit, or validate a domain model, ontology, semantic layer, taxonomy with relations, knowledge graph schema, RAG metadata vocabulary, MCP/tool schema, or business concept map from a company, market, product, API, codebase, or documentation set.

## What This Skill Produces

Produce a four-layer ontology:

1. L0 Upper: universal anchors such as Agent, Event, Object, Process, Role, Place, Time, Quantity, and InformationArtifact.
2. L1 Domain: the business/domain nouns that would survive a competitor swap, such as Charge, Refund, Article, Comment, Market, Position.
3. L2 Task: the verbs, workflows, inputs, outputs, preconditions, and effects, such as ProcessRefund or PublishArticle.
4. L3 Application: concrete system artifacts such as DB tables, API types, tool schemas, events, UI objects, and field mappings.

The output is useful for:

- AI agent tool contracts
- MCP tools
- RAG metadata and query routing
- knowledge graph schemas
- DB/API semantic mapping
- codebase audits
- market or competitor research
- documentation alignment

## Required Deliverables

Create this file set unless the user asks for a narrower output:

```text
<target>-ontology/
  00-scope.md
  10-upper.yaml
  20-domain.yaml
  30-task.yaml
  40-application.yaml
  50-mappings.yaml
  README.md
```

If the consumer is known, add the matching binding:

- Agent tools or MCP: derive tool schemas from L2 tasks.
- RAG: emit metadata keys from L1 classes and L2 tasks.
- Knowledge graph: emit RDF/Turtle, JSON-LD, or property-graph mapping.
- DB/API: emit TypeScript, Pydantic, OpenAPI notes, or JSON-LD context.
- Documentation: emit Mermaid diagrams and a prose summary.

## Operating Workflow

Work middle-out and keep the production ontology rule in mind: **model how the real-world business operates, not how source tables or department systems happen to be shaped.**

1. Scope the target, consumer, and boundary.
2. Write 5 to 15 competency questions that define what the ontology must answer.
3. Mine evidence from source material.
4. Select L0 anchors from existing upper ontologies. Do not invent L0.
5. Build L1 domain classes, relations, and reusable interfaces/traits.
6. Build L2 task/workflow definitions.
7. Bind L3 app artifacts to L1 classes and L2 tasks.
8. Validate references, mappings, duplicates, orphan classes, evidence, and anti-pattern warnings.
9. Emit the consumer-specific binding.

## Source Mining

For public companies, markets, or products, mine:

- API docs and OpenAPI/GraphQL schemas
- help centers, glossaries, docs, and tutorials
- sitemap/navigation and product pages
- pricing pages and integration marketplaces
- job postings
- annual reports, S-1s, or 10-Ks when available
- industry standards and published ontologies

For codebases, mine:

- DB schemas and migrations
- type definitions and models
- API routes and controllers
- event names and queue topics
- tests and specs
- middleware, guards, roles, and permissions
- existing agent/MCP tool schemas
- README, ADRs, and design docs

Every extracted class, relation, task, or binding should carry a `source` field. Evidence or it did not happen.

## Modeling Rules

- L0 is selected from standards, never invented.
- L1 contains nouns, not verbs.
- L2 contains verbs/tasks, not static nouns.
- L3 contains only system-specific artifacts.
- Every L1 class must anchor to L0.
- Every L2 task must reference L1 inputs and outputs.
- Every L3 concept must bind to an L1 class.
- Synonyms belong under one canonical class id.
- Relations require domain, range, cardinality, definition, and source.
- A class list with no relations is only a taxonomy, not an ontology.
- Prefer composition with `interfaces`/`implements` for shared traits such as Addressable, Auditable, Monetary, Geospatial, or TimeBound.
- Avoid deep abstract hierarchies and vague names (`Asset`, `Entity`, `Object`, `Record`, `Item`) unless the source proves they are precise business terms.
- The mapping table is mandatory.

## Default Upper Ontology Choices

- gist: default for business and enterprise targets.
- schema.org: web-facing output, SEO/GEO, public web payloads, crawler or LLM-readable contexts.
- BFO 2020 plus CCO: regulated, scientific, defense, or formal interoperability settings.
- PROV-O overlay: audit, lineage, attribution, provenance, or oracle/resolution history.
- QUDT overlay: any numeric value with units.
- OWL-Time overlay: temporal intervals and instants.
- GeoSPARQL overlay: spatial entities.

## Validation Checklist

Before final output:

- Can the ontology answer every competency question?
- Do all L1 classes have definitions, sources, and L0 anchors?
- Do all relations resolve to known classes?
- Do all tasks reference known L1 inputs and outputs?
- Do all app concepts bind to known L1 classes?
- Are duplicate ids removed?
- Are synonyms collapsed?
- Are unmapped L3 fields explained as app-only, missing L1, or dead schema?
- Did you inspect warnings for God Object, Kitchen Sink, department/system silos, action sprawl, schema overload, and vague misnomers?
- Is the final binding appropriate for the consumer?

If the bundled script is available, run:

```bash
python3 scripts/scaffold.py validate <ontology-dir>
python3 scripts/scaffold.py mappings <ontology-dir>
```

## Prompt Pattern For Any AI Agent

```text
Use the Agent Ontology Skill.
Target: <company, market, product, API, codebase, or docs>
Consumer: <agent tools | MCP | RAG | knowledge graph | DB/API | docs>
Boundary: <what is in and out of scope>
Deliver: 00-scope.md, 10-upper.yaml, 20-domain.yaml, 30-task.yaml,
40-application.yaml, 50-mappings.yaml, validation notes, and the consumer binding.
```

## Anti-Pattern Guardrails

Read `ontology-extraction/references/design-principles.md` when doing full ontology work. In short:

- **God Object / Kitchen Sink**: do not overload one class or blindly copy every source column.
- **Department/System Silos**: one real-world entity should have one L1 class; preserve source-specific forms in L3.
- **Action Sprawl**: group property-level updates into business-level tasks where possible.
- **Schema Overload**: extend stable production classes additively with links, interfaces, optional fields, or new L3 bindings.
- **Misnomer / Deep Hierarchy**: use precise semantic labels and shared interfaces rather than brittle inheritance towers.

## Output Quality Bar

The final result should feel like infrastructure, not notes. It should give an AI agent or software system a grounded map of the domain before it acts.
