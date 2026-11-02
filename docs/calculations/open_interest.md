# Open interest

28Oct conditional capacity saving (ADR-0174): the350-pair Preview's own ring is
30060s slots after an identity-checked fenced in-place migration.1H/4H retain
own/E60 preference;24H uses the existing verified cold5m/E300 baseline, disclosing
cold-start mode, actual duration and error. Formula/tolerances/source unchanged;
no interpolation. Other scopes retain1536 slots. Missing cold history stays
unavailable until quota-admitted repair. Approved by project owner only when measured
headroom requires; observed114.8MB against115MB stop activates that condition.

27 Oct detail history (ADR-0126): charts show retained 5-minute **quantity** with
its own units and source times. They do not reconstruct historical OI returns.
The three current return formulas/tolerances below remain unchanged. Missing
cold baselines are repaired with bounded admitted historical requests.

26Oct adapter rule (ADR-0064, peer reviewer c6a8556): select an own-series baseline within
60s first; if none is eligible, select the independent cold-series baseline within
300s and label cold-start. Never choose an older own sample after an ambiguity.
An eligible own zero denominator/identity defect does not trigger opportunistic
substitution. Publish mode, both source times, actual duration and error per window.
The canonical quantity-return formula and tolerances below are unchanged.

26Oct numerical evaluation note(ADR-0047): `(current-baseline)/baseline` evaluates the same simple-return definition with better near-unchanged precision than dividing then subtracting1. Exact Fraction-based fixture and all existing zero/tolerance/overflow tests pass. Incoming source times and trustedclock share the ring's safe-integer ms ceiling; no metric source/units/window/threshold changes.

Implementation checkpoint (26Oct2026, separate C0 branch): pure quantity-return fixtures are documented in [C0 OI checkpoint](../c0_oi_calculation_checkpoint_2026-10-26.md) / [ADR-0046](../decisions/ADR-0046-offline-oi-calculation-fixtures.md). Existing definition below unchanged. Own-series baseline is selected from60s-step ring withE60s; cold-start baseline from a separate300s-step history ring withE300s. No interpolation, notional substitution or level calibration introduced. Other OI fields/live adapters remain pending.

- Definition version: `open_interest.v1` (proposed) · ADRs: 0010, 0004, 0003, 0012
- Status: defined; round-2 corrections applied (R2-12); cut-offs pending; awaiting peer reviewer re-review

## Formula

```text
OI(T)          = Binance GET /fapi/v1/openInterest → openInterest, T = response `time`
                 UNITS: base-asset units of the contract symbol (BTC for BTCUSDT, 1000PEPE for 1000PEPEUSDT, XAU for XAUUSDT)
change_W       = OI(T) / OI(baseline_W) − 1                W ∈ {1H, 4H, 24H}, follows the toggle
baseline_W     = nearest sample with time ≤ T − W and (T − W) − time ≤ E   (boundaries inclusive)
E              = 60 s (own 60-s series) | 300 s (cold start, from 5-min openInterestHist)
display        = 100 × change_W, in %; publish asof, baseline_asof, actual duration, error, mode (own | cold-start)
level_basis    = |change_W| on a per-window scale (scale per window is a calibration choice)
direction      = increasing | unchanged (exactly 0) | decreasing
OI level (USD) = reported: sumOpenInterestValue at ITS hist timestamp (label "as of <bucket>")
                 or derived current: OI(T) × mark(t_m), with |T − t_m| disclosed (no extra multiplier)
```

- **Units verified on 24 Oct (R2-12):** reported `sumOpenInterestValue / sumOpenInterest` = mark price within 0.3% (BTC 1.0009, 1000PEPE 0.9971, XAU 1.0000, DOGE 0.9994).
  - So quantity is in the symbol's base units (prefix included), and USD = qty × price **with no extra contract multiplier.**
  - Every mapped contract is unit-checked before USD is shown, **with like timestamps** (R3-07): compare `sumOpenInterestValue / sumOpenInterest` at bucket B with the mark (or 1-min close) at B, within ±60 s and a ±2% tolerance. A mismatch → USD unsupported. Four probes don't prove every contract.
  - Reported (historical) notional and the derived current estimate are never mixed under one `asof`.

## Why this way
- **Current OI every 60 s** costs ~21% of Binance weight and gives ~1–2 min freshness, against 5–10 min from history alone.
- **Quantity, not notional,** removes the price effect. **Simple %, not log,** because users read %. (ADR-0010)
- **Tolerance E is disclosed,** not hidden; approved by project owner.
  - E is a time-window error, **not** a bound on the OI-value error.
  - At 300 s cold start it can stretch a 1H window by up to 8.33% (24H: 0.347%). Mode and actual duration are published, and classification near band edges is checked for sensitivity.

