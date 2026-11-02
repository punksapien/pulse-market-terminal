# Price and price change (1H / 4H / 24H)

29Oct availability clarification (ADR-0193): a last successfully verified
reference candidate can survive generation handoff for its ORIGINAL validity.
A fresh numerator is recomputed against its exact event-time minute, with that
candidate's original mapping/definition/generation pin. Prefer current references.
No old percentage is copied, and no clock, slot or denominator is extended.
Per-window provenance and expiry are retained independently. Missing/expired
candidates still yield unavailable. Arithmetic and sources below are unchanged.

27 Oct transport amendment: [ADR-0100](../decisions/ADR-0100-preview-stream-bridge.md).
The passive Preview bridge changes transport only. Tick event time still selects
the same generation-pinned minute reference; numerator and return paint together.
An absent/expired reference never becomes an interpolated or zero return.

26Oct runtime amendment: [ADR-0075](../decisions/ADR-0075-sparse-exact-minute-reference-collector.md)
implements sparse CoinDCX minute-open queries for the existing five-slot ladder
and fallback, without a25h reference ring. Formula and tolerances unchanged.
The older ten-minute fetch/ring schedule below is historical, not this sparse
collector's cadence. Three queries per pair every60s cost2.5HTTP/s at50 pairs,
25.2HTTP/s at504. Full-universe sparse scheduling is NOT yet admitted; the
recorded16/s limit and206s coverage budget must both be satisfied independently.

26OctC0parityproposal: [ADR-0049](../decisions/ADR-0049-price-precision-parity-and-coverage.md)/[checkpoint](../c0_price_parity_checkpoint_2026-10-26.md) specifies sharednumericvectors, exact-source half-eventies, binary64ingressguard and206sbudget against true240.001sremaining. Notproductionpolicy; historicalring-to-artifactsourceprecision and hosttiming remainunresolved. Noformula/volume/source substitution.

Offlineimplementation26Oct2026: [ADR-0048](../decisions/ADR-0048-price-reference-calculations.md) / [C0price checkpoint](../c0_price_reference_checkpoint_2026-10-26.md) covers pureevent-time ladder/fallback semantics and fourrecorded24hfixtures. No production/JS/wire/sourceadapter change. SourceDecimal retained in this semantic reference; explicit34-digit context for return division, no silentbinary64coercion. Sourceprecision/storage/transport bridge and completepayload gate remain open; definition below unchanged.

- Definition version: `price_change.v1` (proposed) · ADRs: 0005, 0006, 0001, 0003, **0019 (reference ladder, accepted in principle; transport by real payload measurement)**
- Status: defined; round-2 corrections applied (R2-01); awaiting peer reviewer re-review

## Formula

```text
tick              = (price p, event time T) from CoinDCX B-X_USDT@prices-futures
ref_W(T)          = open of the 1-min candle starting at floor_to_minute(T − W)   ← selected by the TICK's own T
change_W          = p / ref_W(T) − 1                   W ∈ {1H, 4H, 24H}
display           = 100 × change_W, in %
fallback          = (snapshot price ls, its own source time Ts) uses ref_W(Ts), never ref_W(now)
```

