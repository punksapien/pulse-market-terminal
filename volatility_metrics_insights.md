# Volatility Metrics: Exact Calculation, Interpretation, and Trading Use

## 1) What this framework measures

This framework is not trying to predict direction. It measures **how violent the price process is**, then converts that into a **risk score** that can be used for sizing, leverage caps, stop design, and strategy selection.

The uploaded code builds the score from four pieces:

- **Yang-Zhang volatility** on OHLC data
- **EWMA volatility** on close-to-close returns
- **24h rolling return volatility** (`bvol_window`)
- **Jump fraction** from a Barndorff-Nielsen / Shephard-style realized variance decomposition

It then blends the smooth-vol estimates and applies a jump correction to get a final **volatility_score**.

---

## 2) Exact calculation flow used in the code

### Step A: Load candle data
The code loads OHLCV candles, validates them, and extracts:

- `open`, `high`, `low`, `close`
- `volume`
- `ret_cc` = close-to-close returns from the loaded OHLCV frame

It also records data quality fields such as missing values, duplicate timestamps, flat bars, gaps, and a synthetic `quality_score`.

### Step B: Time scaling
The score is normalized to a user-selected horizon.

```text
S = sqrt(target_seconds / candle_seconds)
```

So if you are using 1m candles and a 24h horizon, the estimator is scaled from bar-level volatility to a 24h-like horizon. This matters because a 1-minute return series and a 1-hour return series are not comparable without scaling. Humans do love comparing unscaled numbers and then acting surprised.

### Step C: Core volatility estimators

#### 1. Yang-Zhang volatility
The code calls the imported Yang-Zhang estimator on OHLC data, then scales it by `S`.

Conceptually, Yang-Zhang combines:

- overnight / opening gap information
- intraday open-to-close movement
- high-low range information

That makes it better than a plain close-to-close standard deviation when candles contain meaningful gaps or intrabar movement.

#### 2. EWMA volatility
The code computes an exponentially weighted variance recursion:

```text
var[t] = λ * var[t-1] + (1 - λ) * r[t-1]^2
sigma[t] = sqrt(var[t])
```

with:

- `λ = 0.94` by default
- `r` = close-to-close returns
- final value scaled by `S`

This makes the estimate more reactive to recent shocks than a simple rolling standard deviation.

#### 3. 24h rolling volatility
A rolling standard deviation over a bar window equivalent to 24 hours:

```text
bars_per_window = round(24h / candle_seconds)
bvol_window = rolling_std(returns, bars_per_window) * S
```

This is a plain historical volatility check. It is simpler than Yang-Zhang and more interpretable than EWMA, but also more vulnerable to window choice.

#### 4. Jump fraction
The code computes:

```text
RV = sum(r^2)
BV = (π / 2) * sum(|r_t| * |r_{t-1}|)
jump_var = max(RV - BV, 0)
jump_fraction = jump_var / RV
```

Interpretation:

- `RV` = total realized variation
- `BV` = continuous, more diffusion-like part
- `jump_fraction` = share of movement that looks discontinuous

This is the most useful field when you care about liquidation spikes, stop-slippage, and regime breaks.

---

## 3) How the final score is built

The final score is not a raw volatility estimate. It is a **jump-adjusted blend**:

```text
sigma_base = (alpha * sigma_yz + beta * sigma_ewma) / (alpha + beta)
jump_correction = 1 / sqrt(max(1 - gamma * jump_fraction, 0.05))
volatility_score = sigma_base * jump_correction
```

Default weights in the file:

- `alpha = 0.65`
- `beta = 0.35`
- `gamma = 1.0`

Meaning:

- **Yang-Zhang** is the main anchor
- **EWMA** is the smoother recent-memory component
- **jump_fraction** increases the score when the market is behaving in a discontinuous, gap-heavy way

This is a sensible design if the purpose is **risk control**, not prettier charts.

---

## 4) Score bands and what they mean

The code defines annualized bands and scales them to the target horizon.

### Annual reference bands
- Quiet: `0.00 - 0.20`
- Stable: `0.20 - 0.40`
- Active: `0.40 - 0.75`
- High Risk: `0.75 - 1.20`
- Extreme Turbulence: `> 1.20`

