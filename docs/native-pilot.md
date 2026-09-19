# Native-agent pilot: the first genuine onboarding run

**Status: NOT_RUN.** The shipped examples are developer-authored replays. This guide is
for observing the installed skill in an authenticated coding-agent host, then testing a
separate session's implementation. A script or compiler pass is not a substitute.

Use the current OntologyEX checkout for the operator commands below. Keep the full
checkout, reference adapters, model recipes, grading code, and recorded results outside
the agent's accessible project. The code below prepares sources only; it does not author
a model, build a candidate, or execute the upstream library.

## 1. Prepare an untouched source-only project

Python 3.11+ is required. From the OntologyEX repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r examples/cachetools-domain/requirements-case.txt
claude --version
```

Claude Code must be installed and signed in through the operator's normal local login.
Do not paste credentials into chat, a source file, or an experiment transcript. The
scripts do not supply a model subscription. A missing host is a blocker, not a failed
model trial. On Windows, use the corresponding PowerShell virtual-environment activation.

Create a new directory; existing results are never overwritten:

```bash
python - <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path('examples/cachetools-domain').resolve()))
import case
import domain_skill as ds

files, pin = case.source_files()
project = Path.home() / 'ontologyex-native-pilot' / 'project'
ds.write_files(project, files)
print('Prepared sources only:', project)
print('Pinned upstream commit:', pin['commit'])
print('Acknowledged source boundary:', ', '.join(sorted(files)))
print('Authoring: NOT_RUN; native activation: NOT_RUN')
PY

python ontology-extraction/scripts/install.py \
  --project "$HOME/ontologyex-native-pilot/project"
```

The project initially contains exactly `TASK.md`, `LICENSE`,
`src/cachetools/__init__.py`, and `src/cachetools/keys.py`, plus the installed
`.claude/skills/ontology-extraction` tooling after installation. The source helper
verifies the upstream license and source against the pinned Git blob and SHA-256 hashes.
It does not call `model_recipe.author`, `run_case`, or the reference adapter.

## 2. Activate the installed skill in a fresh session

Stay in the same terminal so the Python virtual environment remains active:

```bash
cd "$HOME/ontologyex-native-pilot/project"
claude
```

Use normal project trust and tool-approval controls. Do not enable bypass-permissions
flags. Do not use `--bare` for this interactive activation check: bare mode skips normal
skill discovery. This guide does not change user/global Claude settings, hooks, or memory.
A clean directory is **not** an OS sandbox; a controlled comparative experiment must also
restrict access to parent directories, other trials, credentials, and evaluator files.

Paste this request:

```text
/ontology-extraction

Onboard the batch-lookup workflow defined in TASK.md. Build a reusable domain skill
for implementing that task while preserving the behavior of the pinned library.

I acknowledge this exact domain-source boundary:
TASK.md
LICENSE
src/cachetools/__init__.py
src/cachetools/keys.py

Use the installed OntologyEX scripts and references as tooling, not as new domain evidence.
Use the name cachetools-native and workspaces/native-onboarding as the session path.
Follow the tracked onboarding workflow: author from these sources, use the evidence
helper, and make at most three compiler submissions in total. Do not use a model recipe,
reference implementation, prior chat, or the operator's study checkout.

Do not implement adapter.py yet. Do not execute the selected library during extraction.
Record unsupported semantics as manual-review rules or explicit unknowns. Do not force
noninteger time semantics into the integer-only checker. Do not edit the frozen source
snapshot, source inventory, attempt history, or acceptance criteria to make a build pass.

Export a task-focused handoff to build/native-handoff using the actual modeled task ID.
Report the session path, candidate path, attempts used, evidence, unresolved questions,
and actual checks performed. A compiler pass is not semantic approval. Stop for review.
```

Record the host version, exact model identifier, session ID, transcript, start/end times,
permissions, budget, every intervention, source hashes, and candidate bundle ID. Merely
seeing the skill in a menu is not an end-to-end success. Verify the transcript shows the
installed workflow being used and the artifacts match its claimed result.

If the operator manually repairs a contract or supplies a missing interpretation, record
that intervention. Do not label the run unassisted. A revision after a terminal result
must be explicit and retained, not a reset disguised as a first attempt.

## 3. Review, then use the result in another fresh session

Review source-to-claim support, conflicts, and unknowns outside the proposing session.
Keep the candidate unchanged; record the review decision separately. Copy only the
original source set, the same TASK.md, and the reviewed generated context into a separate,
clean project. Do not carry the author's conversation, reference implementation, or tests.

Ask the fresh agent to implement `adapter.py:get_many(cache, keys)` from TASK.md using
that material. Keep its permissions and budget fixed and record the complete attempt,
including failures. Do not give it the evaluator or the developer's solution.

Review the submitted Python before execution and grade it in a disposable test environment
without secrets. The existing `examples/cachetools-domain/acceptance.py` exposes
`run(get_many, library)`; use it with the submitted adapter and the pinned library,
not `reference_adapter.get_many`. It has no standalone CLI. Compare actual behavior,
retain every case result, and do not count an agent's completion message as a pass.

These public, developer-authored acceptance tests are not a secret held-out or independently
audited benchmark. No code-quality or semantic review should be replaced by one test score.

## 4. Run the small comparison only after the activation path works

Freeze the newly agent-authored candidate before downstream trials. Use three independent
sessions per variant, with identical source information, task, model, permissions, and
predeclared budgets:

| Variant | Additional context |
|---|---|
| Raw | None beyond source and task |
| Markdown | A concise domain guide |
| Domain skill | The frozen, newly agent-authored candidate |

The existing case's exported `trials/domain-skill` contains a **developer-authored** skill.
Do not use it to claim fresh extraction. Replace that variant with the native candidate
and verify all variants still contain identical underlying source bytes. Keep grader and
solution files inaccessible, not merely in a sibling folder. Randomize trial order; do
not resume sessions or share memory across variants.

Record all runs and preparation effort, not only the best result. Count legitimate task
success, behavioral failures, unnecessary refusals, operator work, elapsed time, and total
preparation-plus-execution usage. Record unknown cost as unknown, never zero. A nine-run
pilot is a case study, not evidence of universal gains. Equal performance is a valid result.

## Release gate

Do not turn `NOT_RUN` into `PASS` until a transcript and independently checked artifacts
exist. Publish the configuration, interventions, failures, and evidence limits with any
case study. A tagged release or announcement claiming native-host success or performance
improvement waits for those results; passing offline CI alone does not meet that gate.

Official host documentation (reviewed 2026-09-18):

- [Skills and explicit invocation](https://code.claude.com/docs/en/skills)
- [Programmatic usage and bare-mode behavior](https://code.claude.com/docs/en/headless)
- [Authentication](https://code.claude.com/docs/en/authentication)
