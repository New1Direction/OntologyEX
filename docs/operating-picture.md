# Business operating picture

The control room's system map shows manifests, services and their declared
dependencies. The **operating picture** adds the business layer that OntologyEX
extracts. Business objects, actions, and rules are joined to the systems they
run on, so one screen answers:

- Which business actions are at risk right now, and why?
- Which objects and actions depend on a failing system?
- Which actions still have open review questions?
- How far was each rule's evidence checked (G0–G4)?

It is a read-only, loopback view at `/business`, next to the existing
system map at `/`.

## One command

```bash
python examples/operating-picture/demo.py --serve
```

Open `http://127.0.0.1:8765/business`. The demo builds a fictional business
repository, the payments domain skill, declared links, **synthetic** health
samples, and synthetic grade events. No real system is observed.

## Your own business

1. Build a domain skill for one workflow with the guided onboarding workflow
   (`/ontology-extraction`).
2. Declare which systems run each code binding in a links file:

   ```json
   {"schema_version": 1, "kind": "ONTOLOGYEX_SYSTEM_LINKS", "links": [
     {"skill": "payments-domain", "binding": "payments.apply_refund",
      "component": "discovery:service:compose.yml:payments",
      "declared_by": "platform-team", "note": "Refund code ships in the payments service."}
   ]}
   ```

   `component` is a node ID from the system map (`/api/node`, or
   `control_room.py --state-only`). A link is an operator declaration, not a
   discovered fact. Links to unknown skills, bindings, or components are listed
   as **link gaps** and never become edges.
3. Start the control room with the skill, links, observations, and optional
   grade events:

   ```bash
   python ontology-extraction/scripts/control_room.py --repo /path/to/repo \
     --skill /path/to/skill --links links.json \
     --observations health.jsonl --events reviews/events.json
   ```

   Repeat `--skill` for more domains (up to 16, with distinct names).
   `--github owner/name` works in place of `--repo`.

## Suggested links (auto-linking with confirmation)

Writing links by hand is the slow part, so the control room suggests them.
`link_suggest.py` compares each code binding with each discovered service,
module, route, and table, and explains every suggestion with its signals:

| Signal | Strength | Example |
|---|---|---|
| `SAME_SOURCE_FILE` | STRONG | The binding's evidence file is the module's source file (`payments.py` and `src/payments.py`). Never used for services, whose source is the manifest. |
| `PATH_NAMES_COMPONENT` | MODERATE | A folder or file name in the binding's path is the component's name (`services/payments/refund.py` and `payments`). |
| `SYSTEM_NAMES_COMPONENT` | MODERATE | The skill's application `system` names the component (`fictional-payments` and `payments`). |
| `SHARED_NAME` | WEAK | The binding and the component share a word. |

Strength is ordinal, not a probability. At most five suggestions per binding are
shown, strongest first. Pairs that are already linked or were rejected are not
suggested again.

**A suggestion is never a link.** Only a person's decision changes the links file:

```bash
python ontology-extraction/scripts/link_suggest.py suggest --repo REPO --skill SKILL --links links.json
python ontology-extraction/scripts/link_suggest.py confirm --repo REPO --skill SKILL --links links.json \
  --id SUGGESTION_ID --by your-name
python ontology-extraction/scripts/link_suggest.py reject  --repo REPO --skill SKILL --links links.json \
  --id SUGGESTION_ID --by your-name --note "Payment lives in ledger"
python ontology-extraction/scripts/link_suggest.py confirm --repo REPO --skill SKILL --links links.json \
  --all-strong --by your-name                 # or --id A --id B ... for several at once
python ontology-extraction/scripts/link_suggest.py undo    --repo REPO --skill SKILL --links links.json \
  --id DECIDED_ID --by your-name
```

A confirm adds to `links`; a reject adds to `rejected`; a pair can never be both.
Several decisions at once are one atomic write: all of them land, or none do (an
unknown ID, or a file someone else changed meanwhile, refuses the whole batch).
Deciding an already decided pair changes nothing. `undo` removes a decided row, so
its suggestion comes back; `suggest` lists the decided rows with the ID that undoes
each (the same ID the suggestion had). Every change is also appended to
`links.history.jsonl` beside the links file: what, who, when, the batch it was part
of, and the row before and after. `--by` is recorded, not verified.
`dependency_suggest.py` takes the same `confirm`, `reject`, `undo`, `--all-strong`, and
repeated `--id`.

### Batches: previewed, applied whole, undone whole

Every decision is a compare-and-set on one item (`decisions.py`): it names the item,
the **version** of the decided row the person saw (the first 24 hex digits of the
sha256 of the row and its list; none when undecided), and what to write. A batch
applies only if every item is still at the version seen; otherwise nothing is written
and the answer is `409 STALE_VIEW` naming the items that changed.
- **Preview.** `"preview": true` returns the batch item by item (`confirm`, `reject`,
  `take back`, `put back`, `re-confirm`), with one digest per item. Applying with
  `"plan": {id: digest}` writes only if the batch computed now is the same: the same
  items, versions and rows. A decision made in between on one of those items makes the
  approval stale; one on other items does not, because a batch only reads and writes its
  own items.
