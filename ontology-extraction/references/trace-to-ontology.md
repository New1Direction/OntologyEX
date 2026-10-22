# Trace → Ontology (experimental, draft-only)

Goal: use **observed agent behavior** as candidate evidence alongside repository code,
documents and human policy. This is not an automatic ontology authority, Palantir Foundry
replica, production decision engine, or completed autonomous extraction system.

## Quick start

From the repository root:

```bash
python ontology-extraction/scripts/trace_extract.py examples/trace-to-ontology/demo.jsonl   --out build/trace-draft
python -m unittest tests/test_trace_extract.py -v
```

Inspect `build/trace-draft/ontology-draft.json`. No mutation, network calls, model calls,
tool execution, or automatic promotion occurs. The output directory must not exist.

## OpenTelemetry GenAI span adapter

This version also accepts **locally exported OTLP/JSON** via a strict normalization
step. Only `gen_ai.operation.name`, `gen_ai.agent.name`,
`gen_ai.tool.name`, `ontologyex.root_goal_id`, and
`ontologyex.entity_type` can influence the normalized output.
Unrecognized attributes—including prompt, completion, tool arguments, output,
environment variables, and raw span names—are ignored, not persisted.
The first-party ontology-specific attributes are optional; standard GenAI
agent and tool spans need no special instrumentation to contribute a candidate.

```bash
python ontology-extraction/scripts/otlp_normalize.py local-otlp-export.json \
  --out build/genai-spans.jsonl
python ontology-extraction/scripts/trace_extract.py build/genai-spans.jsonl \
  --out build/genai-ontology-draft
```

Only a bounded OTLP/JSON `resourceSpans → scopeSpans → spans` structure is
supported. Partial traces with missing parent spans retain the observed spans
without creating nonexistent parents. This is not an OpenTelemetry collector
or complete spec implementation; instrumenters and exporters must minimize
sensitive data before writing the local raw export. **Do not upload raw production
traces.**

## Input: normalized JSONL, not arbitrary chat logs

One JSON object per line, **schema_version=1**. Required fields are
`schema_version, trace_id, span_id, kind, status`. Optional:
`parent_span_id, timestamp, root_goal_id, agent, tool, entity_type, entity_id, operation, effect`.

Allowed kinds: agent, tool, action, decision, observation.
Allowed status: ok, error, unknown.
Allowed effects: created, updated, deleted, read, none, unknown.

Timestamps, when present, must use `YYYY-MM-DDTHH:MM:SS[.fraction](Z|±HH:MM)`
with a valid calendar date and 1–9 fractional digits. This RFC3339 subset requires
uppercase `T`/`Z` and a known timezone: timezone-free values, `-00:00`, leap seconds,
invalid offsets, and dates that overflow years 1–9999 when converted to UTC are
rejected. Timestamp ranges are ordered chronologically and emitted in UTC with
`Z`; fractional precision is preserved and trailing fractional zeros are removed.

Example:

```json
{"schema_version":1,"trace_id":"t-1","span_id":"s-1","kind":"tool","agent":"builder","tool":"db.migrate","entity_type":"Database","operation":"migrate","status":"ok","effect":"updated","root_goal_id":"goal-demo"}
```

Raw prompts, messages, tool arguments and outputs, source code, credentials, hostnames
with query strings, email addresses, free-text rationales, and arbitrary metadata keys
are prohibited. Unknown fields fail closed. The accepted identifiers are a restrictive
ASCII vocabulary; normalization and secret classification must occur **before** this tool.

The file is bounded to 1 MiB and 512 spans (4 KiB per line), with unique span IDs per
trace and validated, acyclic parent references within each trace. All output is
deterministic from exact bytes. Timestamp normalization does not change the input
bytes used for file or line hashes.
The file and line SHA-256 hashes are evidence **identity**, not verification of who
produced the trace. The graph uses opaque hashes for span and entity instance IDs.
Hashes are pseudonyms, **not** guaranteed anonymization against dictionary attacks:
never provide private identity tokens or secrets to the normalizer.

## What ships

- Observed object types, action types and tool/type relationships for review.
- Graph of spans, agent roles, tool names, object types, operations and outcomes.
- Per-observation line number, exact input/line SHA-256 and corresponding graph edges.
- Empty rule, permission and inferred-precondition arrays.
- Explicit `DRAFT_NOT_APPROVED` and `execution_authorized=false`.

The output does **not** convert failures, repeated tool calls or correlated copies into
a causal rule, policy authorization, or independent proof. For example, seeing a
database migration before a deployment is **not** enough to infer a mandatory
precondition. A reviewer must reconcile candidate claims with requirements, actual
code behavior, conflicting evidence and tests.

The existing four-layer compiler remains authoritative for compiled domain-skill
candidates. Trace drafts are **not silently merged** into `60-contract.json`.

## Next steps: from trace extraction to operational ontology

1. **Adapters and boundaries:** normalize consented
   Claude Code/Codex transcripts into this allowlist. Provide a local review/redaction
   step before ingestion. Keep raw traces locally and encrypt/scope storage.
2. **Typed candidate graph:** stable entity identities, event time, temporal link
   validity, source dependency groups, and evidence atoms (including PR #6's provenance
   work). Explicitly mark uncertain extraction.
3. **Candidate → proposed ontology:** optional bounded language-model extraction,
   structured output validation and conflict detection. No automatic promotion.
4. **Operational layer:** object/link types, actions, functions, policies, expected
   effects, actor capabilities, approval workflow and audit/change history.
5. **Simulation before execution:** replay traces and test proposed action semantics,
   role permissions, state transitions, refusal cases, rollback and idempotency.
6. **Real adapters:** governed read/write connectors with transactional integrations,
   live state validation and explicit operator approval. No agent can authorize itself.
7. **Benchmarks:** hidden held-out tasks, extraction precision/recall, false-policy
   promotions (target zero), contradiction retention, decision replay accuracy,
   drift impact, marginal task-cost improvement, and memory usefulness.

## Where this fits among current projects

- [Palantir Ontology](https://www.palantir.com/docs/foundry/ontology/overview):
  semantic **and** kinetic objects/links/actions/functions with dynamic security,
  interfaces and operational write-backs. That is a capability benchmark, not a
  claim of parity.
- [Graphiti](https://github.com/getzep/graphiti): learned/prescribed context graphs,
  temporal validity, episodes, graph retrieval. An excellent reference for memory.
- [TypeDB](https://github.com/typedb/typedb): typed entities, relations, role semantics,
  declarative queries and functions. A reference for type safety and query language.
- [OpenLineage](https://github.com/OpenLineage/OpenLineage): run/job/dataset lineage
  and extensible facets. A possible downstream format for cross-system lineage.
- [OpenTelemetry GenAI conventions](https://opentelemetry.io/docs/specs/semconv/):
  observability standard family; the bounded GenAI OTLP/JSON adapter is shipped, but arbitrary OTLP and complete semantic-convention support are not.

The distinctive aim for OntologyEX is the **evidence → modeled operational decision →
governed action → observed outcome → ontology revision** cycle, rather than graph
extraction alone.

## Proof boundary

Only deterministic extraction of explicit normalized metadata and regression tests
is implemented here. Native-agent trace parsing, AI-based ontology discovery,
security enforcement for actual business systems, persistent temporal graph queries,
operator review UI, and live write-back are future milestones.

Native-agent improvement benchmark status: **NOT_RUN**. Passing tooling tests or the
developer-authored cachetools reference case does not demonstrate improved agent
task success, lower cost, or better ontology extraction. Those claims require a
separate held-out comparison against agent baselines.
