# Operating picture: put reviewed skills on one business screen

Use this after a domain skill exists. It joins the skill's objects, actions, and
rules to the systems discovered in the repository, and shows which business
actions are at risk, what is unlinked, and what still needs review. Run every
command from `SKILL_ROOT/scripts` paths; all of them are local and read-only
unless stated.

## 1. Look at it

```bash
python "$SKILL_ROOT/scripts/control_room.py" --repo REPO --skill SKILL [--skill SKILL2]
```

Open `http://127.0.0.1:8765/business` (system map at `/`). Without links, every
action is `UNLINKED`: health cannot be shown until a person links code to systems.

For a local repository the map also draws the API routes, GraphQL and RPC
operations, and tables the code declares (the `api` and `database` lenses), each
under the app whose folder holds it, from `code_surface.py`. They are syntax
candidates, not verified endpoints: up to 256 of each kind, API before pages, and a
gap names how many more were left out.

## 2. Link code to systems (a person decides)

```bash
python "$SKILL_ROOT/scripts/link_suggest.py" suggest --repo REPO --skill SKILL --links links.json
python "$SKILL_ROOT/scripts/link_suggest.py" confirm --repo REPO --skill SKILL --links links.json \
  --id SUGGESTION_ID --by NAME
python "$SKILL_ROOT/scripts/link_suggest.py" reject  --repo REPO --skill SKILL --links links.json \
  --id SUGGESTION_ID --by NAME --note WHY
```

Show the user each suggestion with its signals and let them choose. Never run
`confirm` or `reject` for a suggestion the user did not decide. When they decide
several, pass `--id` once per suggestion (one atomic write); use `--all-strong` only
when they asked for every STRONG suggestion. If they change their mind, `undo --id
DECIDED_ID --by NAME` removes a decision (`suggest` lists decided rows with their
IDs). To let the user click Confirm/Reject (one at a time or selected together, with
Undo) in the page instead, add `--links links.json --allow-link-decisions` to
`control_room.py`. In the page a batch is previewed before it applies and applies only
if nothing it touches changed since; each recorded batch can be undone as a whole
(Recent batches), which puts back only that batch's items and is refused if someone
changed one of them since. Never apply or undo a batch the user did not approve.

Each confirmation records the hashes of the files it rested on. When one changes, the
link is shown as needing re-confirmation (Business page, Decided list; or
`link_suggest.py reconfirm`), and it no longer counts as confirmed until a person
re-confirms or undoes it.

Agents can propose dependencies too (`agent_tools.py`, `propose_dependency`); with
`--proposals FILE` they appear beside the calls seen in traces, and
`dependency_suggest.py confirm ... --proposals FILE --id ID --by NAME` decides one from
the command line.

Before a change merges, `/impact` (or `change_impact.py --markdown` for a review
comment) lists what it touches in the business, what relies on that, and which
confirmed links will need re-confirming.

To see one entity across both layers (why it exists, what relies on it, every
relationship with how it is known, and the files behind it), open `/entity`; to give
agents the same model through permission-aware tools that can only propose, see
[entity context](entity-context.md).

## 3. Add live health (optional)

Propose feeds from what the repository declares, show them to the user, and write
only the ones they accept:

```bash
python "$SKILL_ROOT/scripts/feed_setup.py" propose --repo REPO
```

Each candidate (an HTTP probe from a compose `healthcheck`, an OTLP mapping from
`OTEL_SERVICE_NAME` or `service.name`, a Prometheus target from a mounted
`prometheus.yml`) has an ID, a strength, and the file and line that declares it.
Show the user each candidate with its evidence and each gap with its reason. Do not
accept on their behalf: WEAK candidates rest on a guess, and every probe will call
the URL it names. Then, with the IDs they chose:

```bash
python "$SKILL_ROOT/scripts/feed_setup.py" init --repo REPO --out feeds.json --accept ID [--accept ID ...]
python "$SKILL_ROOT/scripts/operate.py" up --repo REPO --feeds feeds.json --skill SKILL --links links.json
```

`init` refuses unknown IDs, an empty choice, and an existing file without `--force`,
and checks the config with the same validator `feeds.py` uses. `operate.py up` starts
the feeds (Rust when installed) and the control room together and stops both on
Ctrl-C. To run them yourself, or for feeds the repository does not declare, write
`feeds.json` by hand (see `docs/live-feeds.md`) and run:

