# Entity context: start from any part of the business, reach the evidence

The control room already knows three things about an organization: what its files
declare (services, dependencies, routes, tables), what its domain skills say
(objects, actions, rules, open questions), and what people decided (links and
dependencies confirmed or rejected, with who and when). The entity context joins
them for one entity, in both directions, and labels every relationship with how it
is known. The entity page and the agent tools read the same answer.

## Standings

| Standing | Meaning | Comes from |
| --- | --- | --- |
| CONFIRMED | a person decided it, and the files it rested on are unchanged | a confirm decision (who, when, note; and which agent proposed it, if one did) |
| SOURCE | a file states it | compose dependencies, discovered declarations, domain-skill relations |
| INFERRED | proposed, not decided, or decided on evidence that has since changed | ranked link suggestions, calls seen in traces, agent proposals, reachability, stale confirmations |
| UNKNOWN | a gap | a link whose component is gone, an action with no confirmed system, an open question |

A chain of relationships (an explanation path, or the route by which a change could
reach something) is only as strong as its weakest hop. "What relies on this" is
declared reachability, not a measured blast radius. Lean proves these rules
(`OntologyEX.Context` in `proofs/lean`), and Python, Rust and Lean must print the
same table for them (`tests/conformance.py`).

## When the evidence changes

A confirmation records what it rested on: the SHA-256 of the file that declares the
component, of the binding's source file in the repository, and of the file the domain
skill was compiled from. Every answer recomputes them. If one changed, appeared or
disappeared, the link reads as INFERRED, "confirmed before the evidence changed", with
the changed files named, until a person re-confirms it (recording the evidence as it is
now) or undoes it. The entity page and the Business page show these with Re-confirm
and Undo; `link_suggest.py reconfirm --id DECIDED_ID --by NAME` does the same from the
command line. Links decided before watermarks existed stay CONFIRMED and say the
evidence was not recorded; re-confirming records it. A binding file that the repository
holds in more than one place is not guessed: only the skill's compiled hash is watched
for it.

## The page

Start the control room as usual and open `http://127.0.0.1:8765/entity`, or follow
"Open entity page" from an item on the Business page or a node on the system map.
Each page shows why the entity exists, its relationships grouped by standing (with
the decision and the file and sha256 behind each), suggested changes with their
signals, what relies on it, recent decisions, unknowns, and an "explain a
connection" box that traces every path to another entity. With
`--allow-link-decisions`, suggestions have Confirm and Reject buttons that use the
same token-protected route as the Business page.

The page reads `/api/search?q=`, `/api/context?id=` and `/api/explain?from=&to=`.
With `--view-engine rust` the Rust engine answers (`ontologyex-view context`), and
`tests/rust_context_parity.py` checks it agrees with Python.

## Agent tools (MCP)

`agent_tools.py` gives agents the same model through structured tools. Register it
with an MCP client, for example Claude Code:

```bash
claude mcp add ontologyex -- python "$SKILL_ROOT/scripts/agent_tools.py" serve \
  --control-room http://127.0.0.1:8765 --agent claude --operator "YOUR NAME" \
  --audit REPO/.ontologyex/agent-audit.jsonl
```

Or call one tool without MCP: `agent_tools.py call get_entity_context '{"id": "..."}' ...`.
`agent_tools.py tools` lists the tools with their kinds and inputs. Instead of a
running control room, `--bundle FILE` reads a file from
`control_room.py ... --bundle-only`.

| Tool | Kind | Answers |
| --- | --- | --- |
| `search_entities` | read | entity IDs by words in the name, ID or summary |
| `get_entity_context` | read | everything above for one entity |
| `explain_relationship` | read | every path of up to three hops between two entities, strongest first |
| `analyze_impact` | read | what relies on an entity, with path and standing |
| `recent_changes` | read | the newest recorded decisions, for one entity or all |
| `list_suggestions` | read | proposals nobody has decided, with strength, signals and proposer |
| `list_stale_decisions` | read | confirmed links whose evidence changed since, with the changed files |
| `propose_link` | additive | appends a link proposal; a person decides it |

There are no tools that delete or change decisions, and the server refuses to
register one.

**Proposals.** `propose_link` needs a policy that allows it and `--proposals FILE`;
start the control room with the same `--proposals FILE`. The proposal appears as a
WEAK suggestion with the agent's reason, on the Business page and the entity page.
Only a person confirms or rejects it, through the existing decision route and its
history. A link a person rejected is refused if proposed again. After confirmation
the decision records which agent proposed it.

**Policy.** A JSON file:

```json
{"schema_version": 1,
 "allow_tools": ["search_entities", "get_entity_context", "explain_relationship",
                 "analyze_impact", "recent_changes", "list_suggestions", "propose_link"],
 "deny_paths": ["config/prod/*"],
 "deny_entities": ["discovery:service:compose.yml:vault"]}
```

Without one, the read tools are allowed and `propose_link` is not. Files that
commonly hold secrets (`.env*`, keys, certificates, credentials, `.npmrc`, `.netrc`)
are always withheld, whatever the policy says. A denied entity is left out of search,
refused by ID, and replaced by `[withheld by access policy]` wherever another answer
would name it. Unknown keys or tool names in the policy stop the server at startup.
Refusals say what to change.

**Audit.** Every call, allowed or refused, appends one line to the audit file before
the answer is returned: an ID, the time, the actor (`agent:NAME`), the operator it
acts for (stated, `on_behalf_of_verified: false`), the tool and its kind, a sha256 of
the arguments (not the arguments), the entity IDs named, and the outcome. If the line
cannot be written, the call is refused.

## Boundaries

The context reads local sources and recorded decisions. Status is source-reported and
unattested. The operator name is what the operator typed, not an identity check.
Nothing here runs, deploys or approves an action, and nothing an agent does changes a
link until a person decides.