The thresholds are scaled by:

```text
sqrt(target_seconds / SECONDS_PER_YEAR)
```

So the same instrument can move bands depending on whether you are evaluating a 24h horizon or a 7d horizon. That is correct. Volatility without a horizon is just theater.

### Practical meaning
| Band | Market meaning | Trading posture |
|---|---|---|
| Quiet | Tight ranges, low intraday shock frequency | Normal or slightly larger size, but do not confuse quiet with safe |
| Stable | Movement exists, but not chaotic | Normal risk budget, standard stops |
| Active | Tradeable but no longer cheap to hold risk | Reduce size moderately, check slippage |
| High Risk | Stops get hit often, ranges expand, execution worsens | Cut leverage, widen stops, demand stronger signal quality |
| Extreme Turbulence | Jump-dominated, unstable, liquidation-prone | Very small size or no trade unless the strategy is designed for this regime |

---

## 5) What each metric is useful for

### `sigma_yz`
Best for:
- candle-based risk estimation
- instruments with meaningful intraday range
- comparing assets on a consistent OHLC basis

What it tells you:
- how much the candle structure itself is moving
- whether the market is becoming range-heavy or gap-heavy

### `sigma_ewma`
Best for:
- short-horizon regime detection
- tracking whether the latest few bars are getting hotter

What it tells you:
- whether recent returns are clustering in a way that raises near-term risk

### `bvol_window`
Best for:
- a plain historical check
- confirming whether the regime is persistently volatile rather than just jumpy once

What it tells you:
- whether the last 24h window has structurally expanded

### `jump_fraction`
Best for:
- spotting liquidation cascades
- deciding whether tight stop logic is likely to fail
- separating smooth turbulence from discontinuous shock behavior

What it tells you:
- how much of the movement is likely coming from jumps, not diffusion

### `quality_score`
Best for:
- deciding how much trust to place in the volatility estimate

What it tells you:
- whether the underlying data is clean enough to trust the signal

A high volatility score with poor quality is not a strong signal. It is a warning label on bad data.

### `dollar_vol_24h`
Best for:
- liquidity filtering
- deciding whether the volatility is actually tradable

What it tells you:
- whether the market can absorb your size without turning your entries into self-inflicted slippage

---

## 6) Actionable use cases

## A. Position sizing
The cleanest use of volatility is inverse sizing.

A simple version:

```text
position_size ∝ target_risk / volatility_score
```

A more realistic version should also include:

- `quality_score`
- `dollar_vol_24h`
- `jump_fraction`
- stop distance
- strategy confidence
- execution venue constraints

### Practical rule
- **Higher volatility → smaller size**
- **Higher jump fraction → even smaller size**
- **Lower volume → smaller size again**
- **Lower quality score → haircut the size**

This is more honest than pretending the market cares about your preferred leverage.

## B. Leverage control
Volatility can be used as a **leverage ceiling**, not a permission slip.

A practical cap:

```text
leverage_cap = base_leverage * (target_vol / observed_vol)
```

Then haircut further if:
- `jump_fraction` is high
- `quality_score` is low
- `dollar_vol_24h` is weak
- spread / slippage is rising

### Example
If your strategy is calibrated for `0.04` volatility and the current `volatility_score` is `0.08`, then raw leverage should be about **half** of normal before liquidity penalties.

If `jump_fraction` is also elevated, cut further. Jumps are where “reasonable leverage” becomes an obituary.

## C. Stop-loss design
Volatility should change stop logic.

- **Low vol**: tighter stops can work, but noise still exists
- **High vol**: wider stops are needed, or you will be shaken out by random expansion
- **Extreme turbulence**: fixed stops may become meaningless because the market can traverse them instantly

A good stop should account for:
- volatility regime
- jump risk
- typical adverse excursion
- liquidity depth

## D. Strategy selection
Different strategies like different volatility regimes.

### Strategies that often like higher volatility
- breakout momentum
- trend continuation
- volatility expansion plays
- liquidation-cascade capture
- adaptive regime-following systems