**Reference ladder (R2-01, ADR-0019):**
- One reference per window per snapshot is wrong after the next minute boundary. Example: old ref 100, new ref 110, tick 110 at 12:01:00 must show 0%, not +10%.
- So the snapshot (or a separate shared, cacheable `refs` resource) publishes, per pair and window, the **minute-keyed opens for a bounded future horizon** `H_ladder` (proposed 5 minutes), each with `ref_time`, `valid_from` / `valid_until`, source and version.
- These are ≥ 1 h old past candles, so publishing them early uses no future market data.
- A tick whose `T` falls outside the ladder's validity → window **unavailable** until a fresh ladder arrives.
- **Slots, defined (R3-02):** a ladder starting at minute `m` has slots `[m + k·60 s, m + (k+1)·60 s)` for k = 0…4 (half-open). Each slot is `{price | null}`, and a null slot is unavailable, never filled from a neighbour.
- **Version pinning (ADR-0019 review #3):** the ladder (embedded or `/api/refs`) carries `mapping_version` (pair ↔ index) and `definition_version`; the snapshot pins both. A mismatch → window unavailable, never re-paired by row order (that would give one token another token's denominator).
- **Fallback references:** each row also carries `fallback_ref = {fallback_time Ts, 1H, 4H, 24H}`, the exact three references for the snapshot price's own source time. They're computed at build time, so a 12:00:59 fallback stays valid when the ladder starts at 12:01. Measured: +7.2 KB gzipped for 503 pairs.
- **Coverage inequality (peer reviewer R3, applied):** `collector period + build lag + CDN fresh TTL + permitted stale + poll delay + missed-poll allowance + clock margin < minimum remaining ladder coverage`. With a 5-slot ladder built up to 59 s into its first minute, the minimum remaining is 241 s. Cache, poll and stale settings must leave real margin (the illustrative 240 s is too tight); otherwise lengthen the ladder, which costs ~+1.3 KB gzip per extra slot delta-encoded. **HTTP 200/304 never extends `valid_until`.**
- **Size, measured on REAL CoinDCX 1-min opens for all 503 pairs** (24 Oct, compact JSON, gzip 6 / brotli 5, 0 missing slots):

  | Encoding | 5 slots | 8 slots |
  |---|---|---|
  | packed arrays | 21.5 KB / 20.1 KB | 30.1 KB / 27.9 KB |
  | **delta-encoded** (first price + integer diffs at the pair's precision, lossless) | **13.0 KB / 12.7 KB** | 17.2 KB / 16.7 KB |

  peer reviewer's synthetic ±10% random prices gave 38.6 KB packed (worst-case entropy); real consecutive minutes compress about 3× better.
- **Transport decision rule** (peer reviewer R3, accepted): embed only if the complete worst-fixture snapshot ≤ 60,000 B gzip; otherwise a shared `/api/refs`. Decided on the real producer's output.

## Inputs and sources

| Input | Source | Cadence |
|---|---|---|
| Live price | CoinDCX per-pair socket (Binance trade relayed), p50 206 ms after the trade | every trade |
| 1-min opens | CoinDCX `candlesticks` `resolution=1` (verified identical to Binance) | each pair about every 10 min (batched); kept in a 25 h ring |
| Snapshot fallback price | CoinDCX rt `ls` | collector cadence |

## Why this way
- **Futures has no rolling 1h/4h ticker,** and spot is out of scope (ADR-0001).
- **Binance's own 24h change uses this exact method:** verified 24 Oct; openPrice = 1-min open at the minute-aligned openTime; % replicated exactly; window 24h + 17–20 s.
- **Batching is safe:** a reference is ≥ 1 h old, so it only needs to be stored before it's used. Cost: ~50 CoinDCX calls/min, 0 Binance (ADR-0003).
- **Rejected alternatives:** exact `aggTrades` lookup (over budget), Bybit `prevPrice1h` (wrong venue, no 4h), CoinDCX stats 1H (no 4H, per-pair polling). See ADR-0005.

## Edge cases and status
- **Reference minute missing** (gap, new listing < W old, TradFi market closed): `unavailable`; never carried forward.
- **Ticks:** price ≤ 0, non-finite, or `T` beyond now + 5 s → rejected. Out-of-order (`T` < last accepted) → ignored. Equal `T` → last received wins.
- **Per-instrument trade age is separate from connection health** (an illiquid pair may not trade for minutes). Show the tick's own age; the snapshot fallback stays until a newer valid tick arrives (ADR-0006 review).
- **Socket reconnect:** re-subscribe, take the current snapshot price + ladder, then resume ticks.
- **Fallback age:** fallback price older than 2 min → "delayed" marker; older than its expiry → unavailable.
- **Evidence scope:** the 24h-method equivalence was verified on sampled contracts and times (BTC, ETH, DOGE, XAU, 24 Oct). It stays a fixture, not a proof for every contract.

## Fixtures (must pass)
1. Replicates Binance `priceChangePercent` for the 24h window, from a recorded ticker plus the matching 1-min candle (BTC, ETH, DOGE, XAU samples, kept as sanitized fixture files).
2. T exactly on a minute boundary vs 59.9 s past it.
3. **Rollover before the next snapshot:** ref 100 (12:00 window) / 110 (12:01 window), tick 110 at 12:01:00 → 0%.
4. **Ladder expiry:** a CDN-held ladder past `valid_until` → unavailable.
5. Delayed and out-of-order ticks; equal-timestamp ticks.
6. Sparse / no-trade symbol: fallback with its own time and reference.
7. Missing reference minute → unavailable; new listing younger than W → unavailable for that W only.
8. Reference price 0 or invalid → unavailable.
9. Snapshot N detail open while N+1 publishes: references stay consistent with N.
10. Exact slot start (inclusive) and end (exclusive); a ladder published late in its first minute (241 s remaining).
11. Fallback time outside the ladder (12:00:59 vs ladder from 12:01) → uses `fallback_ref`.
12. Delayed tick within ladder validity vs an expired numerator; incompatible `mapping_version` → unavailable.

## Availability implementation (ADR-0122 / ADR-0123)

Historical ladders remain independently usable when the snapshot numerator expires.
That expired numerator and its changes remain unavailable; a fresh live tick may
form a new complete frame using the same exact-minute rules. Across snapshot
handoff, a complete prior live frame can remain visible only within its original
numerator/reference expiry, with unchanged mapping and a valid selected horizon.
It retains its original reference-generation provenance. No freshness, arithmetic,
source, window or tolerance is changed; never combine a new price with old returns.

## Review
### peer reviewer round-2 review — 24 Oct 2026
1. **Must fix (R2-01):** with ref=100 at 12:00:59 and next minute's ref=110, tick=110 at 12:01:00 must show 0%, not +10%. A single reference/window until the next snapshot cannot meet this. **Solution:** minute-keyed baseline ladder prepublished for a bounded future horizon (all underlying reference candles are already ≥1h old), or unavailable outside valid_until; use tick event time to select.
2. **Must fix:** snapshot fallback price requires its own source timestamp/reference pair. A stale numerator cannot use the live clock's reference. Define zero/nonfinite/negative/future ticks, equal-time ordering, fallback age and socket reconnection bootstrap.
3. **Required fixtures:** boundary rollover before next snapshot, CDN-held ladder expiry, delayed/out-of-order tick, sparse/no-trade symbol, source gap, snapshot N detail during N+1, listing age and 3 window reference identities. Reported four-market equivalence is sampled evidence, not proof all futures share identical provider ticker behavior.
**Verdict:** arithmetic approved in principle; transport/time-selection contract changes required.
**Not checked:** raw probe fixtures, live rollover/browser behavior or latency.

- peer reviewer: pending.

### peer reviewer round-3 review — 24 Oct 2026

1. **Must fix (R3-02):** build at 12:01:10 with fallback time 12:00:59 needs a previous-minute reference absent from a future-only ladder. Add exact fallback refs or past-slot coverage; fixture must exercise this actual slot lookup, not only arithmetic. Missing refs remain unavailable.
2. **Must fix (R3-01):** five-slot binary prices alone are 60,360 bytes; JSON is larger. Use the [ADR-0019 round-3 budget review](../decisions/ADR-0019-reference-ladder-transport.md) for packing, expiry and real producer acceptance. No precision loss to fit the budget.
3. **Should fix:** ADR-0019 is accepted in principle, not wholly Proposed. Add exact slot end/start, late-minute publication, delayed event vs expired numerator, incompatible mapping/version and fallback-outside-ladder fixtures. These prevent silent wrong-pair/old-price display.

**Verdict:** formula/time selection corrected; fallback coverage and transport gates remain before implementation sign-off.
**Not checked:** real candle fixtures, actual producer sizes or live socket rollover.
# Display availability amendment — 28 Oct 2026

ADR-0158 applies project owner's per-field availability approval: a verified fresh
price may display without a return. A missing/mismatched/expired reference makes
only that return unavailable. Never combine a new numerator with a previous
percentage. Source clocks, expiry, exact-minute selection and formula are unchanged.

