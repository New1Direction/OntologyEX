# ontologyex ontology (self-eval / eval-4)

The Agent Ontology Kit run **on itself**, in retrofit mode. The target is this repository;
the modeled field is ontology / knowledge engineering. Produced by following
`AGENT_SKILL.md`, validated by the kit's own `scaffold.py`.

## Files

| File | Layer | What it holds |
|---|---|---|
| `00-scope.md` | — | target (retrofit: this repo), consumer (MCP + docs), boundary, 13 competency questions |
| `10-upper.yaml` | L0 | gist anchors (Agent, Content, Category, Specification, …) |
| `20-domain.yaml` | L1 | 13 classes (Ontology, OntologyLayer, OntologyClass, Relation, OntologyTask, …) + 14 relations |
| `30-task.yaml` | L2 | 12 workflow tasks; `ExtractOntology` decomposes into 11 leaf steps |
| `40-application.yaml` | L3 | 16 concepts — the repo's real artifacts (scaffold subcommands, file templates, docs, refs, evals) |
| `50-mappings.yaml` | — | generated app→domain→upper crosswalk (16 rows) |
| `mcp-tools.json` | binding | 11 MCP tools derived from the L2 leaf tasks |

## Reproduce

```bash
python ../../../ontology-extraction/scripts/scaffold.py validate .   # 0 errors, 0 warnings
python ../../../ontology-extraction/scripts/scaffold.py mappings .    # regenerates 50-mappings.yaml
```

## How the consumer eats this

The consumer is an AI agent that operates the kit. Each L2 leaf task became one MCP tool in
`mcp-tools.json`, with preconditions and effects folded into the tool description so the agent
won't, for example, call `emit_consumer_binding` before `validate_ontology` passes. The
umbrella `ExtractOntology` task is orchestration (it has `decomposes_to`), so it is intentionally
*not* a tool.

## Self-referential note

This ontology is itself an instance of its own `Ontology` class, `models_target`-ing the kit
that produced it. `example.stripe-ontology` and `example.mcp-tools-json` (L3) point at eval-1,
demonstrating that a prior run's outputs are instances of `Ontology` and `ConsumerBinding`.
