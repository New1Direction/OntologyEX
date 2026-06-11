---
name: ontology-extraction
description: Extract or construct a complete four-layer ontology (top-level/upper, domain, task, application) from any company, business, product, market, or codebase — or retrofit one onto an existing project. Use this skill whenever the user mentions ontology/ontologies, domain modeling, knowledge graph schema, semantic layer, taxonomy design, concept extraction, "model this business/domain", "what are the core concepts of X", or wants formal structure for agents, tools, databases, RAG, or knowledge graphs — even if they name only one layer or never say the word "ontology". Also trigger when a user asks where to FIND existing ontologies for an industry, or how to map their app's entities to standard vocabularies.
---

# Ontology Extraction (Four-Layer)

Produce a four-layer ontology for a target — a company you don't control (research mode) or a project/codebase you do (retrofit mode) — and bind it to whatever will consume it (knowledge graph, agent tools, DB/API schema, RAG, docs).

## The four layers, operationally

| Layer | Contains | Litmus test | Where it comes from |
|---|---|---|---|
| **L0 Upper** | Domain-independent categories: Agent, Event, Process, Object, Role, Place, Time, Quantity, Information Artifact | True for *any* business | **Selected, never invented** — pick 10–25 anchors from an established upper ontology (see `references/reuse-catalog.md`) |
| **L1 Domain** | The **nouns** of the field the business operates in, independent of any one company. Payments: Merchant, Charge, Settlement, Chargeback | True for every competitor in the field | Mined from public/industry sources + reused industry ontologies |
| **L2 Task** | The **verbs**: activities and procedures, each with actor roles, inputs, outputs, preconditions, effects | Describes *doing*, references L1 nouns as participants | API verbs, process docs, job postings, user flows, event streams |
| **L3 Application** | Only the concepts a *specific system* touches, bound to concrete artifacts (DB tables, API types, MCP tool schemas, UI objects), each mapped upward | Deleting the app deletes the term | The system's own schemas, types, routes, events |

The discipline that makes four layers worth having: **every L3 concept maps to an L1 class, every L1 class anchors to an L0 category, every L2 task consumes/produces L1 classes.** The mapping table is the deliverable that makes everything else usable.

## Workflow

