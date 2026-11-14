# Change-impact agent comparison

`impact_ab.py` compares the same model on source-grounded change-impact tasks.
Both arms have a real shell with `rg`, Git and Python, running in a fresh,
offline Docker container with a read-only root and a read-only repository. The
treatment additionally receives OntologyEX's read-only tools, including
`analyze_change` and `analyze_impact`. No domain skill is supplied by default.
The arm that goes first alternates by question.

The predeclared questions and expected source references are in
[`datasets/change-impact.json`](datasets/change-impact.json). Preflight checks
the pinned commit, clean checkout, and each evidence anchor before making model
calls. `answers.jsonl` records responses, tool names, tokens, time, and whether
cited files and line ranges exist. `trace.jsonl` contains the full tool inputs
and outputs; the OntologyEX audit is separate. These artifacts contain the
repository content seen by the model. Citation existence does **not** prove the
cited text supports a claim. Review the answers and evidence semantically and
record unsupported claims separately; the benchmark does not calculate a
phrase-matching accuracy score.

The model provider receives questions and tool output. Use public repositories
or obtain explicit authorization before sending any private source. The staged
repository and its parent directories must be readable by the Docker daemon;
only make a public, disposable copy world-readable. Do not place credentials in
that copy or the image. The image has no network connection and cannot access
the local API key when run by the harness.

## Prepare and run

Requires Linux, Docker, Python 3.11+, PyYAML, ripgrep and the Anthropic SDK. Build
an image from installed local commands and libraries; this step does not download
packages. It refuses to reuse an existing tag.

```bash
python evals/impact_sandbox.py --tag ontologyex-impact-eval:local
```

Clone the public repositories at the commits named in the dataset, create clean
copies accessible to the Docker daemon, and provide the path of each copy. For a
rootless daemon, this may require a world-readable copy in `/tmp`; use this only
for public source. Set `ANTHROPIC_API_KEY` through a private environment or
credential facility, not in an answer, script, or repository file.

```bash
python evals/impact_ab.py --repo-id fastapi-template --repo /path/to/public-fastapi-copy \
  --image ontologyex-impact-eval:local --out evals/runs/impact-fastapi \
  --key-env ANTHROPIC_API_KEY
```

```bash
python evals/impact_ab.py --repo-id express --repo /path/to/public-express-copy \
  --image ontologyex-impact-eval:local --out evals/runs/impact-express \
  --key-env ANTHROPIC_API_KEY
```

`--only ID ...` selects questions for a pilot. Existing nonempty output
folders are never overwritten. Remove the temporary image and copies after the
evaluation if they are no longer needed. This small, author-prepared question
set is a diagnostic, not an independent user study or a general benchmark.

`--arms control` or `--arms treatment` is available for a documented retry.
For a separate diagnostic, `--only ID --arms treatment --direct-impact-file
PATH` instructs the agent to call `analyze_change` before answering. That run
has a different prompt and must be reported separately from the paired test.
