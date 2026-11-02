# Funding rate

28 Oct all-mode refresh scheduling (ADR-0134, project owner approved): a verified
settlement report is reused until its next settlement/cycle change, retaining
the original verification clock and R6 expiry. Independent120s metadata gates
still apply when verifying or pairing a native estimate. No fifteen-minute
history refetch solely because the same verified report is older; failed
refreshes retain last good, never extend freshness or refill missing values.

27 Oct detail history (ADR-0126): lazy charts contain the consecutive verified
settlement suffix, limited by the available verified 1D/7D window. They show
individual settled rates, not a cumulative curve or live estimates. Unverified
last-settlement values remain unavailable and are labelled awaiting verification.

Display only (ADR-0125,27Oct): bps are rounded to exactly two decimals, with
rounded zero shown0.00bps. Underlying fractions, levels and calculations unchanged.

27 Oct window-meter amendment: ADR-0119 classifies available1D/7D accumulated
funding on `abs(total * 8/W_hours - 0.0001) * 10000`, using the existing pilot
2/4/9bps scale. Original signed totals, sources, verified event count and expiry
remain unchanged. The meter is an informational eight-hour-equivalent average,
not a calibrated forecast. Earlier statements restricting meters to Current are
superseded for these verified windows only.

27Oct runtime note (ADR-0107): the native estimate now has an independent5s
publication path with the existing15s live envelope. It reuses the canonical
native calculation and source/metadata expiry; settlement verification and R6
retention are unchanged. Numeric L2 cutoffs still require calibration.

- Definition version: `funding.v1` (proposed) · ADRs: 0008, 0004, 0003, 0015, **0041 (settlement verification and accumulation, Proposed)**
- Status: defined; round-2 corrections applied (R2-02, R2-03, R2-08); cut-offs pending; awaiting peer reviewer re-review

## Formula

Wire identity bridge (ADR-0087; no arithmetic change): mapping_version is an opaque
non-empty string bounded at128 characters, matching the common token schema and
admitting71-character `sha256:` identities. Pair-id remains bounded at64. Matching
is exact; never remove the hash prefix to fit an internal resource bound.

```text
F_est             = CoinDCX rt efr (fraction per native interval h ∈ {1,4,8}); mirrors Binance estimate
F_8h              = F × 8 / h                                    8h-equivalent, for levels only
anchor_distance   = |F_8h − 0.0001|                              DISTANCE FROM THE HOUSE ANCHOR (not absolute magnitude)
direction         = longs_pay (F>0) | shorts_pay (F<0) | zero    (sign of F itself)
APR_simple        = F × (24/h) × 365                             constant-rate annualisation, not a forecast or realised yield
cumulative_rate_W = Σ rate of the last N = W/h verified Regular settlements (1D: 24/h, 7D: 168/h)   UNWEIGHTED rate sum, anchored at the latest verified settlement (ADR-0041 R4)
at_cap / at_floor = F ≥ cap  /  F ≤ floor                        signed, per-contract metadata, provider precision
```

- **The level basis is "distance from the house anchor"** (R2-02).
  - +0.01% / 8h is the **product-wide anchor project owner chose**. It is *not* claimed as every contract's native neutral; Binance documents interest exceptions, and stock-linked TradFi perps use 0% interest ([TradFi FAQ](https://www.binance.com/en/support/faq/detail/1b88e951ee42414396b0c3a86ba4be4f)).
  - F = 0 and F = +0.02% have equal distance.
  - Contract-specific anchors would need a new ADR.
  - Unknown interval, interest or cap metadata → not guessed. **Unsupported per field** (e.g. `at_cap` unsupported while the native rate stays available); one missing field never suppresses the others.
- **The cumulative figure is a rate, not cash** (R2-03). Holder cash = Σ(notionalᵢ × Fᵢ) with side and holdings, which is out of scope. Counter-example: notional 10,000 then 20,000 at 0.01% each → cumulative rate 0.02%, cash 3 USDT (not 2).
- **Where cumulative funding comes from:** no provider supplies it (CoinDCX has no history at all; Binance public data has the per-settlement events only). We **accumulate**: a simple unweighted sum of the stored per-event fractions (not compounded, not cash), displayed in the Funding tab's unit (bps = 10000 × Σ). **Counts depend on the interval, and most pairs are not 8H:** 393 of 503 pairs settle every 4H (1D = 6 events, 7D = 42), 103 every 8H (3 / 21), 7 every 1H (24 / 168). Never assume 8H.
- **Verification (ADR-0041, replaces the "expected events from interval history" step below):**
  - the interval `h` must agree between Binance `fundingInfo` and CoinDCX `funding_frequency` (measured 503 / 503) and be 1, 4 or 8;
  - the latest event must satisfy `t_L + h ≈ nextFundingTime` (±5 s) and CoinDCX `fr` must equal its rate;
  - the last N events must be exactly N, all `Regular`, with every spacing `h ± 5 s`; otherwise the window is unavailable with a named reason (`incomplete_or_interval_changed_in_window`, `insufficient_history`);
  - settled-derived values expire at `t_L + h + 15 min`.
