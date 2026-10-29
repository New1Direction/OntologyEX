# Example overview: nextjs-subscription-payments

[The page](https://new1direction.github.io/OntologyEX/docs/examples/nextjs-subscription-payments.html)
is the Business X-Ray an agent wrote for
[vercel/nextjs-subscription-payments](https://github.com/vercel/nextjs-subscription-payments)
(MIT) by following [AGENTS.md](../../AGENTS.md): scan, read, fill in the blueprint,
build. Its claims are the agent's reading of the sources; the risks are hypotheses,
not observed incidents. The three high-severity risks were checked against the
cited lines by hand before publishing.

Rebuild it from a checkout at the same commit:

```bash
git clone https://github.com/vercel/nextjs-subscription-payments /tmp/nsp
git -C /tmp/nsp checkout bdd0813206e47e6b218d42f15a7976c8a0d3c3eb
python ontology-extraction/scripts/overview.py build --repo /tmp/nsp \
  --blueprint examples/overview/nextjs-subscription-payments.blueprint.json \
  --out docs/examples/nextjs-subscription-payments.html --github vercel/nextjs-subscription-payments
```

The build fails if any cited line no longer exists at that commit.
