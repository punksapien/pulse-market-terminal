# Volatility score

28 Oct all-mode request reuse (ADR-0134, project owner approved): the unchanged v1
engine may reuse an exact validated hourly OI response within that response's
current native bucket. Candles/context retain the12min refresh schedule below;
no OI quantities/notional values are reconstructed, and old hourly buckets are
refetched rather than assigned a new source timestamp.

27 Oct retained history (ADR-0126): retain up to96 actual scorer outputs per
horizon, at their original input observation times. Do not rescore older candles
or fabricate earlier outputs to fill a chart. A chart needs two retained scores.

27Oct scheduling correction (ADR-0109): start refresh at12min inside the existing
15min model cadence, rather than the preview collector's former50min check.
The v1 scorer, evaluation order, inputs/lookbacks and level definitions are not
rewritten. Hosted completion time still requires measurement.

- Definition version: `volatility.v1` = today's `volatility-lightweight-v2` behaviour · ADR: 0013
- Status: v1 unchanged; closed-bar recompute **Proposed**

## Formula (existing; full detail in [pulse_metric_spec.md](../pulse_metric_spec.md) §1–4)

```text
YZ    = Yang–Zhang, 21 bars, scaled by sqrt(T/Δ)
EWMA  = λ 0.94 on close-to-close log returns (previous-return recursion)
base  = (0.65·YZ + 0.35·EWMA) / (0.65 + 0.35)
J     = (RV > 0) ? max(RV − BV, 0) / RV : 0         bipower jump share over ALL fetched finite returns
        < 10 returns → J non-finite (unavailable)
J_s   = clip(J, 0, 0.95)                              (score_cross_section fills missing J / components with 0 first)
score = base / sqrt(max(1 − γ·J_s, 0.05)), γ = 1
bands = annual 20 / 40 / 75 / 120 %, scaled by sqrt(T/Y), strict < boundaries; Low folds into Calm
```

Inputs: CoinDCX candles at 60 / 240 / 1D (verified identical to Binance), 90 / 120 / 365 days.

**v1 null handling (documented for parity, not endorsed):** `score_cross_section` fills missing estimator and jump values with 0 before scoring, so a finite score doesn't prove every component was present. This is kept as-is in v1 and is visible in fixtures.

## Why (and known limits kept in v1)
- **Unchanged,** so the screener rebuild doesn't also change a trading-facing number.
- **Known issues, each needing its own ADR to fix:**
  - the open bar is included (repaint);
  - `vol_momentum` is a log ratio shown as %;
  - window counts differ by resolution;
  - `vol_percentile_30d` isn't exactly 30 days;
  - no calibration provenance for λ, the 21-bar window, the weights or the bands.

## Proposed v2 (ADR-0013)
- **Recompute on closed bars only.** The open-bar estimate is deferred; if added later, it's labelled provisional.
- **Must also fix the history window (peer reviewer R2):** J uses *all* fetched returns, so a rolling fetch start can change J with no new bar. v2 must define a fixed scoring cutoff and lookback, and test that repeated recomputation at the same `asof` is identical.
- **Cost:** 18–36 ms per pair per timeframe; ~38 s of CPU for a full rebuild (network dominates).

## Fixtures (v1 parity against the current executable functions; known defects are not repaired)
1. YZ with pandas sample variance; the 21-bar warm-up (fewer bars → fallback path).
2. EWMA previous-return recursion (`V_t` uses `r_{t−1}`), seeded with `r_0²`.
3. RV = 0 (constant series) → J = 0; fewer than 10 returns → J non-finite → filled 0 in scoring.
4. J clip at 0 and 0.95 (max correction √20).
5. A component NaN → fill behaviour.
6. Insufficient bars → error row.
7. Exact band edges (strict `<`) for all three horizons; Low → Calm in the UI mapping.
8. All three resolution/horizon combinations on recorded candles: identical outputs before and after any refactor.

## Review
### peer reviewer round-2 review — 24 Oct 2026
1. **Must fix:** add explicit RV=0→J=0, insufficient-history/null-component handling matching v1. The displayed expression divides 0/0 for a constant-return series if implemented literally. Existing code also fills missing component/jump values before scoring; parity requires documenting those choices.
2. **Should fix:** fixtures must cover YZ sample variance, EWMA previous-return recursion, J=0/0.95 clipping, finite fallback, 21-bar warmup, exact bands with Low folded into Calm and all three horizons. Reference current executable functions; don't silently repair their known defects.
3. **Should fix:** closed-bar v2 needs a fixed asof/history-window policy because rolling the history can change J without another close.
**Verdict:** v1 preservation supported; calculation fixtures incomplete.
**Not checked:** new full numerical suite or v2 approval.

- peer reviewer (plan §8 P6): agrees with closed-bar via approval; defer the open-bar estimate.

### peer reviewer round-3 review — 24 Oct 2026

1. **Resolved at specification level:** constant-series RV=0, fewer-than-10 returns, missing-component fill behavior, YZ sample variance, previous-return EWMA, exact bands and all-three-horizon parity are now named fixtures. Do not silently fix known v1 defects while extracting modules.
2. **Should fix:** record full fixed candle inputs + expected executable outputs for each parity fixture before refactoring. A list of intended tests is not a passing suite: changing return indexing would still move EWMA with no new implementation evidence. Fixed asof/lookback remains required for proposed closed-bar v2; not authorized by v1 parity approval.

**Verdict:** v1 documentation/fixture scope is sufficient to begin implementing parity tests; v2 and release approval remain separate.
**Not checked:** newly executable numerical fixtures, actual parity outputs or closed-bar v2.
