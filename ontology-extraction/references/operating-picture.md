# Operating picture: put reviewed skills on one business screen

Use this after a domain skill exists. It joins the skill's objects, actions, and
rules to the systems discovered in the repository, and shows which business
actions are at risk, what is unlinked, and what still needs review. Run every
command from `SKILL_ROOT/scripts` paths; all of them are local and read-only
unless stated.

## 1. Look at it

```bash
python "$SKILL_ROOT/scripts/control_room.py" --repo REPO --skill SKILL [--skill SKILL2]
```

Open `http://127.0.0.1:8765/business` (system map at `/`). Without links, every
action is `UNLINKED`: health cannot be shown until a person links code to systems.

## 2. Link code to systems (a person decides)

```bash
python "$SKILL_ROOT/scripts/link_suggest.py" suggest --repo REPO --skill SKILL --links links.json
python "$SKILL_ROOT/scripts/link_suggest.py" confirm --repo REPO --skill SKILL --links links.json \
  --id SUGGESTION_ID --by NAME
python "$SKILL_ROOT/scripts/link_suggest.py" reject  --repo REPO --skill SKILL --links links.json \
  --id SUGGESTION_ID --by NAME --note WHY
```

Show the user each suggestion with its signals and let them choose. Never run
`confirm` or `reject` for a suggestion the user did not decide. To let the user
click Confirm/Reject in the page instead, add `--links links.json
--allow-link-decisions` to `control_room.py`.

## 3. Add live health (optional)

`feeds.py` writes observations from HTTP health checks, Prometheus queries, and
OpenTelemetry traces (OTLP/HTTP). The user writes the feed config; it names only
hosts they allow.

```bash
python "$SKILL_ROOT/scripts/feeds.py" check --config feeds.json --repo REPO
python "$SKILL_ROOT/scripts/feeds.py" run --config feeds.json
python "$SKILL_ROOT/scripts/control_room.py" --repo REPO --skill SKILL --links links.json \
  --observations OBSERVATIONS  # the "output" file named in feeds.json
```

Health is what linked systems report. It never proves that an action works.

For more traffic, `ontologyex-feeds run --config feeds.json` does the same job in
Rust with the same files. If the user agrees to download it,
`python "$SKILL_ROOT/scripts/fetch_engine.py"` installs the prebuilt binaries into
the skill's `bin/` after checking their checksum; show them the printed
`gh attestation verify` command. `control_room.py --view-engine rust` then also
uses the Rust discovery and graph views.

## 4. Grades, questions, queries

- `--events EVENTS` on `control_room.py` shows each rule's G0–G4
  [evidence grade](evidence-grades.md). Only the reviewer records events.
- `business_view.py --repo REPO --skill SKILL --links links.json` prints the
  same picture as JSON for scripts and CI.
- `rdf_export.py SKILL` exports the skill as N-Triples for SPARQL tools.
- `--decision-model` (with the user's consent and `ONTOLOGYEX_DECIDE_*` set)
  adds model link routes, review triage, and the page's Ask box.
  See [decision models](decision-models.md).

## Rules

The control room binds to loopback only. Links, health, and model answers are
not approvals: every result keeps `execution_authorized: false`. Do not report
`REPORTED OK` as "the action works", and do not hide `UNKNOWN`, `STALE`, or
`UNLINKED` items.
