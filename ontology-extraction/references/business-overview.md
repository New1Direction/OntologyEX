# Business overview: a whole repository, explained

Use this when the user wants to understand their project, product, business, or
company as a whole: what it is, who it serves, how money moves, how people use it,
where it breaks, and what nobody wrote down. The result is one offline page (the
Business X-Ray) where every claim links to the lines it rests on, plus a short
summary in chat. Nothing in their repository is run or changed; the scan only reads.

## 1. Scan

```bash
python "$SKILL_ROOT/scripts/overview.py" scan --repo REPO --out REPO/.ontologyex/overview
```

`scan.json` lists, each with the file (and line) that shows it:

- **apps**: folder, language, frameworks (Next.js, Django, Express, FastAPI, ...);
- **services** and **datastores** compose declares (Postgres, Redis, Kafka, ...);
- **integrations**: outside services the code depends on, grouped as payments,
  auth, email and messaging, AI, data and queues, cloud, analytics and monitoring;
- **docs** and **read_first**: entry points, routes and APIs, data model, money,
  users and access. These are where the business lives in code.

`blueprint.json` is a draft with the systems already filled in and cited. Tell the
user the folder exists; they can delete it or add it to `.gitignore`.

## 2. Read

Read the docs first (the README usually says what the business is), then the
read_first files, then whatever they lead to. Reading is the work: the scan only
knows what files declare. Stop when you can answer, from the sources, the questions
below; when the sources do not answer one, that is an **unknown**, not a guess.

## 3. Fill in the blueprint

Replace every `TODO` and add the sections below. Every item has an `id` (letters,
digits, `_.-`), a `name`, a `description`, `evidence` (IDs from the `evidence`
list), and a `basis`:

| basis | use when |
|---|---|
| `observed_in_source` | the cited lines say it directly |
| `derived_from_source` | it follows from the cited lines, with a step of reading you could explain |
| `operator_declared` | the user told you |
| `proposed` | your suggestion (risks, improvements) |
| `unknown` | the sources do not say; describe what is missing |

| section | what goes in it |
|---|---|
| `purpose`, `stage` | one sentence each: what it does and for whom; idea, prototype, live... as the sources show it |
| `audiences` | who uses or pays (customers, admins, partners, ...) |
| `value` | what each audience gets |
| `money_flows` | who pays whom for what (plans, prices, fees, payouts); `[]` if nothing in the code or docs shows money |
| `journeys` | `actor`, `outcome`, and 1 to 20 `steps` (`name`, `description`, `evidence`, `state`: `described`, `deployed_claim`, `gated`, `planned`, `unknown`; optional `risk_ids`) |
| `systems` | apps, services, datastores, outside services (the draft has these) |
| `risks` | where it breaks: `basis: proposed`, a `severity` (`critical`, `high`, `medium`, `low`), and what would show it |
| `unknowns` | the questions the sources leave open |
| `improvements` | suggested work; `addresses` lists risk IDs |

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

It refuses a blueprint with a `TODO` left, a citation to a missing file or line, an
unknown basis or risk ID. Each citation in the page records the cited lines, their
SHA-256, and the commit the checkout is on. Fix what it names and build again.

## 5. Tell the user

Give the page path and, in chat, five short parts: **what it is** (purpose, stage,
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
