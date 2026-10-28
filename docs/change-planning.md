# Ontology change impact planning (experimental)

OntologyEX already compares frozen domain skills with the conservative
domain_skill.py compare OLD NEW command. This optional planner adds a more
specific **declared-reference impact map** useful for reviewer triage.

It borrows the *plan before apply* workflow idea from
[Open Ontologies](https://github.com/fabio-rovai/open-ontologies)
and the explicit entity/relationship/version inspection ideas from
[Helix Foundry](https://github.com/HelixDB/helix-foundry).
It does **not** copy either runtime, import their libraries, query a graph
database, or claim formal reasoning.

## Run

Compile two distinct OntologyEX skills for the **same root goal**, using the
existing authoring and evidence review process. Then:

    python ontology-extraction/scripts/change_plan.py /path/to/old-skill /path/to/new-skill

The command verifies both complete bundle manifests and root-goal identity by
calling the existing domain_skill.compare. It exits nonzero on failure. It
does not fetch live sources, run arbitrary code from snapshots, call a model,
write to either bundle, promote a version, or execute modeled actions.

The JSON result preserves the existing comparison output and adds
**declared_impact** with:

- Changed upper anchors, domain classes, interfaces, relations, tasks and
  application bindings, keyed by their stable authored IDs.
- Each changed item's old/new evidence ID (when present).
- Possibly affected classes, relations, tasks and application bindings,
  traversing **both** versions so removed objects retain old consumers.
- Original conservative task reruns, including for changed sources,
  provenance and generator metadata.
- Explicit boundaries: semantic_conservativity = NOT_EVALUATED,
  runtime_impact = UNKNOWN, execution_authorized = false.

## Interpretation

This is **not** a soundness or completeness certificate for ontology effects.
It computes a useful, bounded *structural adjacency estimate* over L0-L3
references. It does not reason over RDFS/OWL entailments, natural-language
rules, source semantics, production state, or unknown integrations. It is not
a replacement for the existing conservative rerun list.

A change in source text can force all tasks to rerun even when no authored
ontology class changed. That is expected. The two sets answer different
questions: what **declared links** changed, and which tasks must be rechecked
**conservatively** before a new version is accepted.

Neither a successful plan nor a successful domain-skill compilation authenticates
a reviewer, approves a policy, or grants execution authority. Keep semantic
review, code-level tests and human approval outside the compiler.

## Test

    python -m unittest tests.test_change_plan -v

The focused tests cover edits and removals across all modeled layers,
legacy-consumer retention, deterministic ordering, conservative source-drift
reruns, and explicit unknown/prohibited execution states. These fixture tests
do not establish production effectiveness or independent semantic validity.

## Later integrations

If operational graph data is introduced, add stable component identity,
observations, and temporal edges with explicit observation freshness. Model
a project-specific read-only adapter separately. Do not silently convert trace
extraction into approvals or runtime policy. A future optional Open Ontologies
RDF/SHACL bridge should preserve source attribution and report exactly which
inferences were verified; it must never silently substitute a semantic risk
score for proven impact.
