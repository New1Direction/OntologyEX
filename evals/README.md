# Does OntologyEX make an agent right more often?

For cross-file change-impact questions with the same offline coding shell in both arms, see the
[change-impact evaluation](change-impact.md).
For a deliberately constructed comparison of early-signal rules, including misses and missing
data, see the [synthetic early-warning replay](early-warning.md).

`agent_ab.py` asks the same model the same questions about a repository twice:

| Arm | The agent can |
| --- | --- |
| **control** | list, read and search the repository's files |
| **treatment** | the same, plus OntologyEX's read-only agent tools over the same repository: `check_answerable`, `search_entities`, `get_entity_context`, `explain_relationship`, `what_needs_attention`, `operations_status`, `who_owns`, `list_suppliers`, `source_drift` |

Both arms end by calling `submit_answer` with the answer, whether the sources answer the question,
and the file lines it rests on. No model grades another. Scoring is deterministic:

- **correct**: an answerable question's answer states every gold fact (whole words, any of the
  accepted phrasings) and is marked answerable; an unanswerable one is declined (`answerable: false`);
- **confidently wrong**: marked answerable when it is not, or missing a fact;
- **citations**: each cited `path:start-end` must exist in the repository; an answer is **grounded**
  when one citation overlaps a gold span; **strict** is correct and grounded;
- tool calls, tokens and refusals per arm.

The report gives each arm's numbers, the difference, every question OntologyEX **fixed** (right only
with it) and **broke** (right only without it), and a breakdown by category.

These scores are indicators, not a semantic review. A correct paraphrase or an alternative valid
citation can miss the gold rubric; conversely, the required phrases can appear alongside an incorrect
claim. Read the saved answers and check consequential claims against the sources before treating a
score as evidence of correctness. The saved call list contains tool names, not their arguments or
responses.

## Datasets

| Dataset | Repository | Questions |
| --- | --- | --- |
| [`demo-shop`](datasets/demo-shop.json) | the synthetic shop from `examples/operating-picture`, as of 2026-10-29T12:00:00Z | 24: rules, written policies, systems, suppliers, records, live health, and 4 the sources do not answer |
| [`fastapi-template`](datasets/fastapi-template.json) | `fastapi/full-stack-fastapi-template` at a pinned commit (cloned on first run) | 13: rules in code, the system around it, and 3 the sources do not answer |

Every gold answer cites the lines it rests on, and `check` holds each span to an anchor phrase, so a
dataset that drifts from its repository fails before any agent runs:

```bash
python evals/agent_ab.py check evals/datasets/demo-shop.json
```

Read the categories apart. **live** questions (payments health, its error rate, queue lag) can only be
answered from live health, which is not in the repository: the control arm cannot get them right, and
counting them in a headline would flatter OntologyEX. **records** questions are answerable in both arms,
from the exported CSVs in the repository, but take arithmetic over weeks of rows without OntologyEX.
The refund-rate question asks for refund **value** divided by payment value over completed weeks,
not the percentage of payment records refunded; the open-dispute question includes both
`needs_response` and `under_review` statuses.
**unanswerable** questions measure whether the agent says "the sources do not say" instead of guessing;
one of them (`approve-exception`) sits next to a nearby rule that tempts a wrong answer.

## Running it

Running agents needs the `anthropic` package and an API key. The kit itself still needs only PyYAML;
this is the one place that calls a model.

```bash
pip install anthropic
```

```bash
export ANTHROPIC_API_KEY=...   # or a profile from `ant auth login`
```

Where `ANTHROPIC_API_KEY` already means something else (an agent harness that runs on its own key, a
cloud session), keep the eval's key in a variable of its own and pass its name: `--key-env
ONTOLOGYEX_EVAL_API_KEY`. The key is read from the environment only; it is never written to the run.

```bash
python evals/agent_ab.py run evals/datasets/demo-shop.json --out evals/runs/demo-shop --model claude-opus-5-5 --effort medium
```

```bash
python evals/agent_ab.py report evals/runs/demo-shop --markdown
```

Each run writes `answers.jsonl` (every arm's calls, stop reason and submitted answer), `scores.jsonl`,
`run.json`, `report.json`, `report.md` and the OntologyEX audit log. Try `--only ID ...` on a few
questions first: the report counts input and output tokens per arm, which is what the run costs.

Choices that keep the comparison fair:

- one model per run, the same in both arms, with the same system prompt, file tools and turn limit;
- the same 64,000-character limit per tool result in both arms; longer results are explicitly marked truncated;
- no fallback model: a refusal or an exhausted turn limit is scored as it came;
- the OntologyEX tools run under the default agent policy, read-only, with every call audited;
- the repository is only read, in both arms.

`--agent scripted:FILE` replays a script instead of a model; `tests/test_agent_eval.py` uses it to pin
the scoring and the report without a key.

## Results

None published yet. Numbers belong here only from a real run, with the run's `report.md` alongside
it and the model, effort and date it ran with, fixed and broken questions included.