```bash
python "$SKILL_ROOT/scripts/feeds.py" check --config feeds.json --repo REPO
python "$SKILL_ROOT/scripts/feeds.py" run --config feeds.json
python "$SKILL_ROOT/scripts/control_room.py" --repo REPO --skill SKILL --links links.json \
  --observations OBSERVATIONS  # the "output" file named in feeds.json
```

Health is what linked systems report. It never proves that an action works.

If the user's services do not send traces yet, offer one-command tracing:

```bash
python "$SKILL_ROOT/scripts/otel_setup.py" propose --repo REPO
```

It lists each app with its evidence, the trace name, and the command that starts it
with OpenTelemetry's zero-code instrumentation (Node and Python; no code change). Show
the user those commands; they start their services with them (the first run downloads
the instrumentation from npm or PyPI into a cache, so ask before running one yourself).

When services send traces, the feeds also count calls between them (a client span
in one service and the server span it caused in another). The business page lists
each call the repository does not declare as a suggested dependency, with counts
and first and last seen. Show these to the user like link suggestions; never
confirm one for them. A call can be a retry, a health check, or a one-off job.

```bash
python "$SKILL_ROOT/scripts/dependency_suggest.py" suggest --repo REPO \
  --calls OBSERVATIONS.calls.json --dependencies dependencies.json
python "$SKILL_ROOT/scripts/dependency_suggest.py" confirm --repo REPO \
  --calls OBSERVATIONS.calls.json --dependencies dependencies.json --id ID --by "THEIR NAME"
```

`--id` repeats, `--all-strong` and `undo` work as for links, under the same rule:
only what the user decided.

A confirmed dependency becomes a CALLS edge in the map and carries possible impact
like a declared `depends_on`. `operate.py up` passes `dependencies.json` beside
`feeds.json`; with `control_room.py`, pass `--dependencies`.

For more traffic, `ontologyex-feeds run --config feeds.json` does the same job in
Rust with the same files. If the user agrees to download it,
`python "$SKILL_ROOT/scripts/fetch_engine.py"` installs the prebuilt binaries into
the skill's `bin/` after checking their checksum; show them the printed
`gh attestation verify` command. The control room then discovers repositories with
Rust automatically; `--view-engine rust` also moves the graph views to Rust.

## 4. Grades, questions, queries

- `--events EVENTS` on `control_room.py` shows each rule's G0–G4
  [evidence grade](evidence-grades.md). Only the reviewer records events.
- `business_view.py --repo REPO --skill SKILL --links links.json` prints the
  same picture as JSON for scripts and CI.
- `rdf_export.py SKILL` exports the skill as N-Triples for SPARQL tools.
- `--decision-model` (with the user's consent and `ONTOLOGYEX_DECIDE_*` set)
  adds model link routes, review triage, and the page's Ask box.
  See [decision models](decision-models.md).

## Background scans

The slow reads of a repository (the code surface, suppliers, a year of git history for
owners, compliance evidence) run on a background thread, and pages read the last result,
so a page never waits for them; only the very first request waits for the first result.
A scan notes the repository's revision (every file's path, size and modification time,
and git's HEAD and index) before and after it runs and keeps its result only if nothing
changed in between: a result that finished while files moved under it is discarded and
read again. `/api/scans` says, per scan, when it last committed, for which revision,
how long it took, how many runs were discarded, and whether it still describes the
repository as it is now. On documenso (about 3,000 files, 720 commits in a year) the
slowest page answer after the first fell from 4.2 s to 0.19 s.

## Rules

The control room binds to loopback only. Links, health, and model answers are
not approvals: every result keeps `execution_authorized: false`. Do not report
`REPORTED OK` as "the action works", and do not hide `UNKNOWN`, `STALE`, or
`UNLINKED` items.

## The business's own records (operations)

What the business actually does comes from its records, not its code. Ask the user for
exports from their payment processor (payments, refunds, disputes, payouts, subscriptions,
invoices), their business bank or card statements, vendor bills, cloud cost exports, and their
helpdesk tickets and CRM deals exports, and the policies, terms and contracts that say how the
business works, and have them put the files in `REPO/.ontologyex/operations/`; `/operations` and
`operations_status` read them as weekly aggregates per currency. Never copy a record, a
customer, or an identifier into anything you write: report the aggregates and signals.
A pull (`operations.py pull stripe|aws|zendesk|hubspot`; `aws` runs their `aws` command line)
goes over the network: ask first. Stripe needs a restricted, read-only key the user sets in their
own environment; never ask for a key in chat, and never use a secret key. When a file is not recognized, write its `operations.json` entry
from the column names the page lists. See `docs/operations.md`.

