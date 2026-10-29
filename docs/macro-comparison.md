# What OntologyEX took from Macro, and what it did not

[Macro](https://github.com/macro-inc/macro) is a workspace that organizes work:
documents, chat, email, tasks, and agents that act in them. OntologyEX reads an
organization's sources to explain the business behind that work, and lets people
decide what is true. This note compares their architectures, ranks what was worth
borrowing, and records what was built. Macro is AGPL-3.0. No Macro code was copied;
the patterns below were reimplemented from scratch. File references are paths in
Macro's Rust workspace at the time of reading.

## Comparison

| Area | Macro | OntologyEX before | OntologyEX now |
| --- | --- | --- | --- |
| Entity graph | References between documents, channels, tasks and CRM records stored with each entity; backlinks answered by query over a reverse index (`crates/channels/.../references.rs`, `crates/documents/src/outbound/mention_tracker.rs`) | Two graphs: the discovered system graph and the business picture, joined only by confirmed links; edges one-directional in the UI | One entity context over both, both directions, every edge with its standing, decision and evidence |
| Provenance | Agent-created edges record source agent/user and who linked them | Decisions record who and when; history written but never read back | History read back into answers ("confirmed by Ana on 2026-10-28"); agent provenance kept after a person confirms |
| Unified context | Mentions, properties and a memory service assembled into model context (`crates/memory/src/domain/service.rs`) | Business page and system map, separate | Entity page and `get_entity_context` give the same answer to people and agents |
| Agent tools | Every tool declares a `ToolKind` (ReadOnly, Additive, Destructive) or it does not compile (`crates/ai_toolset/src/annotations.rs`, `toolset/types.rs`); "user tools" return a proposal a person executes (`crates/ai_tools/src/user_tool_review.rs`); a fail-closed approval gate (`crates/agent_inmem/src/outbound/approval_gate.rs`) | No agent interface; an agent had to call CLIs | MCP server with declared kinds, read tools, one additive propose tool, no destructive tools |
| Permissions | Multi-tenant entity ACLs, share levels, `explain_access` (`crates/entity_access/...`) | Single local operator; a token for writes, nothing for agent reads | A policy per agent: allowed tools, withheld files and entities, secrets always withheld |
| Audit | Activity records with actor and on-behalf-of; deterministic record IDs | Decision history only | Every agent call recorded (actor, operator, tool, argument digest, entities, outcome); no audit, no answer |
| Events | Kafka/SQS, outboxes, a broker (`crates/macro_event_broker`) | Files, polling, local HTTP | Unchanged on purpose |
| Rust | Hexagonal crates (domain, ports, adapters), newtype IDs, in-memory fakes | Rust engine mirrors Python, checked by parity tests | Entity context in Rust with the same parity discipline |
| Verification | Unit and integration tests | Lean and Bend proofs of the decision and health rules, cross-language conformance | New Lean theorems for proposals and path standing, conformance rows in three languages |

**What Macro does better:** a single typed reference model with backlinks everywhere;
tool kinds enforced by the type system; agent edits as reviewable proposals; audit of
what agents read and did, with actor and principal separated; multi-user
permissions that can explain themselves.

**What OntologyEX already did better:** every claim tied to file bytes (path and
sha256); an explicit difference between declared, observed, inferred and decided;
human decisions as the only way a link becomes true, proved in Lean and Bend; the
same logic in Python and Rust checked against each other; local-first, no services
to run.

## Classification

| Pattern | Decision | Why |
| --- | --- | --- |
| Tool kinds declared per tool, mapped to MCP hints | ADOPT | Cheap, and makes "no silent mutation" checkable |
| Agent writes return a proposal a person executes | ADAPT | Became `propose_link` into the existing suggestion queue; humans confirm through the existing route |
| Fail-closed approval with an actionable refusal | ADAPT | Policy refusals say what to change; a failed audit write refuses the call |
| Actor vs on-behalf-of in audit records | ADAPT | Agent is the actor; the operator is recorded as stated, marked unverified |
| Edge provenance (agent or person, and who) | ADOPT | A confirmed link keeps which agent proposed it |
| Backlinks by query with a reverse index | ADAPT | Computed per request from the bundle; no index needed at this size |
| Dangling targets shown as missing, never inferred deleted | ALREADY EXISTS | `link_gaps` (LINK_COMPONENT_NOT_DISCOVERED) now surfaces as UNKNOWN |
| Sticky rejection | ALREADY EXISTS | Kept; agent proposals cannot resurface a rejected link (proved) |
| Decision record that names who and why | ALREADY EXISTS | History file; now read back |
| Decision revision watermark (re-open when evidence changes) | ADAPT | Built: confirmations record file hashes and read as INFERRED once they change |
| `explain_access` | ADAPT, partly | `explain_relationship` explains why two entities are connected; policy refusals explain access |
| Review stacks with compare-and-swap revisions | REJECT for now | One local operator; batch decisions are already atomic |
| Kafka, SQS, Redis, DynamoDB, OpenSearch, pgvector | REJECT | Constraint: no new brokers or databases; files and loopback HTTP are enough |
| Multi-tenant ACLs, acting-as, OAuth client registration | REJECT | Single operator, loopback only; a per-agent policy covers the real need |
| LLM-judged free-text memory | REJECT | Conflicts with evidence-first answers; decisions and citations are the memory |

## Opportunities, ranked

| # | Opportunity | Impact | Complexity | Risk | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | Entity context across both layers, with standings, provenance, impact and explanations (Python + Rust) | High: answers "why does this exist, what depends on it, what supports it" | Medium | Low: read-only | Built |
| 2 | Agent tools with kinds, policy and audit, read-only by default | High: agents use the same model safely | Medium | Low | Built |
| 3 | Agent proposals into the human decision queue | High: agents contribute, people decide | Low: reuses the queue | Low: proved never to add a link alone | Built |
| 4 | Entity page with backlinks, evidence, suggestions and decisions | High for operators | Low | Low | Built |
| 5 | Decision watermark: record the evidence sha256 a decision saw and re-open it when the file changes | High: keeps CONFIRMED honest over time | Medium | Medium: touches the decision format | Built |
| 6 | Impact of a proposed change (diff, or a skill version) mapped through entity context | High | Medium | Low | Built |
| 7 | Dependency proposals from agents (same pattern as links) | Medium | Low | Low | Next |
| 8 | Multi-user identity for decisions | Medium | High | Medium | Not now |

## What was built (the vertical slice)

- `entity_context.py` and `engine/src/context.rs`: context, explain, search, open
  suggestions, recent decisions. Rust gives the same answers
  (`tests/rust_context_parity.py`: the demo plus 120 seeded bundles, about 2,150
  cases).
- Control room: `/entity`, `/api/context`, `/api/search`, `/api/explain`,
  `/api/bundle`, `--proposals`; the Business page and system map link to the entity
  page. On `--view-engine rust` the Rust engine answers.
- `agent_tools.py`: MCP over stdio and a one-shot CLI; six read tools and
  `propose_link`; policy; audit.
- `link_suggest.py`: proposals file (append-only, validated on read) merged into the
  suggestion queue without raising strength.
- Lean: `proposals_alone_change_nothing`, `blocked_proposal_never_queued`,
  `agent_links_were_confirmed`, `confirmed_only_by_person`, `path_confirmed_iff`,
  `hop_no_weaker_than_path`, `path_is_a_hop`; a `path_standing` conformance section
  printed by Python, Rust and Lean.
- Tests: `tests/test_entity_context.py` (standings, provenance, impact, explain,
  refusals, and the HTTP routes on both engines) and `tests/test_agent_tools.py`
  (kinds, policy, redaction, audit, MCP framing, proposals, and an end-to-end run
  where an agent proposes over HTTP and a person confirms with the page token).

The operator path: search a business object, open it, read why it exists and what a
person confirmed, follow a relationship to a system, see what relies on it, inspect
an agent's suggestion and its reason, confirm or reject it, and see the decision
(who, when, proposed by which agent) in the history. An agent reaches the same model
through the tools, under a policy, with every call audited.

## What remains

- Impact is declared reachability. It does not weigh how much traffic or money flows
  along a path.
- The operator name is typed, not authenticated; the policy is per agent process, not
  per user.
- Agents can propose links and dependencies, not rules or skill edits.
- Bend models the original decision rules; the new proposal and path laws are in
  Lean only.

## Next three

1. ~~Decision watermark~~ (built: `watermark.py`; confirmations record file hashes,
   read as INFERRED once one changes, and a person re-confirms or undoes them).
2. ~~Change impact~~ (built: `change_impact.py`, `/impact`, `analyze_change`; a diff
   or a new skill version, the entities it touches, what relies on them, and the
   confirmed links to re-confirm after merge).
3. ~~Dependency proposals from agents~~ (built: `propose_dependency`, through the
   same queue, policy and audit; the queue laws are proved for any fact type). Rule
   and skill-edit proposals remain.
4. Finer declarations: discovery records each compose service's own lines, so a
   change to one service touches only it (today a compose edit touches every
   service in the file, and every watermark that recorded the file).
5. Watermarks for confirmed dependencies: their evidence is trace observations, not
   files, so "changed" needs its own rule (for example, no calls seen for N days).
