# Native-agent pilot: run it, keep the evidence, grade the actual submission

**Status: NOT_RUN.** The shipped examples are developer-authored replays. These operator
commands remove manual setup/import steps; they do not replace a genuine native-agent run.
The scripts do not invoke a model, supply a subscription, or authenticate a human reviewer.

Keep the operator checkout, transcripts, model recipes, reference adapters and grading
code inaccessible to the agent. Folder separation is **not** an OS sandbox. For controlled
comparisons, use isolated environments with no access to other trials or operator files.

## 1. Prepare a source-only project with one command

From a current OntologyEX checkout, using Python 3.11+:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r examples/cachetools-domain/requirements-case.txt

ONTOLOGYEX="$(pwd -P)"
PILOT="$HOME/ontologyex-native-pilot-v2"
python examples/cachetools-domain/pilot.py prepare --out "$PILOT"
python examples/cachetools-domain/pilot.py status "$PILOT"
```

On Windows, use equivalent PowerShell path variables and virtual-environment activation.
Preparation, status, and collection are Python tools. The bounded grader below requires
Linux or macOS; use a disposable Linux environment for grading on Windows.

Preparation verifies the pinned cachetools source and license fingerprints and copies only
`TASK.md`, `LICENSE`, `src/cachetools/__init__.py`, and `src/cachetools/keys.py` into `project/`,
plus the complete installed `.claude/skills/ontology-extraction` tooling. No model recipe,
reference adapter, grading code, prior model, or test results enter that project.

`operator/` contains the prompts, a source/tool/prompt fingerprint, and a run-record template.
No session is started and no compiler attempt is consumed. Existing output is never overwritten.
Use a fresh `--out` for a deliberately new pilot, not to hide a failed attempt. The generated
root `.gitignore` keeps local experiment material out of accidental commits.

Status checks the selected source files, installed tools, fixed prompt, and any tracked
onboarding session. It reports `host_binary: NOT_FOUND` or `FOUND` by looking on PATH, without
executing the binary. **FOUND does not mean signed in, activated, or successful.** Extra global
host settings, hooks, and files outside the selected inventory are not certified by this check.

## 2. Run the actual installed skill

Check your normal local installation and login; these commands do not start a model session:

```bash
claude --version
claude auth status
```

Do not paste authentication output, credentials, or tokens into a source file or a run record.
A missing executable/login is an environment blocker, not a failed model trial. The tool does
not read credentials, change global settings, install hooks, or bypass host approvals.

Keep the Python virtual environment active, then:

```bash
cd "$PILOT/project"
claude
```

Use a fresh session and normal trust/tool approvals. Paste the exact contents of
`$PILOT/operator/ONBOARDING-PROMPT.txt`. It explicitly invokes `/ontology-extraction`,
acknowledges the four-file boundary, sets the session to `workspaces/native-onboarding`,
and asks for a `cachetools-native` domain skill with at most three compiler submissions.
The prompt stops for review before implementing `adapter.py`.

Do not use `--bare` for native skill-discovery testing, resume an earlier conversation,
set permission-bypass flags, or give the agent access to the study checkout. Record the
host version, exact model identifier, session ID, times, permissions, declared budget,
actual usage, and every operator intervention. Unknown token counts or cost stay `null`,
not zero. A script/tool can bound compiler submissions, not all host model activity.

The installed workflow already uses deterministic evidence capture and a bounded repair
process. Do not hand-edit its contract to make an unassisted run look successful. Preserve
failures, source drift, and interruptions. Recovery never resets the attempt counter.

## 3. Collect a private review packet

Return to the **operator checkout**, not the agent project:

```bash
cd "$ONTOLOGYEX"
python examples/cachetools-domain/pilot.py status "$PILOT"
```

Copy `operator/run-template.json` to a new local `operator/run.json` and fill it with actual
observations. Set `authoring` to `native-agent` only for a real host run, or `developer-replay`
for a development fixture. Required timestamps include a timezone, for example
`2026-09-19T10:00:00-07:00`. Preserve unknown usage as `null`; list interventions honestly.

Export and inspect the host transcript yourself. Collection never searches host history or
credential directories. Supply only the transcript you explicitly select (UTF-8, at most
2 MiB); redact secrets before copying or sharing it. Transcript contents are not executed.

```bash
python examples/cachetools-domain/pilot.py collect "$PILOT" \
  --record "$PILOT/operator/run.json" \
  --transcript /path/to/reviewed-transcript.txt \
  --out "$PILOT/operator/packet-01"
