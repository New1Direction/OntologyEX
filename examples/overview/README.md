# Example overviews

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

## Saleor

[The page](https://new1direction.github.io/OntologyEX/docs/examples/saleor.html) was
written the same way for [saleor/saleor](https://github.com/saleor/saleor) (BSD-3). Its
critical risk (checkout waits on synchronous app webhooks with a 2 s + 18 s timeout, and
the circuit breaker is off by default) and the telemetry and transaction-token claims
were checked against the cited lines by hand before publishing.

```bash
git clone https://github.com/saleor/saleor /tmp/saleor
git -C /tmp/saleor checkout c982006cc04c77a97def93218dedac8d715eb041
python ontology-extraction/scripts/overview.py build --repo /tmp/saleor \
  --blueprint examples/overview/saleor.blueprint.json \
  --out docs/examples/saleor.html --github saleor/saleor
```
