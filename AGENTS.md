# For AI agents

## Your user gave you this repository to understand their own project

OntologyEX helps you explain **their** repository, product, business, or company:
what it is, who it serves, how money moves, the main journeys, where it breaks, and
what nobody wrote down. Every claim you make links to the lines it rests on. The
tools only read their repository; they run and change nothing in it.

1. **Get the kit** (Python 3.11 or newer; `KIT` is wherever you can write):

   ```bash
   git clone --depth 1 https://github.com/New1Direction/OntologyEX.git KIT
   python3 -m venv KIT/.venv
   KIT/.venv/bin/python -m pip install 'PyYAML==6.0.3'
   ```

   On Windows use `KIT\.venv\Scripts\python`.

2. **Scan their repository** (`REPO`). This reads files only:

   ```bash
   KIT/.venv/bin/python KIT/ontology-extraction/scripts/overview.py scan \
     --repo REPO --out REPO/.ontologyex/overview
   ```

3. **Read and write**, following
   [business overview](ontology-extraction/references/business-overview.md): read the
   docs and the `read_first` files the scan lists, then fill in
   `REPO/.ontologyex/overview/blueprint.json`: purpose, audiences, value, money flows,
   journeys, risks, unknowns, improvements, each with citations and a basis. The
   reference lists every field and shows one item of each kind.

4. **Build the page**:

   ```bash
   KIT/.venv/bin/python KIT/ontology-extraction/scripts/overview.py build --repo REPO \
     --blueprint REPO/.ontologyex/overview/blueprint.json --out REPO/.ontologyex/overview/business.html
   ```

   Read the `check_citations` it prints (the first and last cited line of each) and
   fix any range that is off.

5. **Tell them**: the page path (one offline file; they open it in a browser), then
   what it is, how money moves, the main journeys, where it breaks, and what is
   unknown, with a file reference or two each. Say it is your reading of the sources,
   linked so they can check it.

Then offer what fits:

- **Their systems, live:** `KIT/.venv/bin/python KIT/ontology-extraction/scripts/operate.py up --repo REPO`
  opens a local map: `/command` ranks everything that needs attention with why, and `/digest`
  says what moved since last week (it records history in `REPO/.ontologyex`), and `/owners` says
  who answers for each part (CODEOWNERS, then git history), and `/compliance` maps SOC 2, PCI DSS,
  HIPAA and GDPR to the evidence in the code;
  `/operations` shows what the business actually does from its own records (exports dropped in
  `REPO/.ontologyex/operations`, kept as weekly aggregates only; a read-only pull needs their key and
  their yes, and `up --pulls` keeps the ones declared in `pulls.json` current, see
  [operations](docs/operations.md));
  `feed_setup.py` adds health checks and `otel_setup.py` adds
  traces between services, one command per service with no code change (see
  [operating picture](ontology-extraction/references/operating-picture.md)).
- **Ask about it as an agent, safely:** `KIT/.venv/bin/python KIT/ontology-extraction/scripts/agent_tools.py serve`
  gives MCP tools over a running control room: read tools, and propose tools (links,
  dependencies, rules) whose proposals a person confirms (see [entity context](ontology-extraction/references/entity-context.md)).
- **Before they merge a change:** `KIT/.venv/bin/python KIT/ontology-extraction/scripts/change_impact.py --repo REPO --base main --head BRANCH --bundle BUNDLE --markdown`
  says what it touches in their business and what relies on it (git is only read); `drift.py check`
  says which cited lines moved or changed under the model, and `drift.py hook install` prints it after
  each commit (it writes a git hook: ask first; see [source drift](docs/drift.md)).
- **Hold an agent's tools to the rules:** `guardrail.py init` proposes `.ontologyex/guardrails.json`
  (which tool performs which task, where each input is in its arguments) and `guardrail.py install`
  adds a Claude Code PreToolUse hook: a broken rule is denied, an open question asks a person, a pass
  decides nothing (it writes `.claude/settings.json`: ask first; see [guardrails](docs/guardrails.md)).
- **Check the conclusions against what happened:** each command-center finding names its evidence
  (`evidence`, `contradicted_by`); `replay.py capture --repo REPO --name NAME` saves what was known at a
  moment, and `replay.py run` replays saved moments against what really happened (see [replays](docs/replay.md)).
- **Change one workflow safely:** build a domain skill for it
  ([guided onboarding](ontology-extraction/references/guided-onboarding.md)).
- **Keep the kit in their project:** `KIT/.venv/bin/python KIT/ontology-extraction/scripts/install.py --project REPO`
  installs it as a Claude Code skill.

Rules: read, do not run their code or install their dependencies; never send their
source anywhere; never copy a secret (key, password, token) into the blueprint; ask before anything that downloads (`otel_setup.py run` installs
OpenTelemetry from npm or PyPI on first use) or writes outside `REPO/.ontologyex`;
when the sources do not say, write it down as unknown instead of guessing.

## Working on OntologyEX itself

- Python tools: `ontology-extraction/scripts/`; the Rust engine: `engine/`.
- Tests: `python -m unittest discover -s tests` (Python 3.11+, `PyYAML==6.0.3`).
- Rust: `cargo test --locked --manifest-path engine/Cargo.toml`; Rust must give the
  same answers as Python: `python tests/rust_feeds_parity.py engine/target/debug/ontologyex-feeds`
  and `python tests/rust_discover_parity.py engine/target/debug/ontologyex-discover`.
- Golden fixtures pin the answers themselves, not only Python-Rust agreement:
  `python tests/golden.py check` (add `--engine-dir engine/target/debug --require-rust` for Rust).
  When a change is meant to alter an answer, run `python tests/golden.py update --reason "why"`;
  the reason is recorded per file in `tests/golden/MANIFEST.json` and `tests/golden/CHANGES.md`.
- Benchmarks report time, CPU and correctness apart, with the machine they ran on:
  `python tests/bench_rust_engines.py --engine-dir engine/target/release --json docs/benchmarks/latest.json --markdown docs/benchmarks/latest.md`,
  then `--write-docs docs/benchmarks/latest.json` so the docs quote it. Never type a benchmark number by hand.
- Lean: statements in `proofs/lean/OntologyEXSpec/` are trusted and pinned; proofs go in
  `proofs/lean/OntologyEXProofs/`. `python tests/lean_statements.py check --lean` (lake on PATH)
  holds each proof to its statement; a changed statement needs `update --reason "why"`.
- Tools propose; a person decides. Nothing writes to production or authorizes actions.
