# Evidence provenance: repeated paths are not extra votes

OntologyEX preserves two different things: **what a selected source actually says** and
**where the author believes that evidence originated**. It does not establish that a
statement is true, a source is authoritative, or two observations are statistically independent.

A refund policy copied into three API guides is still one declared origin. A separate
requirement that contradicts it must remain visible. Conversely, identical wording from
two genuinely different origins must not be silently discarded just because the text matches.

## Use with an existing workspace

Capture exact evidence with `evidence.py` first. Then, when the source history supports it:

```bash
python "$SKILL_ROOT/scripts/provenance.py" attribute WORKSPACE \
  --evidence E-refund-policy \
  --origin refund-policy-v1 \
  --group merchant-policy \
  --basis 'The selected migration notes identify this excerpt as copied from refund-policy-v1.'
```

Repeat for each mirrored evidence record with the **same origin and group**. Give genuinely
different origins different IDs. Correlated origins (for example, documents derived from the
same requirement) should share a group. Do not infer independence from distinct paths,
websites, source hashes, or different wording. Leave uncertain origins unattributed.
The free-text basis is a declaration to review, not proof or authenticated reviewer identity.

Inspect the completed model without writing a candidate:

```bash
python "$SKILL_ROOT/scripts/provenance.py" inspect WORKSPACE
```

Then submit via `onboard.py attempt SESSION` as usual. The optional sidecar is preserved
with each attempt, participates in candidate integrity and version comparison, and appears
in the report and task-focused handoff. Do not bypass the session budget with an extra build.
The provenance tool is part of the project-local install; no new dependency or network is used.

## Optional sidecar, unchanged contract v1

`60-contract.json` and its evidence shape are unchanged. Existing workspaces need no edits.
Without a sidecar, lineage is **UNKNOWN**, never silently assumed independent.
`70-provenance.json` has exactly this structure; use the command to compute the fingerprint:

```json
{
  "schema_version": 1,
  "attributions": [
    {
      "evidence_id": "E-refund-policy",
      "evidence_sha256": "SHA256_OF_THE_CANONICAL_COMPLETE_EVIDENCE_RECORD",
      "origin_id": "refund-policy-v1",
      "independence_group": "merchant-policy",
      "basis": "A reviewable explanation grounded in the selected source history."
    }
  ]
}
```

All fields are required. Duplicate JSON keys, unknown fields, invalid IDs, stale evidence
fingerprints, dangling references, and assigning one origin to inconsistent groups fail.
At most 256 attributions and 2 MiB are accepted. Partial attribution is allowed: omitted
records remain unknown. An explicitly empty array also means no lineage was declared.

The fingerprint binds the complete evidence record: file path/hash, line range, quotation,
and kind. Changing that record invalidates its old attribution. The command is atomic and
idempotent; it refuses to overwrite a different attribution. A deliberate correction belongs
in the editable sidecar of an unsubmitted/revisable workspace, or in a new reviewed version.
It never changes the contract, selected source bytes, accepted candidates, or review decisions.
Source and evidence writers use a shared cooperative lock, not an OS security boundary.

## Read the output correctly

Every new candidate includes `references/provenance.json`, also embedded as `model.json`'s
`provenance` field. Relevant records appear in `context.json` for fresh-session handoffs.

- **Source ID:** deterministic identity of a selected file path and its SHA-256. This
  identifies a file version, not a person, publisher, causal source, or independent observation.
- **Content ID:** exact quotation and evidence kind. This labels equal wording without
  erasing distinct origins, semantic context, file locations, or original citations.
- **Evidence atom:** declared origin + kind + exact quotation. Five representations of the
  same origin/quotation form one atom. Different quotations remain separate atoms even when
  they share one origin. Distinct declared origins remain distinct atoms even with equal text.
- **Unattributed atom:** whole-file hash + kind + exact quotation. This is labeled
  `IDENTICAL_FILE_BYTES_AND_QUOTE_ONLY`. It is a conservative byte-equivalence grouping,
  not an invented causal origin. Near-duplicates without lineage are not auto-merged.
- **Declared groups:** the author's proposed dependency grouping. Multiple origins can
  share a group. `independence_verified` is always false, and `independent_support_count`
  is always null. Counts do not become confidence, votes, truth labels, or permissions.

All evidence IDs, source paths, and recorded conflicts survive grouping. Repeated references
to one evidence ID are counted once for distinct-record summaries. Content-match warnings
show the affected IDs; equal text from different declared origins is retained, not merged away.
Requirements and implementation excerpts have separate kinds even when their words match.

## Conflicts and policy behavior

This feature surfaces **already recorded** contract conflicts. It does **not** discover
contradictions in natural-language documents. Authors must still inspect all relevant origins
and record disagreements in `60-contract.json`'s `conflicts` array. Two conflicting statements
from the same origin also remain a conflict; shared lineage is not a resolution strategy.

The existing offline checker is unchanged. Provenance does not alter its policy conditions
or use evidence counts to override a conflict. A missing lineage declaration does not add a
new executable business rule; it is visible review information. Candidate semantics remain
unreviewed, and all diagnostic outcomes still have `execution_authorized: false`.

Existing frozen bundles/checkers remain usable. New compilation adds metadata and produces
new bundle IDs. A provenance-only change requests review and conservatively reruns tasks.
Existing tracked sessions are pinned to their old toolchain; finish them with those tools,
or explicitly start a new session after upgrading. Never reset attempt history silently.

## Reproducible stress test

From the repository root:

```bash
python examples/provenance-stress/demo.py --out build/provenance-stress
```

Twenty authored policy fixtures each vary identical support paths across 1, 3, and 5 copies.
The original contract checker is the behavioral baseline, not a weakened path-voting agent.
The test requires unchanged modeled outcomes, stable declared origin/group counts, retention
of all cited IDs, and retention of every explicitly authored conflict. Separate controls
require sensitivity to five distinct declared origins with exactly the same words, and check
legitimate success cases without conflicts. Unknown lineage must never produce independent
support. The command writes a JSON summary and representative audit to a new output directory.

These are deterministic regression fixtures, **not** held-out model evaluation, semantic
conflict-detection accuracy, a GraphEcho reproduction, or evidence of improved agent performance.
No model calls or paid evaluations are made. Fresh-agent downstream usefulness remains NOT_RUN.

Research motivation: [GraphEcho: Structural Redundancy and Evidence Provenance in LLM Graph
Agents](https://arxiv.org/abs/2609.17695), September 15, 2026. Its distinction between path count
and origin count motivates this test. OntologyEX does not adopt that benchmark's majority-vote
verdict convention or reproduce its training experiments.
