# 🧭 Agent Ontology Kit

**Make AI agents understand a business before they act.**

![License: MIT](https://img.shields.io/badge/License-MIT-13846f.svg)
![Type: AI agent skill](https://img.shields.io/badge/type-AI%20agent%20skill-7654b5.svg)
![Validator: Python 3](https://img.shields.io/badge/validator-Python%203%20%C2%B7%20pyyaml-256eb2.svg)
![Build: zero](https://img.shields.io/badge/build-none%20·%20portable%20markdown-c28719.svg)

Agent Ontology Kit is a **portable skill for AI agents**. It reads a company, product, API,
market, or codebase and writes a clean, structured **map of how that world works** — the things
that exist, the actions you can take, and the rules between them — in a form an agent can use
*before* it acts.

No framework. No build step. It's markdown you hand to any capable agent (Claude Code, Codex,
Cursor, custom runners), plus a tiny Python validator.

**▶ [Live explainer — in plain English](explain.html) · [Examples](#-worked-examples) · [Quick start](#-quick-start)**

---

## 🤔 The problem

An agent can know how to call an API without knowing the business rules that govern it.
For example, it might attempt to refund an unpaid order when that restriction is missing
from its context. The application must still enforce the restriction at runtime.

This kit makes that understanding **explicit, checkable, and reusable.**

## 🧱 What it builds: four layers

Imagine describing a **coffee shop** to a robot — from "true of anything" down to "this exact shop."

| Layer | Plain English | Coffee-shop example |
|---|---|---|
| **L0 · Upper** | universal kinds of things | a thing, a person, an amount |
| **L1 · Domain** | the nouns of the trade | Order, Drink, Barista |
| **L2 · Task** | the actions + their rules | TakeOrder, Refund *("can't refund what wasn't paid")* |
| **L3 · Application** | this exact system's files | the `orders` table, the "new order" button |

The discipline that makes it worth doing: **every L3 maps to an L1, every L1 anchors to an L0,
every L2 names the L1 nouns it touches.** That cross-layer mapping table is the deliverable.

## 🚀 Quick start

Give any capable agent this:

```text
Use AGENT_SKILL.md as your workflow.
Target:   <company, API, product, market, or codebase>
Consumer: <MCP agent tools | RAG | knowledge graph | DB/API schema | docs>
Boundary: <what is in and out of scope>
Deliver:  the YAML layers, the mapping table, validation notes, and the consumer binding.
```

That's it. The agent scopes the target, mines sources, builds the four layers, validates them,
and emits the output your consumer needs.

## 🛠️ Run the tooling locally (optional)

From a checkout of this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install pyyaml

# Try an included example first; no API keys or transactions.
python ontology-extraction/scripts/scaffold.py validate examples/eval-1-stripe/stripe-support-agent-ontology

# Create your own workspace, then fill in its layers.
python ontology-extraction/scripts/scaffold.py init --name my-target --out ./my-target-ontology
python ontology-extraction/scripts/scaffold.py validate ./my-target-ontology
python ontology-extraction/scripts/scaffold.py mappings ./my-target-ontology
```

The only runtime dependency is `pyyaml`. The Markdown skill itself needs no Python installation.
`init` writes six starter files; `mappings` generates the seventh, `50-mappings.yaml`.
A scaffold contains placeholders, not a finished ontology.

### What validation does — and does not — guarantee

`validate` requires an existing directory and all four non-empty YAML layer documents:
`10-upper.yaml`, `20-domain.yaml`, `30-task.yaml`, and `40-application.yaml`.
It rejects malformed YAML, invalid collection/identifier/reference types, duplicate IDs,
and broken cross-layer references covered by the validator. Upper anchors and domain classes
must be non-empty lists. Task and application lists may explicitly be `[]` while scoping a model;
optional collections must be lists when present (use `[]`, not `null`).

Exit code **0** means these structural checks passed; warnings still require review.
Exit code **1** means invalid input or structural errors. File-specific diagnostics replace
tracebacks for unreadable files, invalid YAML, and the checked shape errors.

The validator does **not** verify source truth, answer competency questions, prove business-rule
correctness, check the completeness of scope/README documents, or check a saved mapping table
against the layers. Regenerate the mapping table after changing layers and review it.
MCP example preconditions are **descriptive instructions, not executable guards**.
Authorization, live-state checks, human approvals, and runtime enforcement belong in the consuming
application. Structural validation is not a security boundary or a guarantee of safe agent behavior.

## 📦 What you get

A 7-file workspace plus a consumer-specific binding:

```
my-target-ontology/
  00-scope.md          target, consumer, boundary, competency questions
  10-upper.yaml        L0 — chosen universal anchors (selected, never invented)
  20-domain.yaml       L1 — the domain nouns + relations
  30-task.yaml         L2 — the actions, with inputs/outputs/preconditions/effects
  40-application.yaml  L3 — the concrete system artifacts
  50-mappings.yaml     the app → domain → upper crosswalk
  README.md
```

…then one binding: **MCP tool schemas**, an RDF/Turtle knowledge graph, TypeScript/Pydantic types,
RAG metadata, or a Mermaid diagram.

## 🧪 Worked examples

Five worked modeling examples in [`examples/`](examples/). CI runs the structural validator on each;
these examples are not agent-performance benchmarks:

| Eval | Target | Highlight |
|---|---|---|
| `eval-1-stripe` | Stripe (research) | API → MCP tool descriptions with explicit preconditions |
| `eval-2-realworld` | RealWorld app (retrofit) | map an existing codebase to its domain |
| `eval-3-prediction-markets` | a market (research) | a sparse / emerging domain |
| `eval-4-self` | **the kit itself** | it described its own code — **0 errors** |
| `eval-5-adyen` | Adyen (research) | **competitor swap vs Stripe: 8/9 domain concepts matched** ⭐ |

The plain-English walkthrough of the last two is in **[explain.html](explain.html)**.

## 💡 Why four layers (the payoff)

The domain layer aims to describe the *trade*, not one vendor. In the supplied
**Stripe–Adyen comparison**, **8 of 9 Stripe domain concepts map across** using canonical IDs
and synonyms. Task and application layers differ with the APIs
(see [`examples/eval-5-adyen/comparison-vs-stripe.md`](examples/eval-5-adyen/comparison-vs-stripe.md)).
This illustrates reuse in these scoped examples; it does not prove effortless provider switching
or improved agent performance. Vendor-specific workflows and bindings still need review and tests.

## 🔌 Use it as a skill

- **Claude Code / Cursor / Codex:** point the agent at `AGENT_SKILL.md`, or drop the
  `ontology-extraction/` folder into your skills directory (it has a ready `SKILL.md` with trigger
  frontmatter).
- **Any runner:** the workflow is plain markdown — no runtime lock-in.

## 🌐 Publish the site (GitHub Pages, zero build)

`index.html` (landing) and `explain.html` (explainer) are self-contained static HTML.
**Settings → Pages → Deploy from a branch → `main` / `(root)`.**
Your live site: `https://<your-username>.github.io/<repo>/`.

## 🗺️ How it works

Work **middle-out**: scope → competency questions → mine sources → anchor L0 → build L1 → build L2
→ project L3 → validate → emit the binding. Full method in
[`ontology-extraction/SKILL.md`](ontology-extraction/SKILL.md); evidence rules, reuse catalog, and
production design principles in [`ontology-extraction/references/`](ontology-extraction/references/).

The design bias is deliberately domain-driven: model how the real business operates, not a 1:1 copy
of source tables or departmental systems. The validator provides heuristic warnings for wide
application schemas, technical-looking fields, possible department/system silos, action sprawl,
vague names, and orphan classes. These are review prompts, not proofs of modeling defects.
Deep-hierarchy review remains part of the manual workflow.

## 📁 Repo layout

```
.
├─ README.md
├─ AGENT_SKILL.md            ← the portable workflow — hand this to any agent
├─ index.html · explain.html ← zero-build site (deploy to GitHub Pages)
├─ ontology-extraction/
│  ├─ SKILL.md
│  ├─ scripts/scaffold.py    ← init · validate · mappings
│  └─ references/            ← source-mining · reuse-catalog · design-principles · output-formats
└─ examples/                 ← 5 worked, validated evals
```

## 🤝 Contributing

PRs welcome — new worked evals (a real company/API/codebase + its validated ontology) are the most
valuable contribution. From the repository root, run:

```bash
python -m unittest discover -s tests -v
python ontology-extraction/scripts/scaffold.py validate path/to/your-ontology
```

The validator workflow runs regression tests and all five bundled examples on Python 3.11 and 3.13.
No model calls, API keys, or paid services are needed for the tests.

## 📄 License

[MIT](LICENSE) © 2026 New1Direction
