# Business overview: a whole repository, explained

Use this when the user wants to understand their project, product, business, or
company as a whole: what it is, who it serves, how money moves, how people use it,
where it breaks, and what nobody wrote down. The result is one offline page (the
Business X-Ray) where every claim links to the lines it rests on, plus a short
summary in chat. Nothing in their repository is run or changed; the scan only reads.

`$SKILL_ROOT` is the `ontology-extraction` folder of the kit: `KIT/ontology-extraction`
when you cloned it as [AGENTS.md](../../AGENTS.md) says.

## 1. Scan

```bash
python "$SKILL_ROOT/scripts/overview.py" scan --repo REPO --out REPO/.ontologyex/overview
```

It prints a summary; `scan.json` has the full lists, each with the file (and line)
that shows it:

- **apps**: folder, language, frameworks (Next.js, Remix, Hono, Django, Echo, ...);
  a package that only holds dev tooling is marked `role: tooling only`;
- **services** and **datastores**: what compose files run (including overrides and
  examples such as `compose.override.yml` or `compose.example.yml`), plus databases the
  dependencies show when nothing runs them (`inferred_from`: the psycopg library,
  the Prisma datasource, the Supabase config);
- **integrations**: outside services the code depends on, grouped as payments,
  auth, email and messaging, AI, data and queues, cloud, analytics and monitoring
  (a family such as `@aws-sdk/*` is one row, the rest named under `also`);
- **code**: routes and pages (with router prefixes and Rails namespaces), GraphQL and
  RPC operations, tables, and jobs (Celery, ActiveJob, Sidekiq, cron schedules, ...) the
  code declares, each with its file and line (the first 30 to 80; the `full_list`
  command prints all of them);
- **docs**, in reading order: README, architecture and agent notes (AGENTS.md,
  CLAUDE.md, `.cursor/rules`), decision records, the rest;
- **read_first**: entry points (what the Dockerfile or start script runs first),
  routes and APIs, data model, money, users and access, background jobs,
  configuration. Money and access files are ranked by what they import and say,
  not only by their names.

A large monorepo is scanned whole: when it is past the live map's discovery budget the
scan says so (`discovery_limited`) and reads compose files and workflows directly, and
dozens of sibling packages (an app store, plugins) count as one entry.

`blueprint.json` is a draft with the systems already filled in and cited. Tell the
user the folder exists; they can delete it or add it to `.gitignore`.

### A repository of documents

A handbook, a policy set or a company wiki has no apps or code; the scan then finds
docs (in the order the README links them) and ranks documents for money and access
by what they say, and the draft cites each document's first line. Read them all.
In the blueprint, `systems` are the tools, vendors and providers the documents name
(`kind`: `internal tool`, `vendor: benefits`, ...), `stage` describes the company or
the documents as they show it, and `derived_from_source` means it follows from the
cited text. Journeys are people's paths through the company (joining, a work cycle,
leaving); money flows include pay, benefits and stipends as well as revenue.

## 2. Read

Read the docs first (the README usually says what the business is), then the
read_first files, then whatever they lead to. Reading is the work: the scan only
knows what files declare. Stop when you can answer, from the sources, the questions
below; when the sources do not answer one, that is an **unknown**, not a guess.

## 3. Fill in the blueprint

Replace the two `TODO`s (`purpose`, `stage`), correct the drafted systems where
reading showed more (each says what is not traced yet; delete, merge or rename any
that are wrong, such as a library that is really a worker process), and add the
sections below. Never copy a secret (a key, password or token from a config or `.env`
file) into the blueprint: the page quotes every cited line.
Every item has an `id` (letters, digits, `_.-`), a `name`, a `description`,
`evidence` (IDs from the `evidence` list), and a `basis`:

| basis | use when |
|---|---|
| `observed_in_source` | the cited lines say it directly |
| `derived_from_source` | it follows from the cited lines, with a step of reading you could explain |
| `operator_declared` | the user told you |
| `proposed` | your suggestion (risks, improvements) |
| `unknown` | the sources do not say; describe what is missing |

