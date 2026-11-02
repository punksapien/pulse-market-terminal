# Preview reader integration — ADR-0085

Local branch `feat/c1-contract-schema`, worktree `/private/tmp/pulse-c1-contract-schema`.
Merge this branch into the frontend integration branch after cross-review; do not
merge into main. No branch was pushed/deployed by this checkpoint. The inherited
`api/pulse.py` is the shared entry; `pulse/http/wire_reader.py` is the new reader.

Trusted Preview configuration:

- VERCEL_ENV=preview (provided by Vercel).
- PULSE_WIRE_V1_ENABLED=1 (explicitly opts into manifest transport).
- Existing Preview-only PULSE_REDIS_URL binding, PULSE_RUNTIME_RUN_ID and
  PULSE_MAPPING_VERSION. Hash mapping includes `sha256:` prefix.
- Existing explicit staging plaintext exception only if still approved/configured;
  never weaken certificate verification for rediss. No Production secret needed.

Without the new flag the existing endpoint behaviour is unchanged. Production
refuses404 before any store factory. Do not enable the flag expecting fixture data.

| URL | Body | Cache |
|---|---|---|
| `/api/pulse?horizon=1H` (also4H/24H) | `pulse.manifest.v1` | <=10s |
| `/api/pulse?artifact=sha256%3A<logicalhex>` | named encoded part from manifest | immutable1year |
| `/api/pulse?live=1` | `pulse.live_funding.v1` | <=3s |
| `/api/pulse?live=price` | `pulse.live_price.v1` | <=3s |

For a part, take `manifest.parts[name].logical_sha256` and construct the fixed
artifact URL via URLSearchParams. Don't put UTM, filters, user ids, symbols, quality
or arbitrary routes on API URLs. Fetch controls and sorting stay client-side.
ADR-0136 all-pair artifacts require prebuilt Brotli11 and gzip; a raw identity
variant is optional. Browsers decode their negotiated encoding before verifying
the logical digest. Clients accepting neither supported encoding receive406;
the public reader never decompresses or rebuilds a representation per viewer.
Old descriptors with identity remain valid and keep their existing hash checks.

The small manifest is identity-only; an explicit identity;q=0 returns406 rather
than sending an unacceptable encoding or invoking viewer-triggered compression.
Normal Accept-Encoding negotiates stored Brotli11, then optional stored Brotli5,
gzip or identity by q-values. Responses carry representation-specific ETags and
Vary. A 304 never extends any financial timestamp. Fetching a new mapping drops old
live overlays; a late old horizon/generation cannot replace the current view.

## Exact producer/store boundary (not implemented here)

1. Validate logical source inputs and full named snapshot; freeze one generation.
2. Encode parts, materialise source-batch witness artifacts, prepare variants.
3. Under fenced writer/quota/memory rules, stage bytes at exact blob hashes and
   descriptors at exact logical hashes; verify all descriptor/blob pairs.
4. Promote manifest to RuntimeDocument channel `snapshot:<horizon lowercase>:manifest`
   with source_ms=generated_ms and expires_ms=manifest expires_ms. Old channels are
   untouched. Live pointer channels are `live_funding_v1` and `live_price_v1`; their
   payload is an artifact descriptor prepared from the corresponding live document.
5. Reaper must retain all blobs/descriptors reachable from current/previous reader
   generations. No artifact writes/reaper deletions were performed in this work.

The API reader now understands that boundary, but **the composer has not staged
these artifacts**. Until it does, the new URLs correctly return503 unavailable.
There is no fallback to a fictional sample or separately fetched legacy report.

## Remaining gates

Source-batch retention/provenance,
worker Brotli packaging, atomic artifact-set staging with input-vector fencing,
incremental collector and quota dimensions, repairs,26-minute reference prefetch,
published real50/504cohort, bom1 proof, origin coalescing/CDN behaviour, full live
payload/gzip budgets, multi-generation memory and soak. The reader does not claim
those gates are done. No production readiness is implied.
