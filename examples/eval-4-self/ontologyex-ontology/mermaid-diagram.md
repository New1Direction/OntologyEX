# Review diagrams — ontologyex self-ontology

Emitted from `20-domain.yaml` + `30-task.yaml` + `40-application.yaml` per
`references/output-formats.md` (§6 Mermaid). Split into two views to stay under ~25 nodes each:
the **domain spine** (L0 anchoring + L1 relations) and the **workflow + L3 binding** view.

## 1. Domain spine — L0 anchors ↑ L1 classes ↓ relations

```mermaid
classDiagram
  direction TB

  class Content["L0 · gist:Content"]
  class Category["L0 · gist:Category"]
  class Specification["L0 · gist:Specification"]
  class Agent["L0 · gist:Agent"]
  class Organization["L0 · gist:Organization"]

  class Ontology["L1 · Ontology"]
  class OntologyLayer["L1 · OntologyLayer"]
  class UpperOntology["L1 · UpperOntology"]
  class OntologyClass["L1 · OntologyClass"]
  class Relation["L1 · Relation"]
  class OntologyTask["L1 · OntologyTask"]
  class ApplicationConcept["L1 · ApplicationConcept"]
  class Mapping["L1 · Mapping"]
  class CompetencyQuestion["L1 · CompetencyQuestion"]
  class EvidenceSource["L1 · EvidenceSource"]
  class ConsumerBinding["L1 · ConsumerBinding"]
  class ExtractionTarget["L1 · ExtractionTarget"]
  class OperatingAgent["L1 · OperatingAgent"]

  Content <|-- Ontology
  Category <|-- OntologyLayer
  Specification <|-- UpperOntology
  Category <|-- OntologyClass
  Category <|-- Relation
  Specification <|-- OntologyTask
  Content <|-- ApplicationConcept
  Content <|-- Mapping
  Content <|-- CompetencyQuestion
  Content <|-- EvidenceSource
  Content <|-- ConsumerBinding
  Organization <|-- ExtractionTarget
  Agent <|-- OperatingAgent

  Ontology --> OntologyLayer : has_layer
  Ontology --> ExtractionTarget : models_target
  Ontology --> CompetencyQuestion : tested_by
  Ontology --> ConsumerBinding : emitted_as
  OntologyClass --> UpperOntology : anchors_to
  OntologyClass --> EvidenceSource : cites
  Relation --> OntologyClass : relation_domain / range
  OntologyTask --> OntologyClass : consumes / produces
  OntologyTask --> OperatingAgent : performed_by
  ApplicationConcept --> OntologyClass : binds_to
  Mapping --> ApplicationConcept : mapping_app
  Mapping --> OntologyClass : mapping_domain
```

## 2. Workflow (L2) + application binding (L3)

The umbrella `ExtractOntology` decomposes into 11 leaf tasks; each writes one file artifact (L3)
and one becomes one MCP tool. L2 tasks shown as the pipeline; L3 artifacts as the files they touch.

```mermaid
flowchart TB
  subgraph L2["L2 · ExtractOntology pipeline"]
    direction TB
    A[ScaffoldWorkspace] --> B[ScopeTarget] --> C[WriteCompetencyQuestions]
    C --> D[MineSources] --> E[AnchorUpper] --> F[BuildDomainLayer]
    F --> G[BuildTaskLayer] --> H[ProjectApplicationLayer] --> I[ValidateOntology]
    I -->|0 errors| J[GenerateMappings] --> K[EmitConsumerBinding]
    I -->|errors → exit 1| I
  end

  B -.writes.-> f00["L3 · file.00-scope → CompetencyQuestion"]
  E -.writes.-> f10["L3 · file.10-upper → UpperOntology"]
  F -.writes.-> f20["L3 · file.20-domain → OntologyClass + Relation"]
  G -.writes.-> f30["L3 · file.30-task → OntologyTask"]
  H -.writes.-> f40["L3 · file.40-application → ApplicationConcept"]
  I -.runs.-> sv["L3 · scaffold.validate → Ontology"]
  J -.runs.-> sm["L3 · scaffold.mappings → Mapping"]
  K -.emits.-> mcp["L3 · example.mcp-tools-json → ConsumerBinding"]
```