| section | what goes in it | fields beyond id, name, description, evidence, basis |
|---|---|---|
| `purpose`, `stage` | one sentence each: what it does and for whom; idea, prototype, live... as the sources show it | (plain strings) |
| `audiences` | who uses or pays (customers, admins, partners, ...) | |
| `value` | what each audience gets | |
| `money_flows` | who pays whom for what (plans, prices, fees, payouts); `[]` if nothing in the code or docs shows money | `state`: `described` (default), `deployed_claim`, `gated`, `planned`, `unknown` |
| `journeys` | how someone gets an outcome | `actor`, `outcome`, `steps`: 1 to 20, each with `id`, `name`, `description`, `evidence`, `basis`, `state` (as above) and optional `risk_ids` |
| `systems` | apps, services, datastores, outside services (the draft has these) | `kind`: free text (`application`, `datastore`, `external: payments`, ...) |
| `risks` | where it breaks | `severity` (required): `critical`, `high`, `medium`, `low`; `signal`: what would show it happening; `basis`: `derived_from_source` when you can follow the failure in the cited lines, `proposed` when it is a judgment |
| `unknowns` | the questions the sources leave open | `basis: unknown`; evidence may be empty |
| `improvements` | suggested work | `priority`: `P0` to `P3` (default `P2`); `addresses`: risk IDs |

One item of each kind (citation IDs are yours to choose; these two would be added to
`evidence` as `{"id": "plans", ...}` and `{"id": "webhook", ...}`):

```json
{"money_flows": [{"id": "pro-plan", "name": "Team pays the Pro plan", "description": "$29 a month per seat through Stripe Checkout",
                  "state": "described", "evidence": ["plans"], "basis": "observed_in_source"}],
 "journeys": [{"id": "upgrade", "name": "Upgrade to Pro", "actor": "Team admin", "outcome": "Pro features unlocked",
               "description": "From the pricing page to an active subscription", "evidence": ["plans"], "basis": "derived_from_source",
               "steps": [{"id": "pay", "name": "Pay in Checkout", "description": "Stripe Checkout session", "evidence": ["plans"],
                          "basis": "observed_in_source", "state": "described", "risk_ids": ["webhook-lost"]}]}],
 "risks": [{"id": "webhook-lost", "name": "A lost webhook leaves a paid team on Free", "description": "No retry or reconciliation",
            "severity": "high", "signal": "Paid Checkout sessions with no active subscription row", "evidence": ["webhook"],
            "basis": "proposed"}],
 "unknowns": [{"id": "real-prices", "name": "Real prices", "description": "Prices live in Stripe, not in the repository",
               "evidence": [], "basis": "unknown"}],
 "improvements": [{"id": "reconcile", "name": "Nightly reconcile with Stripe", "description": "Compare subscriptions both ways",
                   "priority": "P1", "addresses": ["webhook-lost"], "evidence": [], "basis": "proposed"}]}
```

Add a citation to `evidence` for each new source:
`{"id": "e20", "repo": "local", "path": "src/billing/plans.ts", "start": 12, "end": 30}`.
Paths are relative to the repository root, and the lines must exist. Cite the lines
that show the claim, not the file that merely mentions a word: a dependency in
`package.json` shows the dependency, not what it is used for.

Limits: at most 100 items per section, 200 citations, 16 citations per item.

## 4. Build

```bash
python "$SKILL_ROOT/scripts/overview.py" build --repo REPO \
  --blueprint REPO/.ontologyex/overview/blueprint.json --out REPO/.ontologyex/overview/business.html
```

It refuses a blueprint with a value that still starts with `TODO`, a citation to a
missing file or line, an unknown basis or risk ID, or a field the page needs that is
missing or wrong (a risk's `severity`, an improvement's `priority`). Each citation in
the page records the cited lines, their SHA-256, and the commit the checkout is on.
Fix what it names and build again.

The build checks that cited lines exist, not that they say what you claim. Its
output lists `check_citations`, the first and last line of each range (read from the
file); read them, and fix a range that is off at either end. `unused_citations` lists
evidence nothing refers to: cite it or remove it. The page keeps up to 2,000
characters of each cited range, so keep ranges to the lines that show the claim. `open` is the page's `file://` address: one file
that works offline. The cited text is inside the page, so it still reads when the
file is shared, but the links point at the local checkout. To share the page, add
`--github OWNER/NAME`: each citation then links to its lines on GitHub at the
checkout's commit.

## 5. Tell the user

Give the page path (they open it in a browser) and, in chat, five short parts: **what it is** (purpose, stage,
audiences), **how money moves**, **the main journeys**, **where it breaks** (the
risks), and **what is unknown**, each with a file reference or two. Say that the
claims are your reading of the sources, linked so they can check them, and that
nothing about production was observed.

Then offer the next step that fits what they want:

- **See the systems and their health live:** `operate.py up --repo REPO` (see
  [operating picture](operating-picture.md)); with feeds from `feed_setup.py` and
  traces from `otel_setup.py`, the map shows what is healthy and what calls what.
- **Change one workflow safely:** build a domain skill for it with the
  [guided workflow](guided-onboarding.md).
