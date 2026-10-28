# Rule wording

A rule statement must have one meaning. `statement_lint.py` gives **advisory** warnings for
wording that can be read two ways. It uses heuristics in the style of ASD-STE100 Simplified
Technical English, plus a short ambiguity list. It never blocks a build and never changes a
statement.

```bash
python "$SKILL_ROOT/scripts/statement_lint.py" SESSION/workspace
python "$SKILL_ROOT/scripts/statement_lint.py" SKILL
```

| Code | Why it matters |
|---|---|
| `HELPING_VERB` | `may`, `should`, `shall`, `might`, `would`, `ought`. "May" can be permission or possibility. Use `must`, `can`, or `will`. |
| `PASSIVE_VOICE` | "can be refunded" hides who acts. Name the actor. |
| `SEMICOLON`, `MORE_THAN_ONE_SENTENCE` | Usually two rules in one statement. Write two. |
| `SENTENCE_TOO_LONG` | More than 25 words. Divide the condition. |
| `CONTRACTION` | Write the full words. |
| `AND_OR` | "A and/or B" has two readings. Say "A, B, or both". |
| `VAGUE_TERM` | "reasonable", "appropriate", "usually", "etc." Name the exact value, condition, or limit. |
| `NOT_IN_WORD_LIST` | Only with `--word-list`. A word is not in the approved list and is not a technical name. |

## Optional word list

ASD-STE100 permits a small approved dictionary plus project **technical names**. The linter
takes the ontology's class, interface, relation, task, binding, and input field names as
technical names, so domain words such as `Payment` pass. Add technical verbs with
`--technical-word`.

OntologyEX does **not** include the ASD dictionary; it is ASD's property. Supply your own copy
with lines like `ACCEPT (v) [ACCEPTS, ACCEPTED, ACCEPTED]`, for example the `word-list.md`
of an installed [simplified-technical-english](https://github.com/0xpili/simplified-technical-english)
skill:

```bash
python "$SKILL_ROOT/scripts/statement_lint.py" SESSION/workspace \
  --word-list /path/to/simplified-technical-english/references/word-list.md \
  --technical-word refund
```

## Limits

- Heuristics miss errors and report false positives. They do not certify ASD-STE100 compliance.
- **Rewording is a new interpretation.** The evidence quote stays exact; the statement does not.
  Re-read the cited span after each edit.
- Never reword a statement to hide an unknown, a conflict, or an inferred rule, or to make a
  check look executable. Keep `check: null` when the condition is outside the checker language.
