# Reuse Catalog: Select, Don't Author

Reuse beats invention at L0 always, at L1 usually, at L2 sometimes, at L3 never (L3 is by definition yours).

## L0 — Upper ontologies (pick ONE, take 10–25 anchors)

| Ontology | Character | Pick when | IRI base / home |
|---|---|---|---|
| **gist** (Semantic Arts) | Minimalist, business-native (~100 classes), pragmatic | **Default for business/enterprise targets** | `https://w3id.org/semanticarts/ns/ontology/gist/` |
| **BFO 2020** | ISO/IEC 21838-2 standard; rigorous continuant/occurrent split | Regulated, scientific, defense, formal interop | `http://purl.obolibrary.org/obo/` (basic-formal-ontology.org) |
| **CCO** (Common Core Ontologies) | Mid-level bridge under BFO (Agent, Artifact, Event, Information...) | With BFO — gives usable classes so you don't anchor to bare BFO | `https://github.com/CommonCoreOntology/CommonCoreOntologies` |
| **DOLCE / DUL** | Cognitive/linguistic grounding | Academic contexts, language-heavy domains | `http://www.ontologydesignpatterns.org/ont/dul/DUL.owl` |
| **SUMO** | Large, broad, mapped to WordNet | NLP pipelines needing lexical coverage | ontologyportal.org |
| **schema.org** | Informal upper+domain hybrid; what crawlers/LLMs read | Web-facing output; SEO/GEO-adjacent builds; pragmatic default when formality is low | `https://schema.org/` |

Overlay vocabularies (combine with any of the above):
- **PROV-O** (`http://www.w3.org/ns/prov#`) — Entity/Activity/Agent provenance. Use when audit, lineage, or attribution is first-class.
- **OWL-Time** (`http://www.w3.org/2006/time#`) — temporal intervals/instants.
- **GeoSPARQL** (OGC) — spatial.
- **QUDT** (`http://qudt.org/`) — quantities, units, dimensions. Use the moment any numeric attribute has a unit.
- **SKOS** (`http://www.w3.org/2004/02/skos/core#`) — when part of the structure is genuinely just a concept scheme/taxonomy (tags, categories), model it as SKOS instead of faking OWL classes.

Selection heuristics: license (gist = CC-BY, BFO = CC-BY, SNOMED = restricted), maintenance activity, tooling (Protégé-loadable?), weight (SUMO is heavy; gist is light). When in doubt for a business target: **gist**. When output must be crawlable/LLM-readable: **schema.org**.

## L1 — Domain ontologies by industry

| Industry | Reuse first | Notes |
|---|---|---|
| Finance / banking / securities | **FIBO** (EDM Council, `https://spec.edmcouncil.org/fibo/`) | Huge; take modules, not the whole thing |
| Payments / commerce | **schema.org** (Offer, Order, Invoice, PaymentMethod) + GS1 Web Vocabulary (`https://gs1.org/voc/`) | GoodRelations is largely absorbed into schema.org |
| Healthcare | **HL7 FHIR** (resources as L1/L3 hybrid), **SNOMED CT** (clinical terms — licensing varies by country), **LOINC** (labs) | FHIR resources double as L3 templates |
| Life sciences | **Gene Ontology**, **OBO Foundry** family | All BFO-anchored already |
| IoT / sensors / devices | **SOSA/SSN** (`http://www.w3.org/ns/sosa/`) | W3C/OGC standard |
| Industrial / manufacturing | **ECLASS**, **ISA-95** concepts | Product classification + production hierarchy |
| Organizations / HR | **W3C ORG** (`http://www.w3.org/ns/org#`), **FOAF**, **ESCO** (skills/occupations, EU), **O*NET** (US) | ESCO/O*NET are also strong **L2** sources — occupational task statements |
| Logistics / supply chain | GS1 EPCIS, schema.org ParcelDelivery | EPCIS events feed L2 |
| Media / publishing | schema.org CreativeWork subtree, Dublin Core Terms | |
| Cybersecurity | **UCO/CASE**, MITRE ATT&CK (technique taxonomy ≈ adversary L2), D3FEND | ATT&CK is literally a task ontology for attackers |
| Software / dev tools | schema.org SoftwareApplication/SoftwareSourceCode, SPDX (licenses/SBOM), OSLC | Sparser field — expect to author more |
| Crypto / DeFi | No dominant formal standard. Fragments: EthOn (Ethereum concepts, dated), token standards (ERC specs as de-facto L3). Prediction markets: **Gnosis Conditional Token Framework** (Condition/Collection/Position over ERC-1155) is the de-facto L3 vocabulary — align verbatim | Expect to author L1; anchor hard to L0 to compensate |
| Real estate | schema.org Place/Accommodation, RESO Data Dictionary | RESO is the industry's real standard |
| Education | schema.org Course, CEDS, IEEE LOM | |

## L2 — Task-layer reuse

- **schema.org Action hierarchy** — underused and genuinely good: `Action` with `agent`, `object`, `instrument`, `result`, `actionStatus`, plus ~60 subtypes (BuyAction, SearchAction...). `potentialAction` links L1 things to L2 tasks. Best pragmatic default.
- **PROV-O `prov:Activity`** — when tasks need provenance semantics (used/generated/wasAssociatedWith).
- **BBO** (BPMN-Based Ontology) — when the source material is BPMN process models.
- **ESCO / O*NET task statements** — pre-written, validated task phrasings per occupation; excellent seed lists.
- **OWL-S / planning ontologies** — legacy but the precondition/effect pattern is the right shape; copy the *pattern* (IOPE: inputs, outputs, preconditions, effects) even if not the vocabulary.

## L3 — Application bindings (never reused, but standard *formats*)

The application layer reuses serialization standards, not content: JSON Schema for tool/API shapes, JSON-LD `@context` to keep web payloads mapped to L1/L0 IRIs, SQL DDL, OpenAPI, MCP tool schema. See `output-formats.md`.

## Alignment mechanics

When reusing, record alignment explicitly rather than copying classes in:

```yaml
# in 20-domain.yaml
classes:
  - id: Merchant
    definition: "Business entity that accepts payments in exchange for goods or services."
    upper: Agent
    aligns_to: "https://schema.org/Organization"   # equivalence or subclass
    alignment: subClassOf                            # subClassOf | equivalentClass | related
```

`subClassOf` is the safe default; claim `equivalentClass` only when definitions genuinely match. Misclaimed equivalence is how downstream reasoners produce garbage.