### Strategies that usually dislike higher volatility
- mean reversion with tight stops
- market making without strong inventory control
- low-slippage scalps
- fixed-threshold spread systems

### Important caveat
A strategy may “like” volatility only if it has:
- robust execution
- good slippage handling
- enough sample size
- a real edge in that regime

Otherwise it just likes donating PnL to fees.

---

## 7) What can be reliably inferred from the data

### Reliable inferences
1. **Risk magnitude**
   The score tells you whether the instrument is calm, active, or dangerous.

2. **Execution quality pressure**
   High volatility plus low volume usually means worse fills and more slippage.

3. **Stop effectiveness**
   High jump fraction means stops are more likely to be skipped or overwhelmed.

4. **Regime change probability**
   Rising EWMA and widening 24h volatility often indicate a shift from stable to active or high-risk conditions.

5. **Strategy suitability**
   High volatility is a clue about which strategy family is even worth considering.

### Unreliable inferences
1. **Direction**
   Volatility does not tell you up or down.

2. **Next-candle return**
   High volatility does not guarantee continuation or reversal.

3. **Safety**
   A quiet market can still explode on news.

4. **Cause**
   Jump fraction detects discontinuity, not the reason behind it.

---

## 8) How to use high volatility without being reckless

### If volatility is high but tradable
Use:
- smaller size
- wider stops
- stricter entry filters
- shorter holding periods if the edge is intraday
- lower leverage than usual
- hard liquidity filters

### If volatility is high and jump fraction is high
Treat it as a **fragile regime**:
- avoid tight mean reversion
- avoid large leverage
- prefer confirmed momentum only if the strategy is built for it
- expect worse fills and more noise

### If volatility is high but volume is strong
This can be tradeable:
- the market has movement and liquidity
- but your edge still needs to survive the noise

### If volatility is high and volume is weak
This is the ugly one:
- large candles
- poor execution
- unreliable fills
- false breakouts
- nasty stop behavior

That is usually where traders confuse “opportunity” with “range expansion plus self-harm.”

---

## 9) A realistic example

Suppose an asset has:

- `sigma_yz = 0.06`
- `sigma_ewma = 0.08`
- `jump_fraction = 0.25`
- `alpha = 0.65`
- `beta = 0.35`
- `gamma = 1.0`

Then:

```text
sigma_base = 0.65 * 0.06 + 0.35 * 0.08 = 0.067
jump_correction = 1 / sqrt(1 - 0.25) ≈ 1.155
volatility_score ≈ 0.077
```

Interpretation:
- not just volatile, but jumpy
- size should be reduced
- leverage should be trimmed
- tight mean reversion should be avoided
- breakout logic becomes more plausible, but only with good liquidity and execution

---

## 10) Practical decision rules

### For sizing
- score low, volume high, quality high: normal size
- score moderate: reduce size
- score high: cut size aggressively
- score extreme: only trade if the strategy explicitly targets turbulence

### For leverage
- never let leverage rise simply because price is cheap
- anchor leverage to volatility, not nominal token price
- apply a jump penalty on top of volatility

### For confidence
- trust high volatility numbers only when quality is clean
- trust jump risk only when enough bars exist
- use cross-checks: `sigma_yz`, `sigma_ewma`, `bvol_window`, `jump_fraction`

If those disagree sharply, the market is usually transitioning, or the sample is messy.

---

## 11) Limits of this framework

This is a solid risk framework, but it is not magic.

It does **not**:
- predict direction
- detect news before price reacts
- guarantee leverage safety
- isolate the true microstructure cause of jumps
- replace execution analysis

It **does**:
- tell you when the market is becoming harder to hold
- help size down before pain starts
- help decide whether a strategy should even participate

---

## 12) Bottom line

Use volatility as a **risk governor**, not as a trading signal.

The right reading is:

- **low score** means the market may allow larger size, but not free lunch
- **high score** means reduce exposure, increase caution, and expect worse execution
- **high jump fraction** means the market is unstable in a way that punishes naive stops
- **high volume + high volatility** can be tradable
- **low volume + high volatility** is usually a trap
- **extreme turbulence** is only useful if the strategy is built for shock regimes

Volatility is not the enemy. Unscaled position size is.
