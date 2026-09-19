# Payments: source → domain skill → tested update

This is a small, fictional teaching service. It has no external dependencies beyond PyYAML during build, makes no model calls, and moves no money. `source/payments.py` is a pure in-memory ledger transition, not a production payment gateway.

Run from the repository root:

```bash
python examples/payments-domain/demo.py --out build/payments-demo
```

The demo freezes two versions of the requirements, populates a **pre-authored** four-layer model with exact local evidence, compiles two portable skills, checks 13 cases per version, and records a policy-change impact report. Version 2 adds a 1,000-minor-unit per-request limit. A 2,000 request that fit the remaining balance in v1 fails v2; a 1,000 request still passes. A 3,000 over-refund fails both versions. Exception approval remains unspecified and produces `NEEDS_REVIEW`.

The consumer runs the generated model's offline checks before calling the trusted fixture's ledger function. The regression harness compares the resulting immutable ledger record with independently authored expectations. Rejected and unresolved actions do not transition the ledger. This proves this local integration on these cases—not that an AI agent has improved or that an application is safe.

## Files to inspect

Open `build/payments-demo/v2/payments-domain/report.html` first. Compare the two `model.json` files and inspect `impact.json`. Check `summary.json`, `v1/evaluation.json`, and `v2/evaluation.json` for actual deterministic results. Open the original requirement and implementation files to inspect the proposed source support.

The modeled rules are authored in `populate` in `demo.py`; the expected outcomes are explicitly authored in `cases`. The latter are not derived by calling the checker. They are still small public fixtures maintained in the same project, not a separately audited gold standard.

## A real agent comparison is a separate run

Each version also exports:

```text
benchmark/
  raw/         original code and requirements, task inputs, fresh-session prompt
  markdown/    same sources and tasks, plus a concise business-rules guide
  skill/       same sources and tasks, plus the generated skill
  oracle.json  reference outcomes; do not supply this to evaluated agents
  STATUS.json  all three arms remain NOT_RUN until you actually run them
```

Use a fresh, isolated session for each arm. Supply only that arm's folder, not the parent benchmark directory. Match model/version, tool permissions, and budgets, randomize arm order when possible, and preserve full traces, tokens, elapsed time, refusals, and errors. Include preparation cost, not just inference cost. The raw baseline receives all source information; it is not deliberately deprived of requirements.

The `reserved` split is omitted from model construction as task cases, but it is public and small. It is not a sealed or contamination-free holdout. For meaningful generalization claims, add new privately held tasks or repositories before tuning the skill.

A session should return a JSON array of outcome proposals:

```json
[
  {"id": "valid-small", "status": "CHECKS_PASS", "refunded_minor_after": 8500}
]
```

The snippet is incomplete; an actual submission must cover every task exactly once. Score a complete result:

```bash
python examples/payments-domain/score.py \
  --oracle build/payments-demo/v1/benchmark/oracle.json \
  --predictions path/to/raw-session-output.json
```

Repeat for the other arms and keep the scores with the corresponding run metadata. The scorer checks both decisions and proposed final ledger values. It refuses missing, extra, duplicate, and malformed predictions; it does not silently drop difficult cases.

**A proposal score is not an authenticated execution trace.** The scorer does not run the agent, prove session isolation, authenticate model identity, or establish a production state change. No raw/Markdown/skill improvement percentages are published until real runs and their evidence exist.

## Review the update

The demo deliberately verifies that v1's source snapshot cannot claim freshness against the changed v2 repository. It builds a new candidate and reports impacted actions without replacing v1.

Both skills remain `UNREVIEWED_CANDIDATE`. The generated review checklist requests semantic review and independent integration tests. No automated review approval or promotion occurs. Human approval is not fabricated just because CI passes.
