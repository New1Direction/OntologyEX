# Decision models: Jev and Clef

[Jev](https://vercel.com/ai-gateway/models/jev) (TypeSafe AI) and
[Clef / Clef-flash](https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/)
(Cloudflare, open weights under Apache 2.0) are **decision models**: you send a
`state` and typed questions (`noul` yes/no, `choice`, `score`) and get a probability
for every allowed answer instead of text. Both use the System One API, so one client
(`ontology-extraction/scripts/decide.py`) drives either.

OntologyEX uses them only where it **proposes** something. A model answer never
confirms a link, records a review, changes health, edits a contract, or authorizes
anything. The proven paths (health rollup, multi-source combining, evidence grades,
and link decisions, in Python, Rust, Lean and Bend) never call a model.

## Configure (environment variables only)

| Provider | Settings |
|---|---|
| Cloudflare Workers AI (Clef, Clef-flash) | `ONTOLOGYEX_DECIDE_PROVIDER=cloudflare`, `ONTOLOGYEX_DECIDE_MODEL=clef-flash` (or `clef`), `CLOUDFLARE_ACCOUNT_ID`, `ONTOLOGYEX_DECIDE_TOKEN` (API token) |
| TypeSafe (Jev) | `ONTOLOGYEX_DECIDE_PROVIDER=typesafe`, `ONTOLOGYEX_DECIDE_MODEL=jev-latest`, `ONTOLOGYEX_DECIDE_TOKEN` |
| Any System One endpoint (self-hosted Clef, a gateway) | `ONTOLOGYEX_DECIDE_PROVIDER=system-one`, `ONTOLOGYEX_DECIDE_URL` (https, or http on loopback), optional `ONTOLOGYEX_DECIDE_TOKEN` |

Answers are cached by a digest of provider, model, state, and questions in
`ONTOLOGYEX_DECIDE_CACHE` (default `.ontologyex-decisions/`), so repeating a question
(for example the control room's 5-second refresh) costs nothing. Requests above 64
questions are split into batches. Every answer is validated: a choice must be one of
the options and every probability must be between 0 and 1, or the call fails.

**Data leaves your machine.** Each integration below says exactly what it sends.
Only turn it on (`--model-assist` or `--decision-model`) for sources you may share
with that provider, or point `system-one` at a model you host.

## Where it is used

| Step | Command | Question | What is sent | What you get |
|---|---|---|---|---|
| Propose sources | `propose_sources.py --repo R --goal G --model-assist` | `score`: how relevant is this file to the goal (4 levels) | Goal, file path, first 60 lines (≤3,000 chars) of each shortlisted file; never `.env`, keys, or files with token-like text | Ranked files, a proposed set, and the `onboard.py start` command. Without a model it ranks by word overlap. Replaces a separate jevgrep install. |
| Rule wording | `statement_lint.py TARGET --model-assist` | `noul`: can statement S be read in more than one way | Rule, unknown, and conflict statements (one batched request) | `MODEL_AMBIGUITY` warnings. Advisory, never blocking. |
| Conflict candidates | `conflict_scan.py TARGET` | `noul`: do spans A and B contradict each other about task T | Per task: its statements and cited quotes | Likely contradicting pairs with a draft conflict entry. The contract is never changed. |
| Auto-linking | `link_suggest.py suggest ... --model-assist`, or the control room with `--decision-model` | `choice`: which listed service, module, route, or table runs this code binding (or none) | The binding's id, kind, business concept, tasks, system, and cited source quote; candidate names, kinds, and manifest paths | A `MODEL_ROUTED` signal on a suggestion (MODERATE at ≥0.8, WEAK at ≥0.5). Still confirm-only. |
| Review triage | `evidence_grade.py triage SKILL`, or the control room with `--decision-model` | `noul`: does evidence E support the rule as written | Each rule's statement and its cited quotes | Spans ordered lowest support first; `CHECK CITATION` items in the business page. Never recorded as a review; grades do not change. |
| Ask (generative UI) | Control room with `--decision-model`: the **Ask** box on `/business` | `choice` ×3: which view, which item, which problem type | The request text, view and filter descriptions, item names and health | The page switches view, selects the item, and filters, using real data. Without a model, keywords route. |

The control room makes no outbound calls unless `--decision-model` is set.

## Speed and cost (published figures, not measured here)

Cloudflare reports median latency of 38.8 ms for Clef-flash, 209.3 ms for Clef, and
524.1 ms for Jev, and Clef pricing of $0.24 per million input tokens with free output.
Jev is listed at $0.042 per million input tokens on Vercel AI Gateway. Independent
accuracy reports differ by workload, so try both on your own decisions.

## Tests

`tests/test_decide.py` runs the client and every integration against a local System
One endpoint (`tests/fake_system_one.py`). That endpoint is **not a model**: it checks
requests and returns controlled answers. No call to a real provider was made in
development or CI; the first live run needs your key.