## Research (plan §3.11; hypotheses, not evidence)
- No authoritative threshold standard was identified in the reviewed sources; Coinglass shows 1h/4h/24h change with no bands.
- **Separate scales per window** is a calibration choice. *(Correction: an earlier "variance grows with √window" justification was removed; price-volatility scaling doesn't establish OI behaviour, R2-06.)*
- **A minimum-OI floor** is a *proposed* eligibility rule, not an established one. It needs its own ADR if adopted.
- **Data caveat:** academic work finds some exchanges misquote OI (Giagkiozis & Said, Ledger 2024).
- **Calibration caveat (R2-05):** the 30-day pilot uses 5-min history as a proxy for the planned 60-s series. Overlap behaviour is validated before claiming the distributions transfer.

## Edge cases and status
- Baseline outside E, zero baseline, non-finite values → unavailable.
- Source time older than 15 min → stale.
- Future timestamps (beyond 5 s jitter) rejected.
- 5-min history is never relabelled as 1-min; no interpolation.
- New listing with history shorter than W → unavailable for that W.
- The legacy signal's OI input stays separate (ADR-0012).

## Fixtures
1. Exact baseline.
2. Baseline 59 s early (accept) vs 61 s (reject).
3. Cold start with 5-min history only.
4. Missed polls.
5. Delayed source time.
6. New listing.
7. 100 → 110 = +10% (not ln 1.1).
8. Current OI = 0 → valid level; as a denominator → unavailable.
9. Negative, NaN or infinite quantity → rejected; duplicate timestamp with conflicting values → unavailable.
10. Out-of-order current samples; error exactly 60 s and exactly 300 s (inclusive) vs 60.001 / 300.001 s.
11. USD units: qty 2 × mark 3 = 6 for a base-unit contract; unit-check mismatch → USD unsupported.
12. Mismatched times (hist bucket 12:00 vs mark 12:05) → no unit verdict; current USD stays unsupported until a contemporaneous check passes.
13. Exact zero change → direction `unchanged`, kept by filters and sorting.

## Review
### peer reviewer round-2 review — 24 Oct 2026
1. **Must fix (R2-12):** quantity×mark requires proven provider quantity units and any multiplier; mark and quantity also need compatible times. If quantity is already base units, don't multiply twice. Label reported historical notional separately from a current estimate; don't mix old notional and new quantity as one asof.
2. **Should fix:** add exact zero current OI, negative/NaN/infinite quantities, zero denominator, duplicate timestamp conflict, out-of-order current samples, 60/300-s inclusive boundaries and stale baseline coverage. Current OI=0 may be valid after closure even though denominator must be positive.
3. **Should fix:** source sample error tolerance is not a statistical error bound. At 1H, 300 s adds up to 8.33% to duration; at 24H, .347%. Floor eligibility and per-window scaling remain proposed research choices; sqrt(window) behavior for OI change is not established by price-volatility scaling.
**Verdict:** approved quantity/simple-return/tolerance policy supported; units and fixture gaps remain.
**Not checked:** true contract-unit metadata, mark synchronization, overlap distributions or fitted minimum-OI floor.

- peer reviewer: M2 merged; doc review pending.

### peer reviewer round-3 review — 24 Oct 2026

1. **Should fix (R3-07):** notional/quantity must be compared to a contemporaneous mark under explicit timing/numerical tolerances. Historical qty=100/notional=10,000 and a later mark=110 create 10% mismatch without a unit error. Keep current USD unsupported until the independent unit check is valid; four sampled successes are not all-contract evidence. Preserve base-unit handling without an extra multiplier.
2. **Should fix:** define exact zero change's direction (unchanged vs neither), and add mismatched mark-time, quantity-unit and unsupported-USD fixtures. Otherwise sorting/filtering can drop a valid unchanged row or display a false derived notional.
3. **Resolved at specification level:** simple-return math, cold-start duration/error disclosure, zero/nonfinite/out-of-order fixtures and the distinction between own-series data and a 5-min calibration proxy.

**Verdict:** canonical quantity return supported; notional unit/time evidence and unchanged semantics still need precision before implementation.
**Not checked:** raw four-contract probes, all-contract units, mark synchronization or 5-min/1-min overlap validation.

## Pilot level bands (ADR-0116, 27 Oct 2026)
Magnitude = `|quantity simple return|` per window. Pilot cut-offs 1H **1/3/6 %**, 4H **2/6/12 %**, 24H **5/15/30 %**, inclusive lower bounds, source `pulse/wire/pilot_policy.py`. Hypothesis (no published standard); peer reviewer's proposal under project owner's delegation. Fixtures: `tests/test_pilot_policy.py`.
