# OntologyEX portable entry point

Read [ontology-extraction/SKILL.md](ontology-extraction/SKILL.md) and choose the route
that matches the user's goal. Resolve all script/reference paths from that skill directory.

For an **overview of a whole repository, product, or business** (what it is, who it serves,
how money moves, the journeys, where it breaks, what is unknown), follow
[business overview](ontology-extraction/references/business-overview.md): `overview.py scan`,
read, fill in the blueprint, `overview.py build`, then summarize the page for the user.

For **repository onboarding**, use the guided session workflow: acknowledge a narrow source
boundary, let the existing agent author the model, copy evidence with the helper, submit within
the fixed attempt budget, and produce a reviewable domain-skill handoff. Stop at unresolved
policy and separate compiler success from behavioral tests or fresh-agent results.

For **ontology-only extraction**, the original four-layer method is preserved in
[ontology-extraction/METHOD.md](ontology-extraction/METHOD.md), with its reference catalog,
source-mining method, design principles, and output bindings.

No autonomous deployment, semantic approval, unrestricted source execution, or model-quality
guarantee is implied. See the [README](README.md) for installation and examples.
