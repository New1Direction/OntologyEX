# Source Mining: Where Each Layer Hides

Evidence-driven extraction. Every term in the ontology carries a `source` field pointing at the artifact it came from.

## Research mode (company/business you don't control)

| Artifact | How to get it | Primarily yields |
|---|---|---|
| Sitemap / site navigation / IA | fetch `/sitemap.xml`, crawl nav | **L1 taxonomy skeleton** — the company has already organized its domain nouns for you |
| Product & pricing pages | fetch | Offerings as L1 individuals, tiers, feature names (candidate L3), packaging logic |
| API reference / OpenAPI / GraphQL schema | fetch developer docs; look for `openapi.json`, `schema.graphql` | **L3 nearly verbatim** (types, fields, enums); HTTP verbs + resource nouns ⇒ **L2 tasks** |
| Help center / glossary / docs | fetch + search `site glossary`, `what is <term> <company>` | **L1 definitions and synonym sets** — canonical wording |
| Job postings | search `<company> careers <function>` | **L2**: "you will reconcile settlements / triage incidents" = task list; titles = actor roles |
| 10-K / annual report / S-1 (public cos) | SEC EDGAR, investor relations | Business model, segments, value chain ⇒ coarse **L2**; risk factors ⇒ constraint candidates |
| Integration marketplace / partner directory | fetch | Boundary objects and external **actors** (what crosses the system edge) |
| Onboarding flows / tutorials / changelogs | fetch | **L2 sequences**, preconditions, state transitions |
| Industry standards bodies & published ontologies | see `reuse-catalog.md` | **L1 reuse targets** — check before authoring any class |
| Webhook/event documentation | fetch | **L2 occurrents** — `charge.succeeded`, `invoice.paid` are tasks/events with effects spelled out |

Priority order when time-boxed: API docs → glossary → sitemap → pricing → job postings. API docs alone often yield 70% of L2+L3.

## Retrofit mode (your own project/codebase)

| Artifact | Yields |
|---|---|
| DB schema / migrations | **L3 entities** with attributes and cardinality (FKs = relations) |
| Type definitions (TS interfaces, Rust structs, Pydantic models) | **L3 concepts** + field-level attributes |
| API routes (verb + noun) | **L2 tasks** + the L3 concepts they touch |
| Route middleware / guards (`auth.required`, role checks, rate limits) | **L2 preconditions verbatim** — the framework already encodes them; copy into the task's `preconditions` |
| Event names / queue topics / pub-sub channels | **L2 occurrents** and state transitions — event past-tense names are task effects |
| Enums / config constants | Value partitions, states, controlled vocabularies |
| Test names / specs | **L2 invariants**, preconditions, edge cases ("refund fails when charge unsettled") |
| Existing MCP/agent tool schemas | **L2 already formalized** — inputs/outputs/descriptions map directly |
| README / ADRs / design docs | Intent, boundary, the L1 the authors had in their heads |
| Logs / analytics event taxonomy | What actually happens vs. what the schema permits |

Retrofit insight: L3 is already written; the work is **mapping upward**. Tables/types that bind to no domain concept are either missing L1 classes or dead schema — both findings are valuable.

## Harvesting procedure

1. **Collect**: pull candidate terms from the artifacts above. Frequency matters — a noun appearing in nav, glossary, AND API schema is core; a one-off is peripheral.
2. **Collapse synonyms**: pick canonical id (prefer the API/schema spelling — it's the machine-facing one), park the rest in `synonyms`. Watch for near-synonyms hiding real distinctions (User vs Account vs Customer often differ; check definitions before merging).
3. **Sort nouns/verbs**: nouns → L1 candidates, verb phrases → L2 candidates, anything that only exists inside the specific system → L3.
4. **Define**: one sentence per term, written so a competitor's employee would agree (L1 test). If the definition needs the company name, it's L3.
5. **Relate**: for each L1 class pair that co-occurs in source sentences/schemas, ask "what relation?" FKs, "has", "belongs to", "settles to". Record domain/range/cardinality.
6. **Evidence**: stamp every entry with `source:` (URL, file path, or doc name).

## Finding *published* ontologies for a domain (when the user asks "does one exist?")

Search in this order:
1. **LOV** (lov.linkeddata.es) — general-purpose vocabulary search; start here for most domains
2. **OKG** (Open Knowledge Graphs, launched ~2026) — newer ontology/vocabulary search engine indexing ontologies, taxonomies, and semantic-web tools; has an MCP server, so check connected tools before browsing
3. **BioPortal** (bioportal.bioontology.org) — the largest ontology index, but biomedical-focused; lead with it only for life-science/health targets
4. **schema.org full hierarchy** — covers more business ground than people expect (Action subtree for L2!)
5. **Industry body sites** — EDM Council (FIBO), HL7 (FHIR), GS1, OGC, W3C TR index
6. Web search: `<industry> ontology OWL`, `<industry> vocabulary RDF`, `<industry> data standard`
7. **GitHub**: `topic:ontology <domain>` — many maintained ontologies live only in repos
8. **De-facto machine standards**: when no formal ontology exists, the field's dominant protocol/API spec often is the L3 vocabulary (e.g. token standards, wire formats). Search `<domain> data model standard` / `<domain> protocol specification` and align L3 to it verbatim.

If a published ontology covers ≥60% of the L1 need, align to it and extend; below that, author L1 fresh but still align the L0 anchors.

## When the ontology file itself won't fetch

GitHub API rate limits, 404s on moved files, and content-negotiation redirects are common. Fallback ladder: (1) the publisher's versioned import URL (e.g. `ontologies.semanticarts.com/o/gistCore<version>`), (2) LOV's cached copy, (3) pin version + class names from the publisher's release page/docs via web search. If anchors can't be byte-verified, record them anyway and mark the `version` field with a re-verify note — an unverified anchor beats an invented one, and the validator still enforces internal consistency.
