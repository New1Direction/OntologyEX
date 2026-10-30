# Source drift: is the model still about the code that is there?

Everything the model knows cites lines: each domain skill's evidence (an exact quote of
`path:start-end`), each confirmed rule's citation, each confirmed link's binding. Code moves
under them. `drift.py` finds every cited span again in the repository as it is now, and says
what happened to it:

| Span | Meaning | Needs a person? |
|---|---|---|
| `UNCHANGED` | the same text at the same lines | no |
| `REFORMATTED` | the same text once whitespace is evened out (indentation, spacing, blank lines), at about the same place | no |
| `MOVED` | the same text (exactly, or reformatted) at other lines; the new lines are given | no: read at the new lines |
| `CHANGED` | the text is not in the file any more; the nearest look-alike lines are named, with how alike | yes |
| `FILE_GONE` | the file is not in the repository (or not under one unique path) | yes |

Comments count as text: a comment is often where a rule is written down, so changing one is
a change. This compares lines of text with whitespace evened out; it does not parse the code,
so a rename that keeps every line's meaning still reads as `CHANGED`.

What rests on the spans:

- **Business objects and actions** read `CURRENT`, `REPAIRED` (their evidence only moved) or
  `STALE_REPAIR` (their evidence changed or is gone: the model may describe code that is no
  longer there) until the skill is rebuilt from the current code or the evidence re-cited.
  `STALE_REPAIR` items are ranked in the command center (under Decisions) with the changed lines
  and the nearest look-alike.
- **Confirmed rules** follow their lines: moved or reformatted, they stay confirmed and are read
  at their new lines; changed, they read *confirmed before the evidence changed* until a person
  re-confirms. Rules confirmed from now on also record a whitespace-evened hash, so
  re-indenting them is not a change; older ones are found again only when the text is exact.
- **Confirmed links** record their binding's cited lines (`span:PATH`) instead of the whole file
  the binding is in, so editing other parts of that file, moving the lines or re-indenting them
  does not reopen the link. Links confirmed before keep being compared by whole file until they
  are re-confirmed. The file that declares the component (a compose file, a manifest) is still
  compared whole.

## Use it

In the control room it runs in the background (every minute, discarded if the repository
changed under it): `/api/drift` (`?id=ENTITY`, `?format=markdown`), the entity page's *Cited
code now* panel, the command center, and the `source_drift` agent tool.

From the command line, or in CI:

```bash
python ontology-extraction/scripts/drift.py check --repo REPO --skill SKILL [--rules RULES.json] \
  [--format json] [--fail-on-stale]
```

After every commit, on request:

```bash
python ontology-extraction/scripts/drift.py hook install --repo REPO --skill SKILL [--rules RULES.json]
python ontology-extraction/scripts/drift.py hook remove --repo REPO
```

The hook is a `post-commit` hook (where git looks for hooks, `core.hooksPath` included) that
prints what the commit moved or changed under the model, and nothing when nothing needs repair.
It never blocks a commit. It refuses to replace a hook that is not its own and prints the line
to add to it instead. Installing it writes into `.git/hooks`, outside `.ontologyex`, so an
agent asks first.

It reads files only; it says the cited lines were found again, not that they still support the
claim: that is for the person reading them.