- **Undo a batch.** `{"batch": ID, "decision": "undo_batch"}` (preview it first the same
  way) puts back what that batch changed, item by item, from the before and after it
  recorded. Every later decision on other items stays. If a later decision changed one of
  the batch's own items, the undo is refused (the preview lists who changed what) and
  nothing is overwritten. An undo is itself a batch, so it can be undone; a batch is
  undone at most once. Undoing a re-confirm puts back the row as it was before.
- **Undo one row.** The page sends the version of the row it showed, so an undo never
  removes a row someone re-decided after the page loaded.
- **Batches list.** `GET /api/links/batches` (and `/api/dependencies/batches`,
  `/api/rules/batches`) lists the recorded batches, newest first: who, when, what, which
  batch undid it.
- **Rust.** With `--view-engine rust`, `ontologyex-view decide` computes each batch (the
  same function, checked against Python by `tests/rust_decide_parity.py`); the control
  room still reads and writes the files. An `ontologyex-view` from before the kernel is
  detected at start and decisions stay on Python; it says which at start.

What is proved about this, and how it relates to the code: see
[business verification](business-verification.md#lean-decisions).

In the page, **Suggested links** lists each suggestion with its signals. By default
the server stays read-only and each card has a **Copy confirm command** button.
Start the control room with `--allow-link-decisions` (and `--links`) to get
**Confirm link** and **Reject** buttons, checkboxes with **Select all** and **Select
STRONG**, **Confirm/Reject N selected** (previewed first: what each item becomes,
then **Apply**), an **Undo** for the last action (previewed the same way), a
**Decided** list with an Undo per row, and **Recent batches**, each with **Undo batch**. That enables the write route
`POST /api/links/decision` (and `POST /api/dependencies/decision` with
`--dependencies`), which requires:

- a same-origin loopback `Origin` header (requests without one are refused),
- the per-process token that is embedded only in the `/business` page,
- a JSON body of exactly `ids` (1 to 100; or one `id`), `decision` (`confirm`,
  `reject`, `undo`, or `reconfirm`), `by`, and `note`, or `batch`, `decision`
  (`undo_batch`), `by`, and `note`; optionally `seen`, `preview`, or `plan`; at most 8 KB,
- for confirm and reject, IDs that all match suggestions in the current state; for
  undo, IDs that all match decided rows.

`python examples/operating-picture/demo.py --serve --start-unlinked` starts the
demo with no links so you can confirm the suggestions yourself.

## How health is derived

An action's health is the most urgent condition among the systems linked to
its bindings. An object's health uses the bindings that implement it.

| Health | Meaning |
|---|---|
| AT RISK | A linked system reports `error`. |
| POSSIBLY IMPACTED | A linked system declares a dependency on a failing system. Possible, not confirmed. |
| DEGRADED | A linked system reports `warning`. |
| STALE | A linked system's last observation expired. |
| UNKNOWN | A linked system has no current observation. |
| UNLINKED | No system is linked, so health cannot be shown. |
| REPORTED OK | Every linked system reports `ok`. Reports are not independently attested. |

Health describes linked systems. It is **not** the outcome of the action and
never proves the action works or fails. Readiness is separate:
`REVIEW_BLOCKED` when the action has unknowns, conflicts, inferred rules, or
rules without an executable check; otherwise `MODELED_NOT_APPROVED`. Nothing
here approves or runs an action.

## Interface

- **Key indicators:** actions at risk, possibly impacted, health unknown,
  review blockers, rules reviewed (G1+), systems reporting. Each tile opens the
  first matching item.
- **Needs attention:** at-risk, impacted, degraded, and stale items first, then
  review blockers, then unlinked actions.
- **Ontology graph:** objects, actions, and systems in three lanes. Selecting an
  item highlights its connections.
- **Inspector:** health with its reason, linked systems, open questions, rules
  with grades and exact evidence spans, objects, code bindings, and source.
- `/` focuses search, `Esc` clears. The selection is kept in the URL fragment.

## API

`GET /api/business` returns the `BUSINESS_OPERATING_PICTURE` JSON for one
control-room state evaluation. It takes no parameters. Headless:

```bash
python ontology-extraction/scripts/control_room.py --repo REPO --skill SKILL \
  --links links.json --state-only
```

includes the same projection under `business`, with `link_suggestions`.

## Boundaries

- Skills are hash-verified on every request; a changed bundle is refused.
- Evidence quotes are shown as text (at most 600 characters each), never as HTML.
- Same loopback-only server, CSP, and Host/Origin checks as the system map.
  The only write is the opt-in link decision above. No credentials, network
  calls, or remediation.
- Labels, paths, rules, and quotes can reveal private policy and architecture.

## Tests

```bash
python -m unittest tests.test_business_view -v
python tests/business_browser_smoke.py   # needs Chrome and selenium
```
