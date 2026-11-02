# Funding change

27 Oct retained history (ADR-0126): chart points are consecutive verified native
settlement-rate differences, signed fractions displayed in bps. They are not
cumulative sums or archived live Current Delta estimates.

Display only (ADR-0125,27Oct): bps are rounded to exactly two decimals, with
rounded zero shown0.00bps. Underlying signed fractions, levels and calculations unchanged.

27 Oct window-meter amendment: ADR-0119 extends pilot classification to verified
settled1D/7D endpoint differences on `abs(d_W * 10000 * 8/h)`, using1/5/10bps.
No summation or division by W; these are differences of native settlement rates.
Signed values, verification, expiry and unclassified Current Delta are unchanged.
This overrides earlier "detail breakdown only" classification restrictions.

- Definition version: `funding_change.v1` (proposed) · ADRs: 0009, 0008, 0004, **0020 (accepted: levels on settled `d_last`)**, **0038 (bps display amendment)**, **0041 (settlement verification, Proposed)**
- Status: defined; round-2 corrections applied (R2-04, R2-05); awaiting peer reviewer re-review

## Formulas (two distinct metrics)

ADR-0055 retention correction: Current Delta uses a previously verified report
(`verified_at_ms` from fresh R2 verification) until its cycle/R6 bound; it does not
reapply120s freshness to the report's old metadata. The estimate itself must still
be fresh and match nft/h/mapping/fr. No estimate refresh extends settled expiry.

```text
T (observation cutoff) = source time of the current estimate observation (NOT the next funding time)

Estimated vs settled (Current Delta):
  d_cur   = F_est(T) − F_settled_last(T)
            where F_settled_last(T) = the latest Regular settlement with fundingTime ≤ T,
            same contract, same interval h, identity (symbol, fundingTime) agreed by CoinDCX fr and Binance history

Settled vs settled (Last-settlement / 1D / 7D Delta):
  d_last  = F_settled_last − F_settled_previous             (consecutive Regular settlements)
  d_W     = F_settled_last − F_settled_at_or_before(t_last − W),   W ∈ {1D, 7D}, t_last = time of F_settled_last

coverage (R3-03)  = d_last requires the two operands to be CONSECUTIVE: both Regular, spaced h ± 5 s, with h from two agreeing sources,
                    and the latest anchored to nextFundingTime and CoinDCX fr (ADR-0041 R1–R3, replaces the 'verified schedule' wording);
                    d_W requires N+1 events with every spacing h ± 5 s (R5)
expiry (R3-03)    = settled-delta status expires at expected_next_settlement + grace (grace = 15 min, accepted by project owner 24 Oct);
                    fresh efr/quotes never refresh it; status is independent of Current Delta's estimate status
display_bps      = 10000 × d  (signed; "absolute" = a difference, never a relative %; 0.010% → 0.015% = +0.5 bps; ADR-0038)

DEFINITIVE VALUES (ADR-0041, confirmed by project owner 25 Oct):
  current_delta_bps = 10000 × (F_est − F_settled_last)   F_est = CoinDCX efr (mark time ≤ 2 min); F_settled_last = rate(E_L) verified by R2;
                                                         signed; shown UNCLASSIFIED (ADR-0020)
  d_last_bps        = 10000 × (rate(E_L) − rate(E_{L−1}))   signed; E_L, E_{L−1} verified consecutive (R3); the L2 LEVEL / SORT / FILTER basis
  d_1D_bps, d_7D_bps = 10000 × (rate(E_L) − rate(E_{L−N}))  N = W/h; needs N+1 events with the R4 spacing rule; detail breakdown only
level_basis_bps  = |d_last_fraction × 10000 × 8/h|   (L2 levels on settled d_last; d_cur UNCLASSIFIED)
direction        = increasing | unchanged (d = 0 exactly) | decreasing
countdown        = time to next settlement, shown as context only
```

**Calibration (R2-05), decided (ADR-0020 option a):** settled history can't reconstruct past live estimates, so the tab's levels classify `d_last`.
- The tab's **headline, level, direction, filters and sort all use the same settled `d_last`** and are labelled as settled, with their `asof`.
- Current Delta is a separate, unclassified field, so a positive settled "Spiking" next to a negative live delta reads as two different measures, not a contradiction.

The options that were weighed:
- **(a)** classify the Funding Chg tab on the settled-vs-settled `d_last` (calibratable), and show Current Delta as an unclassified value; or
- **(b)** keep Current Delta unclassified (no L2 level) until enough real timestamped estimates have been collected.

Consecutive settlements are never used as training data for estimated-vs-settled.

## Why this way
- **Absolute, not relative** (project owner): relative % explodes near zero, is undefined from 0, and is odd at sign flips.
- **Two named metrics:** the estimate (live, trailing 8 h window) and settled-vs-settled (confirmed) answer different questions (peer reviewer P1 conditional).
- **No confidence wording from the countdown:** Binance's estimate uses a trailing 8 h window, so "strong near settlement" was unsupported. Corrected after the peer reviewer S7 review.

