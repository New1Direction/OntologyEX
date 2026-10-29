# OntologyEX — Agent Ontology Kit

**Give your agent a repository; get the business behind it, linked to source.**

What it is, who it serves, how money moves, the main journeys, where it breaks, and
what nobody wrote down, on one page. Then a live map of the systems, and portable
domain skills with exact evidence before your agent changes a workflow.

[![Validator](https://github.com/New1Direction/OntologyEX/actions/workflows/validator.yml/badge.svg)](https://github.com/New1Direction/OntologyEX/actions/workflows/validator.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-13846f)](LICENSE)

**[Example overview](https://new1direction.github.io/OntologyEX/docs/examples/nextjs-subscription-payments.html) · [Guided onboarding](ontology-extraction/references/guided-onboarding.md) · [Real-source case](examples/cachetools-domain/README.md) · [Payments report](https://new1direction.github.io/OntologyEX/docs/payments-demo.html) · [Contract](ontology-extraction/references/domain-skill.md)**

## Give this repository to your agent

In Claude Code, Codex, Cursor, or any coding agent, open your project and say:

> Use https://github.com/New1Direction/OntologyEX to give me an overview of this repository and the business behind it.

Your agent clones the kit, scans your repository (reading only), reads what matters,
and writes **one page**: what the business is, who it serves, how money moves, the
main journeys, where it breaks, and what nobody wrote down, with every claim linked
to the lines it rests on. Then it can show your systems live, trace the calls between
your services, or build a domain skill before you change a workflow.
Instructions for the agent: [AGENTS.md](AGENTS.md).

[![An agent scans a repository, reads what matters, and builds the page; then the page: journeys, money, where it breaks, each with the cited lines](docs/media/ontologyex-demo.gif)](https://new1direction.github.io/OntologyEX/docs/examples/nextjs-subscription-payments.html)

**Examples**, each written by an agent from a public repository, every claim linked to its
lines at a pinned commit (the risks are the agent's reading, not observed incidents):

- [Vercel's Next.js subscription starter](https://new1direction.github.io/OntologyEX/docs/examples/nextjs-subscription-payments.html): a SaaS skeleton with Stripe and
  Supabase ([blueprint](examples/overview/nextjs-subscription-payments.blueprint.json))
- [Saleor](https://new1direction.github.io/OntologyEX/docs/examples/saleor.html): an open-source commerce platform, payments through apps over
  webhooks ([blueprint](examples/overview/saleor.blueprint.json))

It has been tried by fresh agents on Go, Next.js, Remix, Django, Rails, FastAPI,
Laravel and Spring apps, a 7,700-file monorepo, and a company handbook with no code.

## Onboard your repository from one request

Python 3.11+ is needed for the local tools. From an OntologyEX checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate  # PowerShell: .venv\Scripts\Activate.ps1
python -m pip install 'PyYAML==6.0.3'
python ontology-extraction/scripts/install.py --project /path/to/your-project
```

The installer copies the complete package into the project's
`.claude/skills/ontology-extraction` directory. It refuses overwrites, changes no global
settings, and pre-approves no tools. Make the Python environment available to your coding agent.

Then in Claude Code:

```text
/ontology-extraction Onboard the reservation and cancellation workflow in this repository.
Propose the smallest useful source set. After I acknowledge it, build a reusable domain
skill for modifying that workflow. Show the rules, implementation locations, evidence,
and anything the sources leave unclear.
```

Replace the example workflow with your own. The agent coordinates source selection,
authoring, evidence collection, bounded compilation, and review. It does **not** automatically
execute your project or deploy the generated skill.

**Compatibility:** the Claude Code project layout and installed-script workflow are smoke-tested.
Native host activation and fresh-model quality comparisons remain separate, unrun checks.
Other runners can read `ontology-extraction/SKILL.md` directly; no agent framework is required.

## What is different from a summary?

| Output | Use |
|---|---|
| Business concepts and actions | Explain the system in domain terms, not just file names. |
| Source-linked rules and implementation bindings | Locate the code and inspect the evidence for a claim. |
| Explicit unknowns and conflicts | Expose unanswered questions instead of inventing permission. |
| Portable domain skill and task handoff | Carry the reviewed context into a separate agent session. |
| Source freshness and version comparison | Identify drift and the tasks that need review again. |

**Your agent does the interpretation.** The tools handle exact copying, fingerprints,
structural checks, and packaging. Source hashes establish byte identity, not semantic truth.

## Try a real-source case without an API key

```bash
python -m pip install -r examples/cachetools-domain/requirements-case.txt
python examples/cachetools-domain/case.py
```

The case verifies installed `cachetools` source bytes against a pinned upstream commit,
uses the evidence helper to compile a domain skill, exports a task-focused handoff, and runs
**18 behavioral acceptance checks** against a developer-written batch-lookup adapter.

It covers expiration at the exact boundary, fractional and datetime clocks, falsy values,
ordered misses, recency, no TTL refresh, and error propagation. These behaviors are exercised
against the actual pinned library, not inferred from a successful ontology check.

Open the report under:

```text
build/cachetools-case/session/attempts/01/candidate/cachetools-domain/report.html
```

The model and adapter are **developer-authored replay artifacts**, not proof of automatic
extraction or superior agent performance. Raw-source, Markdown, and domain-skill trial inputs
are exported separately; fresh-agent comparisons are explicitly `NOT_RUN`.
See the [case protocol and limitations](examples/cachetools-domain/README.md).

The earlier fictional payments/update demo remains available:

```bash
python examples/payments-domain/demo.py
```

Expected result: 26/26 deterministic fixture checks across two policy versions, detection of
stale sources, and a reviewable change report. No real money moves. Use a fresh `--out` directory
to rerun either example; existing outputs are never overwritten.

## Run a genuine native-agent pilot

The operator workflow is now command-driven. From this checkout:

```bash
python -m pip install -r examples/cachetools-domain/requirements-case.txt
python examples/cachetools-domain/pilot.py prepare --out "$HOME/ontologyex-native-pilot-v2"
python examples/cachetools-domain/pilot.py status "$HOME/ontologyex-native-pilot-v2"
```

Open the generated `project/` in a fresh, signed-in Claude Code session and paste
`operator/ONBOARDING-PROMPT.txt`. The project contains only pinned sources and installed
extraction tooling, not a prebuilt model, reference solution, or grading tests. The
scripts never start a model or copy credentials. A detected host binary is not an
authentication or activation check.

The [native pilot runbook](docs/native-pilot.md) covers private run capture, a separate
implementation project after review, and explicit grading of the submitted adapter
using the existing acceptance cases. The grader requires reviewed code, an acknowledgment
flag, and a disposable environment; its bounded child process is **not an OS sandbox**.
Native-agent comparisons remain `NOT_RUN` until actual reviewed results exist.

## Bounded authoring, not endless retries

The guided driver records one initial submission and at most two correction attempts.
Each attempt freezes the submitted files and records diagnostics. Process interruptions
consume a slot; recovery requires acknowledgment that the earlier process is not running.

```bash
python ontology-extraction/scripts/onboard.py status /path/to/session
python ontology-extraction/scripts/onboard.py attempt /path/to/session
python ontology-extraction/scripts/onboard.py handoff /path/to/session \
  --task TASK_ID --out /path/to/new-handoff
```

A candidate can be structurally valid and still need semantic review. Unknowns, conflicts,
inferred rules, and unsupported conditions must not be removed simply to obtain a pass.
The limit bounds submissions to the driver, not arbitrary host-agent tokens or off-tool actions.
No command approves, promotes, or deploys a candidate.

## Evidence without manually copying hashes

```bash
python ontology-extraction/scripts/evidence.py add /path/to/session/workspace \
  --path docs/policy.md --start 12 --end 16 --id E-policy --kind requirement
```

The helper copies an exact source span and digest into the authoring contract. `show` displays
numbered source lines; `record` emits JSON without changing the contract. Existing evidence IDs
cannot be silently repointed. Someone must still review whether the span supports the claim.

## Business operating picture

```bash
python examples/operating-picture/demo.py --serve   # then open http://127.0.0.1:8765/business
```

One screen joins domain skills to the system map: which business actions are at
risk and why, which objects depend on a failing system, open review questions,
and how far each rule's evidence was checked. Health is the source-reported status
of operator-linked systems, not proof that an action works. The demo data is
synthetic. See [operating picture](docs/operating-picture.md).

For your own project, the installed skill includes the business screen. After onboarding
a workflow, ask your agent:

```text
/ontology-extraction Show my business screen for this repository. Suggest which systems
run each code binding and let me confirm the links.
```

For the wider business story (users, journeys, money flows, and where they can break),
the **Business X-Ray** preview compiles authored, source-linked blueprints into one offline
page. See [Business X-Ray](business/README.md).

Every business object, action and system also has an **entity page** (`/entity`): why
it exists, what a person confirmed and what is only inferred, what relies on it, and
the files behind each relationship. Agents get the same model through an MCP server
(`agent_tools.py`) whose tools read, or propose a link for a person to decide, under a
per-agent policy with every call audited. See [entity context](ontology-extraction/references/entity-context.md)
and [what OntologyEX took from Macro](docs/macro-comparison.md).

Live health comes from real telemetry: `feeds.py` receives OpenTelemetry traces and
polls Prometheus and HTTP health checks (`python examples/operating-picture/live.py`).
For your own repository, `feed_setup.py propose` lists the feeds its compose files
declare, with the line for each; you accept the ones to watch, and `operate.py up`
starts the feeds and the business screen together. No traces yet? `otel_setup.py run
--service web -- npm start` starts a Node or Python service with OpenTelemetry and no
code change. When services send traces, calls
between them that the repository does not declare show up as suggested dependencies;
a confirmed one carries possible impact on the map like a declared one. See
[live feeds](docs/live-feeds.md).

The same jobs also run on an optional Rust engine: live feeds with about 18× the
trace throughput in a fifth of the memory, and repository discovery about 15× faster,
with answers CI checks against Python on every change. Install prebuilt binaries
with `python ontology-extraction/scripts/fetch_engine.py`. See [Rust engine](docs/rust-engine.md).

Optional decision models (Jev or Clef) speed up the proposal steps: source selection,
link routing, review triage, conflict candidates, wording checks, and an **Ask** box that
routes questions to the right view. They never make the proven decisions. See
[decision models](docs/decision-models.md).

Links are suggested from names and paths and applied only when a person confirms
them. The health rollup, grade ladder, and link decisions are implemented in Python
and Rust, proven in Lean and Bend, and cross-checked in CI. See
[business verification](docs/business-verification.md).

## Grades, wording, and queries

- **Evidence grades:** `evidence_grade.py` reports an ordinal G0–G4 grade per rule from
  recorded review and behavior-check events. No numbers, no approvals; nothing reaches G4
  until a tool observes fresh-agent use. See [evidence grades](ontology-extraction/references/evidence-grades.md).
- **Rule wording:** `statement_lint.py` gives advisory, STE-style warnings for statements that
  can be read two ways. See [rule wording](ontology-extraction/references/rule-wording.md).
- **SPARQL:** `rdf_export.py` exports declarations, evidence, rules, unknowns, and conflicts
  as N-Triples; `examples/sparql/` has queries tested with the optional sparq engine.
  See [RDF interchange](docs/rdf-interop.md).

## Existing compiler and four-layer method

The original method remains intact in [METHOD.md](ontology-extraction/METHOD.md):
upper anchors → domain nouns/relations → task actions/conditions → application bindings.
Use the [portable entry point](AGENT_SKILL.md) for either guided skills or ontology-only outputs.

`domain_skill.py` still provides `prepare`, `build`, `verify`, `freshness`, and `compare`.
The [contract reference](ontology-extraction/references/domain-skill.md) documents direct use.
The five original modeling examples remain under `examples/eval-*`; they are not agent benchmarks.

```bash
python ontology-extraction/scripts/scaffold.py validate examples/eval-1-stripe/stripe-support-agent-ontology
python ontology-extraction/scripts/domain_skill.py freshness WORKSPACE --repo ORIGINAL_REPO
python ontology-extraction/scripts/domain_skill.py compare OLD_SKILL NEW_SKILL
```

## Checks are not permissions

Generated checkers use bounded typed comparisons, not arbitrary code or model calls.
`CHECKS_PASS` only describes modeled conditions on supplied inputs. Unknown/manual conditions
produce `NEEDS_REVIEW`; failed conditions produce `CHECKS_FAIL`. Every result leaves
`execution_authorized: false`. The consuming application owns authenticated authorization,
current state, concurrency, atomicity, idempotency, and integration safety.

Source text is untrusted data. Secret-name filtering is not an exhaustive secret scanner.
Fingerprints are not signatures; session files and trial folders are not an OS security sandbox.
Keep private snapshots out of commits and use proper isolation for untrusted execution.

## Experimental: traces → ontology candidates

OntologyEX now includes a bounded, offline prototype to extract **observed** object types,
actions, tool relationships and evidence lineages from normalized agent spans. These are
**unreviewed candidates**, not independently confirmed policy or operational authorization.

```bash
python ontology-extraction/scripts/trace_extract.py \
  examples/trace-to-ontology/demo.jsonl --out build/trace-ontology-draft
```

See [the trace format, privacy boundary and roadmap](ontology-extraction/references/trace-to-ontology.md).
Raw prompt/tool inputs and outputs are forbidden; neither agents nor source files are executed.
The prototype does not interpret natural-language transcripts automatically or grant execution rights.

## Contributing and verification

```bash
python -m pip install -r examples/cachetools-domain/requirements-case.txt
python -m unittest discover -s tests -v
```

CI runs the suite on Python 3.11 and 3.13, validates the original examples, exercises the payments
update demo and report reproducibility, and runs the pinned-source onboarding case. The installed
package is exercised from an unrelated working directory. No API keys or model calls are needed.
A useful contribution is a scoped real-source example, a failing behavioral case, or a documented
fresh-agent run with comparable baselines. Do not treat compiler success as proof of model quality.

Useful for your agent stack? Star the repo to follow worked examples and releases.

## License

[MIT](LICENSE) © 2026 New1Direction. The optional cachetools case preserves upstream MIT attribution
and verifies the upstream files against the pinned commit listed in its provenance manifest.
