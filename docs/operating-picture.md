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

includes the same projection under `business`.

## Boundaries

- Skills are hash-verified on every request; a changed bundle is refused.
- Evidence quotes are shown as text (at most 600 characters each), never as HTML.
- Same loopback-only, GET-only server, CSP, and Host/Origin checks as the
  system map. No credentials, writes, network calls, or remediation.
- Labels, paths, rules, and quotes can reveal private policy and architecture.

## Tests

```bash
python -m unittest tests.test_business_view -v
python tests/business_browser_smoke.py   # needs Chrome and selenium
```
