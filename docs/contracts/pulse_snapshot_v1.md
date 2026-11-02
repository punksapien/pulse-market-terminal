# Pulse snapshot v1 — review draft

26Oct schema proposals: [common records](schema/common.schema.json) (ADR-0077),
[self-contained rows/envelope](schema/snapshot.schema.json) (ADR-0078),
[validation/fixtures](README.md). These are pending peer reviewer acceptance, not an
activation. ADR-0078 explicitly resolves the earlier missing price/signal/context
fields: backend carries them from pinned staging inputs, no browser legacy join.
The generated schema defines exact nested names; the older outline below is not
a competing JSON shape. No metric formula or threshold change is proposed.

Proposed ADR-0069. No production or frontend activation. Identifiers below are
illustrative placeholders, not real market values. UTF-8 JSON, UTC epoch ms.

## Shared field record

`{status, reason, value, unit, asof_ms, expires_ms, assurance}`

- status: available | unavailable | settlement_pending | unsupported.
- available requires decimal-string value, explicit unit and source/expiry times.
- unavailable/unsupported/pending require value=null and a named reason; never0.
- unit: fraction | pp | ratio | percent | quantity | USDT | price.
- assurance: consecutive_verified | window_consistent_provisional | null.
- add `classification_status:"uncalibrated"`, `level:null`, `level_basis` as an
  exact decimal string with a named unit when available. No unapproved cutoffs.
- display precision is metadata, never a change to stored arithmetic.

## Main snapshot

Required envelope: `schema:"pulse.snapshot.v1"`, `mapping_version`, `generation`,
`horizon` (1H/4H/24H), `generated_ms`, `expires_ms`, `universe`, `rows`, `inputs`.

`universe`: ordered `{id, pair_id, symbol, funding_hours}` records. ids are never
reused within a mapping; a mapping change invalidates client row-index caches.
Missing/unsupported interval is null, not8. `inputs` pins per-source/report versions
and their verification timestamps; generation digest excludes transport encoding.

Each row: `{id, oi, ls, funding, funding_change, volatility, volume, references}`.

- oi: quantity field plus change_1h/change_4h/change_24h (fraction); baseline time,
  actual duration, baseline_error_ms and mode (own/cold-start) on each change.
- ls: global_ratio/top_ratio (ratio), long_share changes1h/4h/24h (pp), divergence
  (pp, common bucket); source/expiry per series. Account skew, never exposure.
- funding: native_estimate (fraction,h), cumulative_1d/7d (fraction), cap/floor
  flags when supported; pair-specific h/nft. Current estimate belongs to live
  document; the main copy may be unavailable independently of settled fields.
- funding_change: settled_last, settled_1d, settled_7d (fraction), basis_bps
  (abs(d_last*10000*8/h)), verified_at_ms, settlement_ms, settled_rate and nft/h.
  Current Delta is a separate unclassified client projection, never L2 basis.
- volatility: existing v1 result and exact definition version; no scorer rewrite.
- volume: CoinDCX bulk RT v, Binance-venue quote volume, source time/expiry.
- references: per-horizon minute-keyed ladder `{reference_minute_ms, open_price,
  source_ms, expires_ms}` with decimal-string price and mapping/version. Follow
  ADR-0019 coverage/fallback; missing minute is unavailable, not interpolation.

Field expiry overrides envelope cache lifetime. CDN staleness is never permission
to display an expired numeric value. A missing metric does not remove the pair.

## Live funding

Envelope: `schema:"pulse.live_funding.v1"`, `mapping_version`, `sequence`,
`generated_ms`, `expires_ms`, `rows`.

Per row: `{id, status, reason, h, nft, fr, efr, source_ms, expires_ms}`. fr/efr
are exact decimal fraction strings. Never copy fr into missing efr. Expiry is
min(source+120s,nft), not receipt+120s. A batch fetch never refreshes an old row's
source time. Changed-only encoding must include a base generation and explicit
tombstones; do NOT deploy unbased deltas that a newly opened browser cannot decode.
Use a standalone full compact live document first unless a base/delta benchmark
justifies complexity.

Pair Current Delta only if same mapping/id,h,nft,fr as verified main report,
estimate fresh, and now<due. Value=efr−verified_settled_rate, displayed*10000 bps,
classification_status=unclassified. A mismatch is unavailable, not last old delta.
Funding subtraction and bps formatting must use exact decimal arithmetic (e.g.
validated scaled BigInt), not Number subtraction; binary64 price-parity approval
does not silently extend to funding differences. Shared browser/backend vectors
are a frontend wiring gate.

## Delivery and compatibility

No per-viewer compute/exchange reads. Worker serialises/compresses once. One
cacheable URL per horizon plus one live-funding URL. Variant-specific ETags;
`Vary: Accept-Encoding`. Manifest pins artifact ids, sizes, digests and encoding.
Keep active+previous+candidate only. Unknown schema/mapping fails closed. No
user identifiers/UTMs in metric cache keys. Analytics transport is separate.

Review open: formal JSON Schema/typed frontend bindings, exact payload budget,
version-vector fencing/publication, and raw/encoded fixture validation.
