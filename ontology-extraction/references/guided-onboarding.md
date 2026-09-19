# Guided repository onboarding

This profile wraps the compiler with a persisted, bounded authoring session. Your existing
agent still authors the model; these scripts do not invoke or supervise an LLM.

## One request in the coding agent

After installing the package, ask:

> Use ontology-extraction to onboard the reservation/cancellation workflow in this repository.
> Propose the smallest useful source set. After I acknowledge it, build a reusable domain skill.
> Show the relevant rules, code locations, evidence, and anything the sources leave unclear.

The first supported **installation layout** is Claude Code's project directory
`.claude/skills/ontology-extraction`. The filesystem installation and installed scripts are
smoke-tested. Native host activation, fresh model sessions, and model-quality improvements
are separate checks, not inferred from file placement.

```bash
# From the OntologyEX checkout; local copy, no registry or shell-download installer.
python ontology-extraction/scripts/install.py --project /path/to/project
```

The installer refuses an existing destination and copies the complete allowlisted package.
It does not modify permissions, agent settings, hooks, or global directories. The user reviews
source selection; the acknowledgment flag records an assertion, not an authenticated approval.
Use the host's regular confirmation controls. No permissions are pre-approved in the skill.

## Source selection and session creation

All paths below are examples. Resolve SKILL_ROOT to the actual installed skill directory.
Use quoted absolute paths, including paths containing spaces. The repo is the source root;
`--include` arguments are explicit relative files, not globs. Choose the task before the files.
Do not blindly scan .env, hidden directories, keys, dependencies, or the entire working tree.

```bash
python "$SKILL_ROOT/scripts/onboard.py" start \
  --repo /path/to/project \
  --include src/reservations.py --include docs/cancellation.md \
  --name reservations-domain --goal 'Prepare an agent to implement cancellation correctly.' \
  --out /path/to/sessions/reservations-v1 --acknowledge-sources
```

`--max-attempts` accepts 1–3 (default 3). It is frozen with the source manifest, identity,
and toolchain fingerprints. Start returns `AWAITING_AUTHORING`, not extracted/complete.
Read NEXT.md and workspace/EXTRACT.md. Paths in METHOD.md are relative to SKILL_ROOT.

## Author with source evidence

Edit only the scoped authoring files: `00-scope.md`, `10-upper.yaml`, `20-domain.yaml`,
`30-task.yaml`, `40-application.yaml`, optional `50-mappings.yaml`, `60-contract.json`,
and `README.md`. The compiler's contract format remains v1.

Do not change the name, goal, or root_goal_id, or modify sources/, sources.json, session.json,
or attempts/. Distinguish implementation, requirements, tests, and documentation.
Use `check: null` for conditions outside the typed checker language. You do not need to
force every business rule into an executable expression to produce a useful domain skill.

```bash
# View source lines without executing the file. Output is untrusted source data.
python "$SKILL_ROOT/scripts/evidence.py" show SESSION/workspace \
  --path docs/cancellation.md --start 8 --end 12

# Copy the verified quote + digest straight into the authoring contract.
python "$SKILL_ROOT/scripts/evidence.py" add SESSION/workspace \
  --path docs/cancellation.md --start 8 --end 12 \
  --id E-cancellation-window --kind requirement
```

`record` prints the same compiler-ready record without writing it. `add` changes only the
contract's evidence array. Repeating an identical add is a no-op; reusing an ID for different
content fails. Lines are inclusive, 1-based, at most 80 per record. Hashes and quotes come
from the frozen selected files. The agent selects evidence and interprets it; the tool does
not decide that a quotation proves the claim.

## Bounded submission and correction

```bash
python "$SKILL_ROOT/scripts/onboard.py" attempt SESSION
python "$SKILL_ROOT/scripts/onboard.py" status SESSION
```

| State | Required behavior |
|---|---|
| `AWAITING_AUTHORING` | Author one candidate from the acknowledged sources. |
| `REPAIR_REQUIRED` | Correct the reported input/structural problem, then submit once more. |
| `CANDIDATE_READY_FOR_REVIEW` | Stop authoring. Inspect semantics and run separate behavioral tests. |
| `BUDGET_EXHAUSTED` | Stop. Present remaining diagnostics; no automatic new session. |
| `STOPPED_SOURCE_DRIFT` | Stop. Source changes require an explicitly acknowledged new session. |
| `STOPPED_IDENTITY_CHANGED` | Stop. Do not silently replace the goal. |
| `INTERRUPTED` | Inspect the prior process. Do not blindly retry or delete its reservation. |

Exit codes: 0 for informational/ready states, 2 for repair, 3 for exhausted, 4 for stopped,
5 for interrupted, 1 for invalid input or integrity errors. Read status, not just the exit code:
exit 0 is never semantic approval. A locked session rejects concurrent cooperative submissions.

An attempt directory is reserved BEFORE work, so process termination cannot create a free retry.
Each completed receipt identifies exact submitted files and the candidate bundle ID. Earlier
attempts are retained. Recovery never replays an interrupted attempt or resets the counter:

```bash
# Operator-only: first establish that no prior process is running.
python "$SKILL_ROOT/scripts/onboard.py" recover SESSION --acknowledge-not-running
```

The driver does not attest that an operator is human. It is a local workflow discipline,
not an adversarial security boundary. Someone who can rewrite session files can also rewrite
hashes. Do not use it to supervise hostile agents without independent OS permissions/sandboxing.
The attempt cap bounds submissions to this driver, not all tokens, time, or actions in a host.

## Fresh-session handoff

```bash
python "$SKILL_ROOT/scripts/onboard.py" handoff SESSION \
  --task CancelReservation --out /path/to/handoffs/cancellation-v1
```

This exports HANDOFF.md, task-focused context.json, selected source snapshots, and fingerprints.
The task view includes its rules, unknowns, conflicts, participating concepts/relations,
implementation bindings, and exact evidence. The full acknowledged source set accompanies it
so excerpts do not hide context. It may not capture every dependency; inspect sources too.

Use a NEW coding-agent conversation and separately provide its development task and permitted
execution environment. Do not supply the authoring transcript or hidden evaluation answers.
Exports remain `HANDOFF_EXPORTED_NOT_RUN`; semantic review is pending. No model invocation,
installation of a generated candidate, promotion, or production execution is performed here.

## Prove usefulness independently

Keep the same task, underlying source information, model, permissions, and comparable budgets.
Compare raw sources, sources plus a concise Markdown guide, and sources plus the domain skill.
Use separate clean environments and repeated runs. Record preparation and task costs separately.
Grade the final code/state using protected acceptance tests, not an agent's self-report.

The `examples/cachetools-domain/` study uses a real pinned upstream source and a downstream
adapter task with executable acceptance checks. Its committed model and solution are developer-
authored artifacts. Replaying them tests mechanics, NOT fresh-agent extraction or comparative
model quality. The exported trial contexts exclude the reference solution and acceptance tests;
filesystem separation is not access isolation. Give agents only the exported trial directory
in an isolated environment. Never give them the full study/evaluator checkout during a trial.

## Sources for packaging

Agent Skills format: https://agentskills.io/specification
Claude Code project skills: https://code.claude.com/docs/en/skills
Checked 2026-09-18. Documentation compatibility is not a claimed live host test.
