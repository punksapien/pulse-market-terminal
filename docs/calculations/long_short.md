# Long / Short ratio

28 Oct all-mode top polling follows the canonical15min cadence (ADR-0134,
project owner approved), rather than the earlier50-pair10min maintenance shortcut.
The existing bucket+35min expiry and common-bucket divergence are unchanged.

28 Oct all-pair scheduling amendment (ADR-0133, approved by project owner): native
1H/4H/24H headline observations are requested when their corresponding source
bucket advances, not all three every five minutes. Original values, bucket
timestamps and bucket+period+15min expiry remain unchanged. Five-minute global
history is separate. Admission and late-publication retries remain bounded.

27 Oct detail history (ADR-0126): chart series is the retained 5-minute global
accounts ratio, labelled independently of the native-period headline. Top-account
tips refresh in their own maintenance lane every 10 minutes; the existing bucket
+35-minute expiry remains unchanged. No model, source or tolerance change.

27 Oct headline amendment (ADR-0120, project owner selected native-period ratios):
the1H/4H/24H headline uses the latest Binance global account ratio requested with
period1h/4h/1d respectively. This is not a historic T-W value, mean of5m ratios
or share change. Each has its own provider bucket timestamp and expires at
bucket+period+15min. Fixed symmetric log-basis pilot cuts remain unchanged.
Five-minute global history/share evolution and top-account context below remain
independent detail measures. Equal period values are allowed when supplied.

Staging adapter checkpoint (26 Oct 2026): [ADR-0065](../decisions/ADR-0065-ls-shared-capsule-canary.md)
and [real1/3/5 evidence](../c1_ls_checkpoint_2026-10-26.md). Canonical math below
unchanged. Pure functions remain in `pulse/metrics/long_short.py`; admitted source
parsing in `pulse/collectors/binance_ls.py`, bounded capsule composition in
`pulse/store/ls_reader.py`. Levels remain uncalibrated. Operator canaries are not a
scheduled/hosted feed or UI integration; cold backfill exceeds55s invocation budget.

Offline implementation checkpoint (26 Oct 2026): [ADR-0050](../decisions/ADR-0050-offline-long-short-calculations.md) and [C0 evidence](../c0_long_short_checkpoint_2026-10-26.md). Pure accounts ratio/log basis, pp evolution and same-bucket comparison implement ADR-0007/0010/0022; no source or threshold change. One recorded four-decimal BTC payload is covered, not all-source precision validation. Runtime adapters, encodings, calibration and UI remain unwired.

- Definition version: `long_short.v1` (proposed) · ADRs: 0007, 0004, 0010 (baseline rule), 0015
- Status: defined; round-2 corrections applied (R2-07); cut-offs pending; awaiting peer reviewer re-review

## Formula

```text
ratio(T)        = longShortRatio from Binance globalLongShortAccountRatio, 5-min bucket T
share(T)        = longAccount (fraction of accounts net long); shortAccount likewise
level_basis     = |ln(ratio)|                         symmetric: 2.0x ≡ 0.5x
direction       = long_dominant (ratio>1) | short_dominant (ratio<1) | balanced
evolution_W     = 100 × (share(T) − share(baseline_W))  in pp, W ∈ {1H, 4H, 24H}
divergence      = 100 × (top_share(B) − global_share(B)) in pp, B = latest bucket both have, both within their expiry
```

- **Baseline:** nearest bucket at or before T−W, error ≤ 60 s from our series (≤ 5 min cold start), else unavailable (ADR-0010).
- **Top-trader series:** `topLongShortAccountRatio`. Binance's own docs define the cohort as the **top 20% of users by margin balance**, counted as net long or net short accounts ([Binance](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Long-Short-Account-Ratio)).
- **Expiry and cadence contract (R2-07):**
  - **Global:** polled every 5 min; expires at bucket time + 15 min.
  - **Top-trader:** polled every 15 min; Binance publishes about 4 min late, so a bucket+15 min expiry would go stale before the next poll every cycle (bucket 12:00 received 12:04 expires 12:15; next fetch 12:19).
  - Top-trader expiry is therefore **bucket time + 35 min** (15-min cadence + ~5-min publication lag + one missed-poll margin), **shown with its own age** in the detail view. Proposed; part of the peer reviewer re-review.
  - Divergence uses the latest bucket both series share, only if neither has expired.
- **Payload validation:**
  - `longAccount` and `shortAccount` must each be in [0, 1] and sum to 1 within rounding (±0.0002);
  - `ratio` must equal `long / short` within rounding (relative 0.5%).
  - Otherwise the value is `malformed` → unavailable. Never derive long dominance from the ratio and short dominance from the shares at once.
  - `short = 0` → ratio undefined → unavailable; a genuine 0 share is valid data.

