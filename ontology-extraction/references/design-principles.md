# Ontology Design Principles & Anti-Patterns

These rules keep the four-layer ontology usable as production infrastructure for agents, apps, and analysts. The core discipline: **model how the real-world business operates, not how source tables, departments, or vendor APIs happen to be shaped.**

## 1. Domain-driven vocabulary first

Use the language a domain expert would use in a meeting. L1 classes are real-world nouns; L2 tasks are real-world verbs; L3 artifacts are the implementation details that map upward.

Recommended order:

1. Understand the business domain and competency questions.
2. Design L1 objects, relations, L2 actions, and reusable interfaces.
3. Map source tables, APIs, events, tools, and UI concepts into L3.

### Prevents

- **God Object** — one object type absorbs several real-world entities or many unrelated team-specific fields.
- **Kitchen Sink** — every source column is copied into the ontology, including batch ids, ETL flags, extraction metadata, debug fields, and dead schema.
- **Table-shaped ontology** — L1 mirrors database tables instead of the business.

## 2. One shared domain, not departmental replicas

The ontology should break enterprise silos. Prefer a single canonical `Customer`, `Order`, `Asset`, or `Refund` when the real-world entity is the same, even if sales, support, billing, and operations each have a source-system representation.

Use L3 application concepts to preserve source-system specificity:

```yaml
concepts:
  - id: salesforce_account
    kind: api_type
    binds: Customer
  - id: billing_customer_row
    kind: db_table
    binds: Customer
```

### Prevents

- **Department / System Silos** — `SalesCustomer`, `SupportCustomer`, and `BillingCustomer` drift apart and give agents conflicting truths.
- **Action Sprawl** — many tiny property-level actions such as `UpdatePhone`, `UpdateEmail`, and `UpdateAddress` instead of one business action such as `UpdateContactInformation`.
- **Golden Hammer** — every change is forced through a manual operational action when a pipeline, event, or derived view is the correct update path.

## 3. Stable cores, additive extensions

Treat a production ontology class as an API contract. Once consumers rely on it, extend additively rather than reshaping it. Add links, interfaces, optional fields, or new L3 bindings before modifying a stable L1 class in place.

Mark lifecycle status explicitly when useful:

```yaml
classes:
  - id: Customer
    status: active        # draft | active | deprecated
    definition: "Party that receives goods or services or pays for them."
```

### Prevents

- **Schema Overload** — every new use case mutates a shared production object and creates ripple effects across downstream apps, agents, dashboards, and mappings.
- **Breaking semantic contracts** — old tools still call a class name whose meaning quietly changed.

## 4. Compose behavior with interfaces, avoid deep hierarchies

Do not build fragile inheritance towers just to share a trait. If unrelated classes share behavior or fields, model the shared trait as an interface and let classes implement it.

```yaml
interfaces:
  - id: Addressable
    definition: "Can be associated with a postal or physical address."
    properties: [address]
  - id: Monetary
    definition: "Carries an amount and currency."
    properties: [amount, currency]

classes:
  - id: Customer
    implements: [Addressable]
  - id: Invoice
    implements: [Monetary]
```

Use precise semantic labels for classes. Prefer `Warehouse`, `Vehicle`, `Subscription`, or `TradingPosition` over vague abstractions such as `Asset`, `Object`, `Entity`, `Record`, or `Item` unless the vague term is genuinely the business term and is well-defined.

### Prevents

- **Deep hierarchy trap** — `Item -> PhysicalAsset -> Building -> Arena` becomes brittle and hard to extend.
- **Misnomer** — vague names confuse humans and degrade autonomous agents that choose tools by reading ontology labels and descriptions.

## 5. The compounding asset rule

A good ontology makes future use cases mostly additive. New apps should map into existing L1/L2 concepts, add narrow interfaces, or add source-specific L3 concepts without remodeling the whole domain.

Before merging, ask:

- Would a domain expert recognize these names?
- Would this L1 survive a competitor/vendor/source-system swap?
- Are source-system quirks isolated in L3?
- Could an agent choose the right action from task names, preconditions, and effects?
- Are shared traits modeled once as interfaces rather than copied into many classes?
- Does every mapped field earn its place, or is it technical noise?

## Validator coverage

`scaffold.py validate` enforces structural rules and includes lightweight heuristic warnings for these anti-patterns. Heuristics are not a substitute for human review: a warning means "inspect this design choice," not necessarily "change it." 