Work **middle-out**: anchor L0 early (cheap, it's a selection), extract L1 first (most evidence), L2 second, project L3 last. Never start with top-down philosophy or bottom-up app-detail drowning.

### Step 0 — Scope
Establish three things before extracting anything:
1. **Target**: company/business (research mode) or project/codebase (retrofit mode)?
2. **Consumer**: what eats this ontology? Knowledge graph, agent tool surface, DB/API schema, RAG metadata, or documentation. The consumer determines depth and output format.
3. **Boundary**: which slice of the business? "All of Amazon" is a non-goal; "Amazon's fulfillment domain for a returns-automation agent" is a goal.

### Step 1 — Competency questions (the validation contract)
Write 5–15 questions per layer that the finished ontology must be able to answer using only its own terms and relations. Examples:
- L1: "What entities can own a Charge?" — L2: "What are the preconditions for processing a refund?" — L3: "Which DB table realizes Refund, and which tool mutates it?"
Record them in `00-scope.md`. They define done.

### Step 2 — Mine sources
Read `references/source-mining.md` for the full artifact→layer evidence map and harvesting procedure. Summary:
- **Research mode**: sitemap/nav (L1 skeleton), API/OpenAPI docs (L3 near-verbatim, verbs→L2), glossary/help center (L1 definitions), pricing page (offerings as individuals), job postings (L2 roles+tasks), 10-K/annual report (value chain → L2), integration marketplace (boundary actors).
- **Retrofit mode**: DB schema/migrations and type definitions (L3), API routes verb+noun (L2+L3), event names/queue topics (L2 occurrents and state transitions), enums (value partitions), test names (invariants), existing MCP/agent tool schemas (L2 formalized).
Use search/fetch tools aggressively in research mode; read the actual files in retrofit mode. Every extracted term gets a `source` field — evidence or it didn't happen.

### Step 3 — Anchor L0
Do not invent upper categories. Read `references/reuse-catalog.md` and pick:
- **gist** — default for business/enterprise targets (small, business-friendly)
- **BFO 2020 + CCO** — regulated, scientific, defense, or interop-with-standards contexts
- **schema.org** — web-facing targets, anything where LLM/search crawlers also consume the output
- **PROV-O overlay** — when audit/provenance is first-class
Record the chosen ontology, version, and the 10–25 anchor classes with IRIs in `10-upper.yaml`.

### Step 4 — Build L1 (domain)
1. Harvest candidate terms from Step 2 sources; collapse synonyms (one canonical id + `synonyms` list — never both "Doctor" and "Physician" as classes).
2. Check `references/reuse-catalog.md` for an existing industry ontology (FIBO, FHIR, GS1, SOSA, ESCO…). Reuse classes where they fit; extend where they don't. Reuse beats invention — alignment to a published ontology is free interop.
3. For each class: `id`, one-sentence `definition`, `upper` anchor, `synonyms`, `source`.
4. Define **relations** with domain/range and cardinality. A class list with no relations is a taxonomy, not an ontology.

### Step 5 — Build L2 (task)
For each activity: `id`, `verb_phrase`, `actor_roles`, `inputs`, `outputs` (all referencing L1 classes), `preconditions`, `effects`, optional `decomposes_to` for sub-tasks, `source`. Mine API verbs (`POST /v1/refunds` ⇒ ProcessRefund), job-posting bullet points ("you will reconcile settlements"), and process documentation. Keep tasks at the granularity the consumer needs — an agent tool surface wants tool-sized tasks; a strategy doc wants value-chain-sized ones.

### Step 6 — Project L3 (application)
Intersect L1 × L2, filtered by the consumer. For each concept the system actually touches: `id`, `kind` (db_table | api_type | tool_schema | event | ui_object), `binds` (L1 class), `used_by_tasks` (L2 ids), field-level `maps_to` where useful. In retrofit mode this layer is mostly *already written* in the codebase — the work is mapping it upward and exposing the gaps (tables that bind to no domain concept are either missing L1 classes or dead schema).

### Step 7 — Validate
1. Run the structural validator: `python scripts/scaffold.py validate <dir>` — checks unanchored classes, dangling references, duplicate ids, orphans.
2. Answer every competency question using only ontology terms. Unanswerable CQ ⇒ missing class, relation, or task.
3. Round-trip test: take 3 real records or sentences from the sources and express them in the ontology. Anything inexpressible reveals a gap.

## Deliverable structure

Scaffold it: `python scripts/scaffold.py init --name <target> --out <dir>` produces:

```
<target>-ontology/
  00-scope.md           # target, consumer, boundary, competency questions
  10-upper.yaml         # chosen upper ontology + anchors
  20-domain.yaml        # classes + relations
  30-task.yaml          # tasks
  40-application.yaml   # bound concepts
  50-mappings.yaml      # app → domain → upper, with evidence (generated/maintained)
  README.md             # how the consumer eats this
```

Then emit consumer bindings per `references/output-formats.md`: Turtle/OWL, JSON-LD context, TypeScript/Pydantic types, Mermaid diagram, or **MCP tool schemas derived from L2 tasks** (task inputs/outputs → tool input_schema — the highest-leverage binding for agent projects).

## Anti-patterns

- **Taxonomy cosplay**: an is-a tree with no relations or constraints. Relations and cardinality are mandatory.
- **Inventing L0**: if a category feels universal, it already exists in BFO/gist/DOLCE. Select, don't author.
- **Company terms in L1**: "Stripe Connect Account" is L3 (binds to L1 `MerchantAccount`). L1 must survive a competitor swap.
- **Verbs in L1 / nouns in L2**: "Refund" the object is L1; "ProcessRefund" the activity is L2.
- **Boiling the ocean**: scope is whatever the competency questions need. Nothing else.
- **Skipping the mapping table**: unmapped layers are four disconnected documents, not an ontology.
- **Synonym sprawl**: one canonical id, everything else in `synonyms`.

## When the user only wants ONE layer

Still sketch the neighbors thin. A domain ontology with no task layer can't drive agents; an app ontology with no domain mapping can't interoperate. Deliver the requested layer in full, the adjacent layers as stubs, and say so.
