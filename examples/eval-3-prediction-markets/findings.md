# Does a published ontology exist for prediction markets?

## Search ladder executed (per references/source-mining.md §finding-published)
1. BioPortal — skipped, non-biomedical domain (friction: skill listed it first unconditionally → patched)
2. LOV — no betting/wagering/prediction-market vocabulary indexed
3. schema.org — no PredictionMarket/Wager/Bet classes (gambling deliberately uncovered); nearest: Offer, MonetaryAmount, Event
4. Industry bodies — none for PMs; FIBO Derivatives (FIBO-DER) is nearest formal neighbor since the dominant PM form is a binary option
5. Web search "prediction market ontology OWL" — zero formal ontologies; only PM mechanism literature
6. De-facto standards search — RICH result (below)

## Verdict: SPARSE FIELD — formal coverage ~0%, far below the 60% reuse threshold
→ Author L1 fresh, anchor hard to L0 (gist), PROV-O overlay for resolution provenance.
Matches the reuse-catalog crypto/DeFi row prediction exactly.

## De-facto L3 standard found: Gnosis Conditional Token Framework (CTF)
Not an ontology, but a precise machine-level vocabulary the L3 layer should align to verbatim:
- Condition := (oracle, questionId, outcomeSlotCount); Collections via indexSet bitmask; Positions = ERC-1155 token IDs
- Every Yes/No pair fully collateralized 1:1
- Powers Omen, Polymarket (>$2B election volume by 2025)
- Domain axioms documented: outcome set exhaustive + mutually exclusive; outcome prices sum to 1

## Reuse plan
| Layer | Decision |
|---|---|
| L0 | gist (business default) + PROV-O overlay (oracle attestation = provenance) |
| L1 | AUTHOR (seed in ./pm-seed-ontology/) — optional aligns_to FIBO-DER for the instrument facet |
| L2 | Author from venue API verbs (split/merge/redeem/resolve) |
| L3 | Align verbatim to Gnosis CTF terms + venue APIs (e.g. Polymarket Gamma/CLOB) |

## Side-finding → skill patch
OKG (Open Knowledge Graphs, launched ~Mar 2026): new ontology/vocabulary search engine with an MCP server — added to the discovery ladder.
