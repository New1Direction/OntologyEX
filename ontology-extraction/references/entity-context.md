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

## Suppliers

`/suppliers` (and `suppliers.py --repo REPO`) lists the outside companies the business
runs on: payments, banking data, sign-in, managed data and jobs, identity checks, email
and messaging, cloud, AI models, tax, shipping, commerce platforms, accounting, search,
maps, CRM and support, media, analytics, feature flags. A supplier is found from a
vendor SDK in a manifest (the line is cited) or a vendor's setting named in an example
env file or a compose file: the name only, never a value; a real `.env` is never read.
Helper scripts, docs, examples and tests are not the product's suppliers, and neither
are open-source libraries or self-run databases.

Each category says how much the business leans on that kind of supplier (CRITICAL to
LOW), and a CRITICAL or HIGH category with exactly one supplier is marked as the only
one declared: a fact about the sources, not a verdict that a second is needed. Tables
and routes the business keeps about its own suppliers (suppliers, vendors, purchase
orders, procurement) are listed too.

In the control room every supplier is an entity, depended on by the service that
declares it (or the project when no service does), so "what relies on Stripe" reaches
the business actions through the links a person confirmed, and change impact and agents
(`list_suppliers`) see suppliers like any other part of the system. It is what the
repository declares, not contracts, spend or usage.

## Command center

`/command` (and `command_center.py --bundle BUNDLE`) is one list of everything that needs
attention, ranked by importance from 0 to 100: systems in error that the business relies
on, actions and objects at risk, confirmations whose evidence changed, critical suppliers
with no second one declared, open questions, links to components that are gone, decisions
waiting for a person, and what nobody can see (actions not written down, services that
report no health). Each score is the sum of named factors shown beside it: how bad the
kind of problem is, how many business objects and actions reach it, and whether money
moves through it. The ranking can be read and argued with; it is not a probability, a
forecast or an approval, and the same model always gives the same ranking.

Above the list: headline counts (actions reported OK, at risk, health unknown, services in
error, suppliers, decisions waiting); how much can be seen (actions linked to a system,
services reporting health, confirmations still current, rules reviewed; "none yet" rather
than a made-up percentage); and the field the names in the model point to (e-commerce,
subscription software, marketplace, financial services, healthcare, logistics) with what a
business in that field should be able to see, each FOUND with the supplier or entity that
shows it, or NOT FOUND IN SOURCES, which says the sources do not show it, not that the
business lacks it. Agents get the same list from `what_needs_attention`.

## Weekly digest

`/digest` (and `digest.py show`) says what changed since the window began (7 or 30 days,
up to 90): which items in the command center are new, fixed, worse, better, fixed and
back again, or still open and for how long; how the headline counts and coverage moved,
each marked good or bad; the decisions people made on links, dependencies and rules, by
whom; and, with a local repository, the commits in the window and the business objects
and actions they touched (git is only read). "Copy as Markdown" and the Markdown route
(`/api/digest?days=7&format=markdown`) give the same digest for email, chat or a ticket.

It compares against history: the control room started with `--history FILE` appends the
command-center ranking to that JSON-lines file when it changes, and once a day when it
does not (`operate.py up` records to `REPO/.ontologyex/history.jsonl` unless
`--no-history`). For a nightly job without a running control room, `digest.py record
--bundle BUNDLE --history FILE` does the same. Without history the digest says so and
lists what needs attention now. It compares rankings, not outcomes. Agents get it from
`weekly_digest`.

## Before a change merges

`/impact` (and `change_impact.py` on the command line) reads a change from git and
says what it touches and what relies on that. Git is only read: `git diff` and
`git ls-files`, no checkout, hooks or external diff programs.

```bash
python "$SKILL_ROOT/scripts/change_impact.py" --repo REPO --base main --head my-branch \
  --control-room http://127.0.0.1:8765 --markdown
```

With no refs it is the working tree against HEAD (untracked files included); with
`--base` alone, the working tree against that ref; with both, what the head branch
changed since it left the base. `--skill-from OLD --skill-to NEW` adds the bindings a
new compiled skill version changes (`change_plan.py`). `--markdown` prints a short
review comment.

A change touches:

- systems declared in a changed file, marked when the declaring line itself changed;
- business objects and actions whose binding's source code changed, or whose skill
  cites a changed file (matched by path suffix, with line hits when lines are known);
- business objects and actions whose binding changed in a new skill version;
- both ends of every confirmed link whose recorded evidence includes a changed file.
  Those links are listed as "re-confirm after merge": once the change lands they will
  read as inferred until a person re-confirms them.

What relies on the touched entities follows, each through its strongest path, with
how it is known. Changed files that nothing in the model names are listed, not
dropped; Lean proves every changed file is either mapped or listed, and that adding
changes never hides an entity a smaller change touches. A compose file is one
declaring file for every service in it, so changing any line touches all of them (a
line hit marks the service whose own lines changed, when discovery knows the line).

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
| `list_suppliers` | read | the outside companies the business runs on, what relies on each, single suppliers |
| `what_needs_attention` | read | everything that needs attention, ranked, with factors, next steps, coverage and field |
| `weekly_digest` | read | what changed in the last N days: new, fixed, worse, better, decisions, code shipped |
| `analyze_change` | read | what a change (git refs, or a list of files) touches and what relies on it |
| `propose_link` | additive | appends a link proposal; a person decides it |
| `propose_dependency` | additive | appends "A depends on B" with a reason; a person decides it |
| `propose_rule` | additive | appends a business rule for an object or action, citing the lines it rests on |

There are no tools that delete or change decisions, and the server refuses to
register one.

**Proposals.** `propose_link` and `propose_dependency` need a policy that allows them
and `--proposals FILE`; start the control room with the same `--proposals FILE` (and
`--dependencies FILE` to record dependency decisions). A proposal appears as a WEAK
suggestion with the agent's reason: a link on the Business page and the entity page,
a dependency beside the calls seen in traces. Only a person confirms or rejects it,
through the existing decision route and its history. A confirmed dependency proposal
keeps the agent's reason as its evidence (never invented trace counts) and becomes a
DEPENDS_ON edge marked PROPOSED_BY_AGENT_CONFIRMED_BY_PERSON. A link or dependency a
person rejected, one already confirmed, or a dependency the repository already
declares is refused if proposed. After confirmation the decision records which agent
proposed it. The same proposals file holds every kind.

**Rule proposals.** `propose_rule` names a business object or action, the rule as one
sentence, and the file and lines (at most 80) it rests on. The control room checks the
lines exist and shows them (files that hold secrets are never quoted); whether they
support the rule is for the person reading them. Start the control room with
`--rules FILE` to record decisions (`rule_suggest.py` does the same from the command
line). A confirmed rule shows on the entity page beside the skill's own rules, with who
confirmed it and which agent proposed it, and records the hash of the cited lines:
when they change, the rule reads as needing re-confirmation until a person
re-confirms or undoes it. Nothing here edits a compiled skill.

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