## Why this way
- **Only Binance has *historical* data** for 1H/4H/24H. CoinDCX stats could be collected going forward, but has no backfill.
- **The ratio is lopsided,** so change is measured on share (pp).
- **Log space makes levels symmetric.**
- **Account counts ≠ exposure.** The ratio counts net-long vs net-short accounts; it says nothing about position sizes. *(Correction: an earlier "average short account is twice as large" inference was removed, R2-07.)* Labels say "accounts skew".

## Research (plan §3.11; hypotheses, not evidence)
- No authoritative four-band standard was identified in the reviewed sources.
- Practitioner extremes ~1.8–3.0 / ~0.65 (unverified).
- **Candidate anchors:** Neutral around 1.0, symmetric log bands.
- Calibrate on 30 days (ADR-0015).

## Edge cases and status
- `shortAccount = 0`, or a non-finite or ≤ 0 ratio → unavailable.
- **Expiry comes from one per-series lookup** (R3-04): global = bucket + 15 min; top-trader = bucket + 35 min (**ADR-0022, accepted**), always shown with its own age. Divergence uses the latest common bucket only if it is unexpired under *both* series' rules.
- Top-trader and global series are compared only at the same bucket time.

## Fixtures
1. `0.60 → 0.66 = +6 pp`.
2. Ratio 2.0 and 0.5 give equal `level_basis`.
3. Missing baseline → unavailable.
4. Mismatched bucket times → no divergence.
5. Zero short accounts → unavailable.
6. Top bucket 12:00 received 12:04: still valid at 12:19 (expiry 12:35); divergence computed with the shown age.
7. Inconsistent payload (ratio 2, shares .40 / .60) → malformed, unavailable.
8. Shares summing to 1.01 → malformed.
9. Both series stale → no divergence.
10. Exact expiry boundaries (global bucket + 15 min, top-trader bucket + 35 min, each inclusive/exclusive as specified).
11. One series expired → no divergence.
12. Zero and near-zero shares at source rounding.

**The validation tolerances (±0.0002 sum, 0.5% ratio) are house rules.** Confirm them against the observed source precision (decimal places in recorded payloads) before implementation.

## Review
### peer reviewer round-2 review — 24 Oct 2026
1. **Must fix (R2-07):** top series expires at 15 min although polling is 15 min plus upstream publication lag. Fixture: bucket 12:00 received 12:04 expires 12:15 before next 12:19 fetch. Define explicit top expiry/cadence rather than assuming uninterrupted availability.
2. **Must fix:** validate share range/sum and ratio-vs-shares consistency within source rounding. Example ratio=2 with shares .40/.60 must not produce long dominance from ratio and short dominance from share simultaneously. Distinguish malformed payload, genuine zero and undefined ratios.
3. **Should fix:** remove average gross-size inference from account counts; official definition is a net-account cohort, not a position-size statistic. Binance confirms top 20% by margin balance in its own docs. Specify earliest allowed common bucket and its own expiry; don't compute divergence on two stale matched buckets.
**Verdict:** pp/log-magnitude formulas supported; timing/validation corrections required.
**Not checked:** raw auth/rounding observations or calibration. Source: [Binance top accounts](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Long-Short-Account-Ratio).

- peer reviewer: P2/P3 agreed in plan §8; doc review pending.

### peer reviewer round-3 review — 24 Oct 2026

1. **Must fix (R3-04):** Edge cases says bucket+15min while top cadence section says +35min. At 12:20, top bucket 12:00 changes validity depending on paragraph. Smallest fix: one per-series TTL table reused by value and common-bucket divergence; record the proposed TTL amendment for project owner before implementation. Without it table/detail can disagree.
2. **Resolved at specification level:** account-vs-exposure wording and share/ratio validation. Add exact expiry boundary, one-series-expired divergence and zero/near-zero rounding fixtures; numerical tolerance is a house validation rule and needs observed source-precision evidence, not independent live stress.

**Verdict:** formulas and corrected validation/cohort meaning supported; expiry policy remains internally inconsistent and pending approval.
**Not checked:** raw rounding/latency probes, approved 35-min TTL or calibrated bands.

## Pilot level bands (ADR-0116, 27 Oct 2026)
Magnitude = `|ln(global accounts ratio)|`. Pilot cut-offs at ratio **1.25x / 1.8x / 3.0x** either side (same band for r and 1/r), inclusive lower bounds, source `pulse/wire/pilot_policy.py`. peer reviewer's proposal under project owner's delegation. Fixtures: `tests/test_pilot_policy.py`.
