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

AI agents are great at *doing* things — issuing refunds, booking slots, calling APIs. They're bad
at *understanding the business* first. An agent will happily refund an order that was never paid,
because nobody told it that's impossible.

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

```bash
cd ontology-extraction
python scripts/scaffold.py init --name my-target --out ../my-target-ontology
# ...fill in the YAML layers...
python scripts/scaffold.py validate ../my-target-ontology   # 0 errors = structurally sound
python scripts/scaffold.py mappings ../my-target-ontology    # regenerate the crosswalk
```

The only dependency is `pyyaml` (`pip install pyyaml`). The skill itself needs nothing.

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

Five runs in [`examples/`](examples/), each validated clean by the bundled script:

| Eval | Target | Highlight |
|---|---|---|
| `eval-1-stripe` | Stripe (research) | API → safe MCP tools with preconditions baked in |
| `eval-2-realworld` | RealWorld app (retrofit) | map an existing codebase to its domain |
| `eval-3-prediction-markets` | a market (research) | a sparse / emerging domain |
| `eval-4-self` | **the kit itself** | it described its own code — **0 errors** |
| `eval-5-adyen` | Adyen (research) | **competitor swap vs Stripe: 8/9 domain concepts matched** ⭐ |

The plain-English walkthrough of the last two is in **[explain.html](explain.html)**.

## 💡 Why four layers (the payoff)

Because the middle layer belongs to the *trade*, not the *vendor*. We proved it: building the same
model for **Stripe and Adyen**, **8 of 9 core concepts matched** — only the bottom, vendor-specific
layer differed (see [`examples/eval-5-adyen/comparison-vs-stripe.md`](examples/eval-5-adyen/comparison-vs-stripe.md)).
Build your agent once on the shared layer; swap providers without re-teaching it the business.

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
[`ontology-extraction/SKILL.md`](ontology-extraction/SKILL.md); evidence rules and reuse catalog in
[`ontology-extraction/references/`](ontology-extraction/references/).

## 📁 Repo layout

```
.
├─ README.md
├─ AGENT_SKILL.md            ← the portable workflow — hand this to any agent
├─ index.html · explain.html ← zero-build site (deploy to GitHub Pages)
├─ ontology-extraction/
│  ├─ SKILL.md
│  ├─ scripts/scaffold.py    ← init · validate · mappings
│  └─ references/            ← source-mining · reuse-catalog · output-formats
└─ examples/                 ← 5 worked, validated evals
```

## 🤝 Contributing

PRs welcome — new worked evals (a real company/API/codebase + its validated ontology) are the most
valuable contribution. Run `scaffold.py validate` before opening a PR.

## 📄 License

[MIT](LICENSE) © 2026 New1Direction
