# OntologyEX — Agent Ontology Kit

**Turn unfamiliar software into a source-linked domain skill for your agent.**

Code and documentation → business concepts and rules → portable agent context → reviewable updates.

[![Validator](https://github.com/New1Direction/OntologyEX/actions/workflows/validator.yml/badge.svg)](https://github.com/New1Direction/OntologyEX/actions/workflows/validator.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-13846f)](LICENSE)

OntologyEX is a portable extraction workflow plus a small local compiler. **Your agent does the modeling.** The compiler checks the model's structure and evidence references, then packages it into an inspectable skill with a bounded, offline checker. No new agent framework, hosted service, or model subscription is required by the tooling.

**[See the example report](https://new1direction.github.io/OntologyEX/docs/payments-demo.html) · [Run the demo](#try-it-without-an-api-key) · [Use your own repository](#use-your-own-repository) · [Contract format](ontology-extraction/references/domain-skill.md)**

## See the useful result first

The included fictional payments example prepares a domain skill for partial refunds:

| Situation | Offline diagnostic | Fixture ledger |
|---|---|---|
| Captured 10,000; already refunded 8,000; request 3,000 | `CHECKS_FAIL` — exceeds the remainder | Unchanged |
| Same payment; request 500 with supplied authorization | `CHECKS_PASS` — modeled conditions pass | Refunded total becomes 8,500 |
| Ask who can approve an undocumented exception | `NEEDS_REVIEW` — unknown policy | No transition |
| Policy changes to a 1,000-per-request cap | Old source snapshot is stale; new candidate identifies impacted tasks | New checks reject 2,000; 1,000 still passes |

Amounts are integer minor units in a single-currency teaching fixture. Nothing moves real money.

The demo is **pre-authored**, not evidence that the compiler autonomously extracted a domain. The generic extraction step uses your existing agent. Source hashes prove byte identity, not the truth of a business rule.

## Try it without an API key

From a fresh checkout (Python 3.11 or later):

```bash
git clone https://github.com/New1Direction/OntologyEX.git
cd OntologyEX
python3 -m venv .venv
source .venv/bin/activate  # PowerShell: .venv\Scripts\Activate.ps1
python -m pip install 'PyYAML==6.0.3'
python examples/payments-domain/demo.py
```

Open `build/payments-demo/v2/payments-domain/report.html` in your browser. It is a standalone local page: no JavaScript, accounts, analytics, or remote assets.

Expected result: `DEMO_PASSED`, **26/26 deterministic fixture checks** across two policy versions, stale-source rejection, and a change-impact report. These are not LLM performance results. The demo explicitly leaves `agent_comparison: NOT_RUN`, `human_review: PENDING`, and `promotion: NOT_PERFORMED`.

Outputs:

```text
build/payments-demo/
  v1/payments-domain/       first immutable candidate skill
  v2/payments-domain/       candidate after the policy change
    SKILL.md               portable agent instructions
    model.json             four layers, rules, evidence, and input schemas
    report.html            inspect concepts, actions, mappings, and exact evidence
    references/domain.md   focused domain context
    references/sources/    selected source snapshots only
    references/review.md   acceptance checklist; pending human review
    scripts/check.py       standalone standard-library checker
    manifest.json          content fingerprints
  v1/evaluation.json       observed fixture transitions versus authored expectations
  v2/evaluation.json
  v1/benchmark/            raw / Markdown / skill evaluation contexts; NOT_RUN
  v2/benchmark/
  impact.json              changed sources and contract items; tasks to rerun
  summary.json
```

The default demo directory is never overwritten. To rerun, choose a fresh output:

```bash
python examples/payments-domain/demo.py --out build/payments-demo-next
```

## Use your own repository

**1. Select the smallest useful source set.** Explicit paths only; no hidden full-repository crawl.

```bash
python ontology-extraction/scripts/domain_skill.py prepare \
  --repo /path/to/your-project \
  --include src/payments.py \
  --include docs/refunds.md \
  --name your-domain \
  --goal 'Prepare my agent to implement partial refunds correctly.' \
  --out ./workspaces/your-domain-v1
```

Replace the two example paths with real files in your project. Preparation freezes their bytes, creates the existing four-layer starter files, and writes `EXTRACT.md` plus a contract template. **It does not claim extraction is complete.**

**2. Give your existing agent this task:**

```text
Read workspaces/your-domain-v1/EXTRACT.md and follow
ontology-extraction/references/domain-skill.md.

Use only the selected source snapshots to fill the four YAML layers
and 60-contract.json. Preserve the source snapshot and inventory.
Link modeled claims to exact evidence. Mark unsupported conditions
as inferred or unknown, and record disagreements as conflicts.

Do not execute source files, call paid APIs, approve the candidate,
or claim that a successful build proves semantic correctness.
```

**3. Compile and inspect a new candidate.**

```bash
python ontology-extraction/scripts/domain_skill.py build \
  workspaces/your-domain-v1 \
  --repo /path/to/your-project \
  --out ./build/v1/your-domain
```

The output folder's name must match the skill name. `--repo` checks the selected current files against the frozen snapshot. Omitting it is an explicit snapshot-only build, reported as `live_sources_checked: false`.

After review, point your agent at the generated `SKILL.md`, or copy the **whole directory** into its supported skills directory. The generated checker also runs without PyYAML or this repository. Agent-specific installer/auto-discovery behavior is not claimed as tested.

## Checks are not permissions

From inside a generated skill:

```bash
python scripts/check.py --task IssueRefund --input /path/to/inputs.json
```

| Exit | Result | Meaning |
|---|---|---|
| 0 | `CHECKS_PASS` | The modeled checks passed for supplied values. **Not permission to execute.** |
| 2 | `CHECKS_FAIL` | At least one modeled condition failed. |
| 3 | `NEEDS_REVIEW` | An unknown, conflict, inferred rule, or unsupported condition remains. |
| 1 | `INVALID_INPUT` | Malformed inputs, unsupported checks, or bundle integrity failure. |

The checker uses a small allowlist of typed comparisons and two-field integer subtraction. It has no `eval`, code generation, network, or tool execution. Authorization flags are **supplied values**, not authenticated facts. The consuming application still owns authorization, current state, atomicity, concurrency, idempotency, and transaction safety.

Before relying on a previously generated skill, check the original sources:

```bash
python ontology-extraction/scripts/domain_skill.py freshness \
  workspaces/your-domain-v1 --repo /path/to/your-project
```

## Preserve corrections, not unverified guesses

Prepare a new workspace when requirements change, have your agent propose the revised model, and build a new candidate. Compare it with the prior version:

```bash
python ontology-extraction/scripts/domain_skill.py compare \
  build/v1/your-domain build/v2/your-domain
```

The report lists changed sources and rules, and conservatively identifies tasks to rerun. It is not a complete semantic impact analysis. **No command promotes, overwrites, or deploys an accepted skill.** Independent tests and human review stay outside the proposing agent's authority.

## What is actually evaluated?

The test suite checks malformed input, provenance mismatches, stale sources, rule references, typed diagnostics, unknowns/conflicts, immutable bundles, portable execution, and the example's ledger outcomes.

The demo also exports three contexts with identical raw sources and task inputs: **raw documentation**, **raw documentation plus a concise Markdown guide**, and **raw documentation plus the generated skill**. No agent runs are fabricated. [The evaluation guide](examples/payments-domain/README.md) explains fresh sessions, comparable budgets, outcome scoring, and limitations of the small public case set.

## Four layers, still underneath

| Layer | Purpose |
|---|---|
| L0 — Upper | Select established general categories. |
| L1 — Domain | Model the business concepts and relationships. |
| L2 — Task | Model actions, participants, inputs, outputs, and conditions. |
| L3 — Application | Bind the model to this system's code, types, tools, and other artifacts. |

Every application concept maps to a domain class, every domain class anchors upward, and tasks reference domain concepts. The new compiler reuses the existing validator; it does not replace the method with a second ontology.

The original [portable workflow](AGENT_SKILL.md), [full method](ontology-extraction/SKILL.md), [plain-English explainer](explain.html), and [five worked modeling examples](examples/) remain available. The Stripe–Adyen example matches 8 of 9 Stripe domain concepts using IDs and synonyms. That is a scoped modeling comparison, not proof of effortless vendor switching or better agents.

Legacy tools still work:

```bash
python ontology-extraction/scripts/scaffold.py init --name my-target --out ./my-target-ontology
python ontology-extraction/scripts/scaffold.py validate ./my-target-ontology
python ontology-extraction/scripts/scaffold.py mappings ./my-target-ontology
```

`init` writes six starter files; `mappings` produces the seventh. Placeholders are not a completed model. Legacy validation checks structure, not source truth, saved mapping consistency, or semantic completeness. The **new compiled-skill profile** adds exact local evidence verification, contract checks, and saved mapping consistency when a mapping table is supplied.

## Development and contributions

```bash
python -m unittest discover -s tests -v
python examples/payments-domain/demo.py --out build/ci-demo
```

GitHub Actions runs regression tests, all five legacy examples, and the new domain-skill demo on Python 3.11 and 3.13. No paid services or model calls are required.

The most useful contributions are a real scoped example, an unsupported business condition, a failing provenance case, or a reproducible agent comparison. Include the sources you are allowed to share and the limits of what the example establishes. Do not upload private source snapshots or credentials.

Useful for your agent stack? Star the repository to follow new examples and releases.

[MIT](LICENSE) · Independent project. Not affiliated with Palantir or any payment provider.
