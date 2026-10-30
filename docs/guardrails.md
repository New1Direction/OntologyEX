# Guardrails: an agent's call held to the business rules before it runs

A domain skill's rules carry checks over a task's inputs: *the refund must not exceed what
was captured minus what was refunded*, *only a captured payment can be refunded*. When an
agent has a tool that performs that task (an MCP tool that issues refunds, say),
`guardrail.py` holds every call of it to those checks **before it runs**, as a Claude Code
`PreToolUse` hook.

| The call | The hook answers |
|---|---|
| breaks a rule's check | **deny**, naming the rule and what it says |
| reaches an open question (an unknown, a conflict, a rule with no check), lacks an input or has one of the wrong type, rests on code that changed since the skill was built ([source drift](drift.md)), or the guard cannot be read | **ask** a person, saying why |
| passes every check | **no decision**: the normal permission flow applies |

It never answers *allow*. Passing the modeled checks is not an approval: the rules say what
must hold, not that the action is wanted or authorized.

## Set it up

```bash
# 1. Propose a guard per task (REPO/.ontologyex/guardrails.json), then edit it.
python ontology-extraction/scripts/guardrail.py init --repo REPO --skill SKILL
```

```json
{"schema_version": 1, "skills": ["skills/payments-domain"], "on_error": "ask", "drift": true,
 "guards": [{"tool": "mcp__payments__issue_refund", "skill": "payments-domain", "task": "IssueRefund",
             "fields": {"amount_minor": "amount", "captured_minor": "payment.captured",
                        "refunded_minor": "payment.refunded", "payment_status": "payment.status",
                        "requester_authorized": "approved_by_finance"}}]}
```

- `tool`: the agent tool's name (`mcp__SERVER__TOOL`), or a glob (`mcp__payments__refund_*`).
- `fields`: for each of the task's inputs, where it is in the tool's arguments (`a.b.c`, list
  items by index), or a constant (`{"value": true}`). Types must match the skill's
  (integer, string, boolean); a float, null or missing argument goes to a person.
- `on_error`: what to answer when the guard itself cannot be read (`ask`, or `deny`).
- `drift`: ask when the code the task's rules were read from changed since (default on).

```bash
# 2. Try a call.
python ontology-extraction/scripts/guardrail.py check --repo REPO --tool mcp__payments__issue_refund \
  --input '{"amount": 900, "payment": {"captured": 1000, "refunded": 200, "status": "captured"}, "approved_by_finance": true}'

# 3. Add the hook to REPO/.claude/settings.json (kept: every other setting and hook). Remove it the same way.
python ontology-extraction/scripts/guardrail.py install --repo REPO
python ontology-extraction/scripts/guardrail.py remove --repo REPO
```

`install` adds the hook for the guarded tools only, on `PreToolUse` (the decision) and on
`PostToolUse`, `PostToolUseFailure` and `PermissionDenied` (how each call ended), so other calls never
start it. It writes
outside `.ontologyex`, so an agent asks first.

## Outcomes that are not known

A call with an external effect can time out after the effect happened. Retrying it blindly
refunds twice. So a guarded call that goes ahead is journaled as an intent
(`REPO/.ontologyex/guardrail-intents.jsonl`: the tool and a fingerprint of its arguments, never
their values), and Claude Code tells the hook how it ended:

| How it ended | Event | Outcome |
|---|---|---|
| completed | `PostToolUse` | done |
| the tool reported an error ("insufficient funds") | `PostToolUseFailure` | failed: known not to have taken effect |
| interrupted, or an error that reads like a timeout (timed out, deadline, connection reset, 502/503/504) | `PostToolUseFailure` | **unknown** |
| a person refused it | `PermissionDenied` | not run |
| no end recorded (a crash, a timeout before any report, still running) | none | **unknown** |

While an identical call (same tool, same arguments) has an unknown outcome, the next one is
not dispatched blindly: the hook **asks** a person to check the live state first. It is settled
when an identical call later completes, or when a person records that they checked:

```bash
python ontology-extraction/scripts/guardrail.py outcomes --repo REPO
python ontology-extraction/scripts/guardrail.py reconcile --repo REPO --tool mcp__payments__issue_refund \
  --by "Dana" --note "checked the ledger: the refund went through"
```

An unknown outcome older than 72 hours stops holding identical calls. The command center ranks
unknown outcomes from the last seven days (`OUTCOME_UNKNOWN`). The hook decides and records; it
never retries, reverses or dispatches anything itself.

## What is kept

Each guarded call appends one line to `REPO/.ontologyex/guardrail-audit.jsonl`: the time, the
tool, the decision, and which rules passed, failed or blocked, with the input *names*, never
their values. The control room reads the last seven days: a rule that stopped an agent is
ranked in the command center (`AGENT_BLOCKED`), because either the agent was asked to do
something it should not, or the rule is wrong.

## Limits

- It checks what the skill models, on the values in the call. A value the call does not carry
  (a payment's real captured amount, say) is only as true as the agent's argument; map inputs
  to arguments the tool itself uses, and model checks on those.
- Deny depends on the agent host honoring it. Treat it as a strong check in front of the agent,
  not a security boundary; permissions and the system's own validation still apply.
- A hook that times out (30 seconds) does not block the call.