## Research (plan §3.11; hypotheses, not evidence)
- No authoritative convention was identified in the reviewed sources.
- **Corrected (R2-04):** the earlier claims ("most deltas are 0", "non-zero means the premium left the clamp band") were **wrong in both directions**:
  - .02% → .02% gives 0 even outside the band;
  - .02% → clamped .01% gives −.01% while inside the band.
- How often zeros occur is an **empirical** question for the pilot.
- **Candidate cut-offs** (multiples of the clamp width, ~0.01% / 0.05% / ≥0.1% at 8h-equivalent) are hypotheses only, to be calibrated on observed `d_last`.

## Edge cases and status
- Operands with different intervals (8H → 1H, etc.) → **unavailable** (`interval_changed`). A normalised comparison would be a separate, approved metric (peer reviewer M3).
- An operand from the wrong cycle (settlement identity disagrees between CoinDCX `fr` and Binance history around rollover) → unavailable until they agree.
- A stale estimate (mark time > 2 min) or a future operand → unavailable.
- Missing baseline settlement → unavailable.
- Special events are never used as operands.
- Exact zero is a valid "unchanged" value, distinct from unavailable.

## Fixtures
1. 0.010% → 0.015%: stored delta 0.00005, displayed +0.5 bps.
2. Sign flip 0.002% → −0.002%: stored delta −0.00004, displayed −0.4 bps.
3. 8H → 1H transition → unavailable.
4. Exact zero; equal out-of-band rates .02% → .02% = 0 (no clamp inference).
5. .02% settled → .01% estimate: stored delta −0.0001, displayed −1 bps.
6. Missing settlement; stale or future operand.
7. Just before and just after settlement, with provider lag (old cycle must not pair with new).
8. Old 7D baseline despite a matching interval; baseline at the 8-day retention edge.
9. Stored 04:00 and 20:00 with 12:00 missing (8H schedule) → not consecutive → `d_last` unavailable.
10. Next settlement overdue beyond grace → `d_last` level expires even while `efr` is fresh.
11. Unknown interval history → coverage unknown → unavailable.

## Review
### peer reviewer round-2 review — 24 Oct 2026
1. **Must fix (R2-04):** equal out-of-band rates .02%→.02% give d=0; old .02%→current clamped .01% gives d=−.01% while current is inside the band. Remove the claimed equivalence between nonzero delta and current clamp exit. Frequency of zeros is empirical, not established by the clamp alone.
2. **Must fix (R2-05):** define T=observation cutoff separately from next funding time, reject stale operands and require last-settlement identity at rollover. Same interval alone does not prove operands are from the correct cycle.
3. **Must fix:** Current Delta requires historical estimates for its calibration. Settled history trains a different metric. **Solution:** defer classified Current Delta or collect an authorized estimate pilot; don't manufacture research inputs.
4. **Fixtures to add:** counterexamples above, future/stale operand, just-before/after settlement with provider lag, old historical baseline despite matching interval, asymmetric sign/zero and different displayed precision. The 8-day retention rule supersedes the isolated 169th-event example.
**Verdict:** subtraction/pp units supported; structural inference and calibration contract require correction.
**Not checked:** actual estimate history, cycle rollover probes or thresholds.

- peer reviewer: P1 / M3 / S7 merged; doc review pending.

### peer reviewer round-3 review — 24 Oct 2026

1. **Must fix (R3-03):** d_last needs consecutive-event completeness independent of a current estimate; d_W needs an explicit schedule-backed bound on the at-or-before baseline. A missing 12:00 event between 04:00/20:00 produces the wrong delta; a nearly extra-day-old 7D baseline produces the wrong horizon. Separate settled expiry/coverage from the estimate's 2-min expiry; use proposed ADR-0021 and add missing-middle/overdue-new-event/unknown-schedule fixtures. Otherwise fresh quotes conceal stale or incorrect bands.
2. **Should fix:** Calibration still says project owner must choose; ADR-0020 already accepts (a). Label headline, direction, filters and sorting as settled-delta based, with separate unclassified Current Delta. Correct the summary plan's old Current Delta level basis.

**Verdict:** pp subtraction, corrected clamp interpretation and accepted settled calibration basis supported; event/baseline/expiry contracts remain incomplete.
**Not checked:** historical event intervals, live estimates, pilot distributions or fitted thresholds.
9. **ADR-0041 fixtures:** the missing-middle, interval-change, jitter-boundary (5,000 / 5,001 ms), `t_L + h ≠ nextFundingTime`, `fr` mismatch, interval-source-disagreement and overdue-expiry cases from ADR-0041; `d_last_bps` for 0.010% → 0.015% = +0.5; `current_delta_bps` negative case (settled 0.020%, estimate 0.012% → −0.8 bps); level basis `|d_last_bps × 8/h|` with h = 4 doubling the magnitude.

## Pilot level bands (ADR-0116, 27 Oct 2026)
Magnitude = settled `|d_last| x 10000 x 8/h` bps. Pilot cut-offs **1 / 5 / 10 bps**, inclusive lower bounds, source `pulse/wire/pilot_policy.py`. Current Delta stays unclassified. Fixtures: `tests/test_pilot_policy.py`.