```

The packet keeps the declared configuration/usage, transcript, original selected sources,
each available submitted model and result, and the compiled candidate when one exists.
It also fingerprints the packet and candidate. Failed, interrupted, and source-drift runs
can be retained; a collected packet is not necessarily a successful onboarding.

`EVIDENCE_COLLECTED_REVIEW_REQUIRED` means submitted bytes and records were collected.
It **does not** authenticate a transcript, prove that the native host used the skill, or
certify semantics. Verify the transcript and source-to-claim support separately. The
operator's reported `native-agent` label alone never becomes a success claim.

**Packets are private.** They can contain proprietary source, absolute paths in diagnostics,
and sensitive transcript text. There is no automatic upload or comprehensive secret scrubber.
Review/redact deliberately; fingerprints must be regenerated for any edited packet.

## 4. Prepare a separate implementation session after review

Inspect source-to-claim support, conflicts, unknowns, and unsupported conditions outside the
proposing session. Record reviewer identity, scope, candidate bundle ID and decision in your
normal review process. Acknowledging the command below is only permission for this local
pilot handoff, not authenticated human approval or production authorization.

Use the actual task ID produced by the model; `ReadBatch` is an example:

```bash
IMPLEMENTATION="$HOME/ontologyex-native-implementation-01"
python examples/cachetools-domain/pilot.py implementation "$PILOT" \
  --task ReadBatch --acknowledge-review --out "$IMPLEMENTATION"
```

The new project contains identical original source bytes, the complete unchanged candidate
under `domain-skill/`, its bundle ID, and `PROMPT.txt`. It contains no authoring transcript,
reference adapter, model recipe, or grading tests. It is not automatically installed as a
host skill. Open a **separate fresh session** in this project and paste `PROMPT.txt`.
A directory does not enforce session or memory isolation; the operator must configure it.

Ask the agent to implement `adapter.py:get_many(cache, keys)`. Record the complete attempt,
including failures, without exposing acceptance tests or the reference solution. Do not
replace the submitted file with our developer reference adapter when reporting outcomes.

## 5. Grade the actual Python submission

Review the submitted code before execution. Run grading in a disposable, externally isolated
environment with no credentials, not your normal sensitive workspace. The grader executes
submitted Python, so the acknowledgment below is required.

From the operator checkout:

```bash
python examples/cachetools-domain/grade_adapter.py \
  --source-project "$IMPLEMENTATION" \
  --adapter "$IMPLEMENTATION/adapter.py" \
  --out "$PILOT/operator/grade-01" \
  --timeout 15 --acknowledge-code-execution
```

This command uses the existing 18 public acceptance cases against the actual submitted
adapter and exact pinned library. It reserves a new result directory, freezes the adapter,
sources and tests, then runs disposable copies in a child process. When the input is an
exported implementation project, the result is bound to its verified candidate bundle ID.

The child uses isolated Python without site packages, a minimal environment rather than
inherited secrets, a wall-clock timeout, capped logs, and POSIX resource limits. **This is
not an OS sandbox:** it does not disable network or filesystem access, prevent hostile
subprocess escape, or make a malicious submission safe. Submitted code shares its process
with the tests and could interfere with them. Public tests are not an adversarial grader.

Inspect `summary.json`, `request.json`, `input/adapter.py`, every case result, and the capped
logs. Syntax/import errors, missing output, nonzero exits, timeouts, malformed results, and
changed retained inputs cannot become `TESTS_PASS`. A reservation without a summary means
the run did not complete; preserve it rather than replaying into the same directory.

CLI exits: **0** for all tests passing, **2** for a completed non-passing evaluation (including
timeouts), **1** for setup/input errors. Unknown usage stays unknown; the grader does not
measure model tokens or cost. A behavioral pass does not establish native activation,
independent authoring, semantic completeness, or improved model performance.

## 6. Compare only after the end-to-end native path works

Freeze the newly agent-authored candidate before downstream trials. Use three independent
sessions per variant, with identical source information, task, model, permissions and
predeclared budgets: raw sources, those sources plus a concise Markdown guide, and those
sources plus the frozen domain skill. Record preparation cost separately and in the total.

The existing case's exported `trials/domain-skill` is developer-authored. Do not use it to
claim fresh extraction. Use the native candidate and verify that source bytes match across
variants. Protect grading/reference files with actual access controls, randomize trial
order, retain all failures, and do not share sessions/memory between variants.

Nine runs are a small case study, not evidence of universal gains. Equal performance is a
valid result. Do not launch another framework before running the prepared experiment.

## Release gate

Native activation, fresh authoring, semantic review, and behavioral evaluation are separate
gates. No command in this patch asserts that all gates passed or publishes a performance
claim. A tagged release claiming those outcomes waits for reviewed transcripts and results.

Official documentation checked for the host instructions:
[CLI and authentication status](https://code.claude.com/docs/en/cli-reference),
[skills and explicit invocation](https://code.claude.com/docs/en/skills),
[programmatic usage](https://code.claude.com/docs/en/headless).
