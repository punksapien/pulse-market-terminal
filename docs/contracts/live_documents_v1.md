# Live contracts — ADR-0083

29Oct amendment ADR-0194: optional price-row `volume` carries its own
`definition_version`. New `volume.rt.v2` observations expire exactly
`source_ms+300000`; legacy `volume.rt.v1` (including absent version) keeps60000.
Volume source is bulk batch time, not last trade time. Accept only a fresh live
envelope, then retain an accepted volume until its ORIGINAL field expiry even
when that15s envelope expires. Never extend price, return or funding freshness
with the volume clock. Mapping/id changes refuse reuse. See calculations/volume.md.

Schemas: `schema/live_funding.schema.json`, `schema/live_price.schema.json`.
Fixtures: `fixtures/live_funding.*.json`, `fixtures/live_price.*.json`; dictionary
companion `snapshot.partial.json`. `stale` is valid at its original generation but
expired at fixture clock1800000000000; foreign mapping must be refused. Funding
due-cycle is valid before nft=clock+1000 and must not show Current Delta after due.

Funding preserves the draft discriminator and names. Row: id/status/reason/h/nft/
fr/efr/source_ms/expires_ms/input_refs. fr and efr are signed decimal **fractions**,
not bps; UI multiplies by10000 for bps. No field classifies Current Delta. Source
clock is the funding estimate event, independent of delivery or price events.

Additive positive price fixture: `live_price.in_ladder.json`, paired with
snapshot.partial.json and wire/core+references, has source_ms=clock+4000 and
generated_ms=clock+5000. Each exact100 baseline gives fraction0.03125 at price103.125.
The original live_price.valid fixture intentionally remains unchanged; its event
precedes the ladder start, so it tests valid transport with unavailable ladder lookup.
Classified wire golden files are in `fixtures/wire_classified_stable/` and exactly
rebuild snapshot.classified_stable.json using the same decoder/schema.

Price row: id/status/reason/price/source_ms/expires_ms/input_refs. Price is the
source decimal string; source_ms is btST. The existing binary64 round-trip guard
applies before ring/calculation ingress; no reprinted float or receipt-time price.
Numerator expiry is supplied by the verified producer policy, not inferred from
HTTP cache. Match references by mapping, generation and event-time slot. Missing
ladder windows remain unavailable; never substitute a neighbouring minute.

Both envelopes: schema/mapping_version/sequence/generated_ms/expires_ms/rows/inputs.
Rows are dense dictionary order, all expected ids exactly once, including explicit
unavailable rows. h must match the dictionary's native interval. sequence is a
persisted monotonic integer per document kind and mapping, not a wall-clock guess.
Poll each document every5s; an optional transport bundle needs a separate proposal.
Two live requests per5s =24/minute/viewer before cache. No collection on reads.

## Concrete consumer integration gates (5eb2cca)

Run `node tools/contracts/check_frontend_pairing.mjs /private/tmp/pulse-fe-live`.
The four Python-oracle vectors expose two currently failing cases:

1. Estimate source=1799999998000 precedes last settlement1799999999000, but fr is
   unchanged and nft matches. Python refuses cycle_identity_mismatch; current JS
   returns a number. Smallest fix: compare source against settlement_ms.
2. Actual due=1800000000000, nft=due+3000 (permitted jitter), now=due+1000. Python
   is settlement_pending; current JS returns a number until nft. Smallest fix:
   use actual due=settlement_ms+h*3600000 as well as nft for pairing and expiry.

Also inspect applyLiveFunding's native-rate merge: it currently checks
source+120000 and nft but not the explicit row expires_ms. An estimate with a
shorter backing-input expiry can remain visible past that expiry. The adapter
must respect the minimum of all three. No frontend file was edited here.

Backend mapping bridge is closed by ADR-0087: FundingMetadata now accepts the
shared schema's128-character resource bound, including public71-character tokens.
The oracle and wire use the identical public token, without stripping its prefix
or remapping identities. Real composer/source/store integration remains unproven.

Verdict: schemas proposed; consumer/backend boundary fixes required before live.
Not checked: real live payload size, hosted source clock reliability, end-to-end
<=15s, frontend acceptance of the new schemas or the new compact decoder.