- **Settlement history (R2-08):** Binance `GET /fapi/v1/fundingRate` **without `symbol`**, in bulk.
  - Collected over **overlapping** time windows, deduplicated on `(symbol, fundingTime, rateType)`.
  - Reconciled per pair with the ADR-0041 R1–R6 checks before marking a window complete (current two-source interval + spacing, not an inferred historical schedule).
  - Per-symbol fetches only for unresolved gaps, under budget.
  - Pagination must not skip timestamp ties at the page edge (no naive `lastTime + 1`).
  - The per-pair completeness watermark never advances past unverified events.
  - "1–2 requests per mass settlement" is the measured normal case, not a recovery bound.
  - 8 days kept.
- **Display:** native rate with interval label, next settlement time.

## Why this way
- **CoinDCX-first:** `efr` / `fr` mirror Binance on sampled contracts (ADR-0003). Mismatch detection is in the ADR-0003 review.
- **Cumulative rate sum** (project owner's choice) is comparable across 1H and 8H contracts (24 vs 3 settlements per day). It is labelled "cumulative funding rate", never "paid".
- **Neutral centred on 0.01% / 8h** (project owner's choice): the default interest component is 0.01% / 8h in Binance's published formula ([Binance FAQ](https://www.binance.com/en/support/faq/detail/360033525031)), and the same default appears in the funding documentation of [Bybit](https://www.bybit.com/en/help-center/article/Introduction-to-Funding-Rate), [OKX](https://www.okx.com/help/perps-funding-fee-mechanism) and [Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding) (seen 24 Oct; exceptions exist, e.g. stock-linked TradFi at 0%). The house anchor is project owner's product decision and doesn't depend on this being universal.
- **How Binance computes it:** the displayed estimate uses a **trailing 8 h** premium window; the settled rate uses the period's time-weighted average. This general FAQ wording is not proof that every short-interval or TradFi contract updates identically.

## Research (plan §3.11; hypotheses, not evidence)
- No authoritative four-band standard was identified in the reviewed sources.
- **Candidate anchor-distance cut-offs** come from practitioner figures (~0.03% / ~0.05% / ≥0.1% per 8h, unverified). They are hypotheses for the 30-day pilot; the negative side may need asymmetric treatment.
- Show APR (vendor norm), labelled as simple annualisation.

## Edge cases and status
- `efr` missing → unavailable (never fall back to settled `fr` as if it were an estimate).
- Mark time older than 2 min → stale.
- Cumulative: incomplete coverage (expected vs held settlements), unknown `rateType`, or duplicates conflicting → unavailable.
- Special events excluded or shown separately.
- ADR-0041 supersedes the earlier historical-schedule wording: an interval change or a spacing gap makes the affected cumulative window unavailable. Shorter independently verified windows may remain available.

## Fixtures
1. 8H contract, 3 settlements per day: sum is correct.
2. 1H contract, 24 per day.
3. Missing one settlement → unavailable.
4. Special and Regular events at the same time.
5. 8h-equivalent of 1H 0.00125% = 0.01% → `anchor_distance` 0.
6. APR conversion.
7. Asymmetric caps: floor −0.005%, cap +0.02%, F = −0.005% → `at_floor`.
8. Unknown interval or cap → unsupported, not guessed.
9. Cash counter-example (10,000 / 20,000 notionals): the label stays "rate", with no cash figure.
10. Late event after the watermark; 1,001 same-time events across a page boundary; conflicting duplicates; unknown `rateType`.
11. Interval rollover inside the window; sparse new contract; exact `(T−W)` boundary (exclusive) and `T` (inclusive).
12. ADR-0041 R1–R9 replace the earlier historical-schedule prerequisite. Current two-source interval, latest anchor and spacing give provisional window consistency, not proof against systematic API omissions.

## Offline C0 implementation (ADR-0051)

`pulse/metrics/funding.py` implements R1–R9 from exact Decimal fractions, with eleven fixture families in `tests/test_funding_calculation.py`. ADR-0055 separates verification from retention: metadata must be fresh (120 s) when R2 passes; the report records `verified_at_ms`. A retained report is read through `settled_view` and does not expire merely because its old metadata is now older than 120 s. Each dependent value retains R6 expiry `t_L+h+15 min`; at the newly due cycle it is `settlement_pending`. Never substitute unverified stale metadata or overwrite prior valid verification just because a refresh failed. The 15-minute grace is not permission to pair an old settled operand with a new-cycle estimate. No operational archive audit is claimed. Unknown event types inside a dependent window refuse it (a conservative policy stricter than merely flagging them); Special events are excluded and counted. Adapters still must validate provider parsing, retrieval scope, page-tie completeness and instrument mapping before calling this module.
13. Newly due settlement overdue beyond the 15-min grace → affected window incomplete.

## Review
### peer reviewer round-2 review — 24 Oct 2026
1. **Must fix (R2-02):** keep project owner's +0.01% house anchor but do not claim it is universal per-contract neutrality. Metadata must establish interval, interest and signed cap/floor. Example F=−0.005% at floor=−0.005%, cap=+0.02% is at floor, while abs(F)>=cap is false. Use F>=cap or F<=floor with provider precision rules.
2. **Must fix (R2-03):** sum rates is not holder cash when notional changes. At 10k/20k notionals and 0.01% each: cumulative rate .02%, cash 3 USDT, not 2. Label it cumulative rate; APR is simple annualized current rate assuming constant rate, not forecast/realized return.
3. **Must fix (R2-08):** event completeness requires delayed-event overlap reconciliation, identity (symbol,time,type), interval-history evidence and bounded baseline age. Confirm unknown/missing event type policy. Never advance a global watermark past unverified per-symbol events.
4. **Fixtures to add:** interval rollover, native vs normalized units, asymmetric caps, unknown interval/cap, late/missing/duplicate-conflicting events, sparse new contract, exact window boundary, signed cash counterexample and mark-time proxy limitation. General trailing-8h FAQ wording is not evidence every short-interval/TradFi estimate follows identical update rules.
**Verdict:** corrections required before implementation.
**Not checked:** complete historical contract schedules or estimates. Sources: [funding FAQ](https://www.binance.com/en/support/faq/detail/360033525031), [TradFi FAQ](https://www.binance.com/en/support/faq/detail/1b88e951ee42414396b0c3a86ba4be4f).

- peer reviewer: M3 merged; doc review pending.

### peer reviewer round-3 review — 24 Oct 2026

1. **Resolved at specification level:** house-anchor distance, signed limits, native/normalized units, cumulative-rate vs cash and constant-rate APR. The documented counterexamples now address R2-02/03.
2. **Must fix (R3-03):** complete windows require historical schedules, not today's funding_frequency. A contract switching 8H→1H cannot have yesterday's expected events reconstructed from today's metadata. Specify unknown coverage/unavailable and preserve effective-dated interval evidence; add new-event overdue and schedule-unknown fixtures. The [proposed ADR-0021](../decisions/ADR-0021-admission-control-and-settlement-completeness.md) supplies the retrieval/coverage design requested in §8.5; it still needs peer reviewer review/project owner approval.
3. **Should fix:** do not make unknown interest/cap metadata suppress independent valid native rate fields. Use field-level unsupported states. The Why paragraph's cross-exchange interest claim needs its own supporting sources or narrower wording; the approved house anchor does not depend on that universal claim.

**Verdict:** arithmetic/label corrections supported; schedule completeness and bounded repair must be agreed before implementation.
**Not checked:** historical schedules, source raw probes, complete event ingestion, calibration or production state.
14. **ADR-0041 fixtures:** N = 24/168 (1H), 6/42 (4H), 3/21 (8H); missing middle event → unavailable; interval change in window → unavailable; jitter 5,000 ms accepted and 5,001 ms rejected; `t_L + h ≠ nextFundingTime` → `latest_unverified`; CoinDCX `fr` ≠ Binance latest → `latest_unverified`; interval sources disagree → unavailable; overdue beyond `h + 15 min` → expired; new listing with < N events → only that window unavailable.

## Pilot level bands (ADR-0116, 27 Oct 2026)
Magnitude = bps distance from the +0.01%/8h anchor (`pulse/metrics/levels.py`). Pilot cut-offs **2 / 4 / 9 bps**, inclusive lower bounds, source `pulse/wire/pilot_policy.py`. Informational, labelled pilot; replaced by the ADR-0015 calibration. Fixtures: `tests/test_pilot_policy.py`.
