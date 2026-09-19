# OntologyEX — Agent Ontology Kit

**Turn unfamiliar software into a source-linked domain skill for your agent.**

Choose a workflow. Acknowledge the sources. Let your existing agent model the domain.
Get a portable skill, exact evidence, explicit unknowns, and a handoff for the next task.

[![Validator](https://github.com/New1Direction/OntologyEX/actions/workflows/validator.yml/badge.svg)](https://github.com/New1Direction/OntologyEX/actions/workflows/validator.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-13846f)](LICENSE)

**[Guided onboarding](ontology-extraction/references/guided-onboarding.md) · [Real-source case](examples/cachetools-domain/README.md) · [Payments report](https://new1direction.github.io/OntologyEX/docs/payments-demo.html) · [Contract](ontology-extraction/references/domain-skill.md)**

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
