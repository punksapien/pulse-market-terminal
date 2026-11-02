# VOL SIGNAL — Complete Product Build Specification
### Volatility-Driven Trading Discovery Tool for CoinDCX
### GODMODE Blueprint — Engineering + Design + Signal Engine

---

## DOCUMENT PURPOSE

This is not a requirements list. This is a complete, implementation-ready specification that covers every screen, every state, every computed field, every design token, every interaction, and every data contract. A developer reading this document should be able to build the full product without asking a single clarifying question.

---

## PART 1 — PRODUCT VISION

### 1.1 The Single Problem Being Solved

A CoinDCX day trader opens their phone at 9:00 AM. They have three questions:
1. Which tokens have tradeable movement right now?
2. Is that movement building or fading?
3. What do I actually do — long, short, fade, wait?

No existing tool answers all three in under 30 seconds. Bloomberg Terminal answers question 1 if you already know what you're looking for. TradingView answers nothing without significant setup. The existing vol dashboard answers question 1 partially, but only if the trader already understands vol metrics.

**VOL SIGNAL answers all three in 15 seconds, on mobile, with no configuration.**

### 1.2 What Success Looks Like

A trader opens the app. The top 3 cards immediately tell them:
- CGPT is Hot, vol Surging, signal Momentum Long — OI confirms
- INJ is Active, vol Falling, signal Range — wait
- BTC is Extreme, vol Surging, signal Breakout Watch — compression breaking

They tap CGPT. The drilldown shows the full vol breakdown, the specific reasons, the forward outlook, and a Trade button. Total time: 12 seconds from open to trade intent.

**That is the product.**

### 1.3 What This Tool Is Not

- Not a charting tool (TradingView exists)
- Not a portfolio tracker (Coin management exists)
- Not a general screener (filters-first tools exist)
- Not a volatility education platform

It is a **daily-use, mobile-first, vol-driven opportunity surface** that converts research into action.

---

## PART 2 — USER UNDERSTANDING

### 2.1 Primary User: Active Day Trader

**Context:** Trades 3-8 times per day on CoinDCX futures. Understands basic technical analysis. Does not have quant background. Has seen enough markets to know that "high vol = opportunity" but doesn't know which opportunity or why.

**Device:** Primarily Android mobile (85% of CoinDCX user base). Uses app between 8:30-10:00 AM and 2:00-4:00 PM when Indian market session overlaps with Asian close / European open.

**Time budget:** 15-30 seconds to decide if a token is worth trading. Another 60-90 seconds if they want full context.

**Mental model:** "What's moving?" → "Why is it moving?" → "Which way do I trade?" → "Execute." They do not think in vol bands or EWMA. They think in "hot", "cooling down", "about to pop."

**Fears:** Missing the move. Getting trapped in a dead market. Trading in a vol spike that immediately reverses. Being liquidated on a position that looked clean but had hidden fragility.

### 2.2 Secondary User: Desk Researcher / Analyst

**Context:** Prepares morning briefings for trading teams. Needs the quantitative summary for the top 10 tokens every morning. Currently does this manually.

**Need:** The same discovery surface, but with the ability to export or share specific token drilldowns. Not the primary UX focus but data completeness matters for this user.

### 2.3 Tertiary User: Equity Research Team (Content Producer)

**Context:** Produces "why" content — the fundamental and macro layer behind price movements. They need a simple interface (probably a CMS or admin panel, not this app) to inject reason text into specific tokens.

**Note:** This spec covers the consumer side. The admin/CMS side is a separate build.

---

## PART 3 — INFORMATION ARCHITECTURE

### 3.1 The Hierarchy Problem With The Old Tool

Old tool information hierarchy (flat, no priority):
```
Filter Sidebar = Risk Ranking = Instrument Path = Market Table = Drilldown
```

All panels had equal visual weight. User had to decide where to start. This is a terminal for analysts, not a discovery tool for traders.

### 3.2 New Information Hierarchy (Strict Priority Stack)

```
Layer 0: CONTEXT BAR — market-level pulse (1 line)
Layer 1: OPPORTUNITY LIST — ranked, filtered token cards (primary surface)
Layer 2: INTENT FILTERS — regime + direction chips (narrows layer 1)
Layer 3: TOKEN DRILLDOWN — full picture for selected token (on demand)
Layer 4: METHODOLOGY FOOTER — formula transparency (always accessible)
```

Every design decision must respect this hierarchy. Layer 1 is always dominant.

### 3.3 Navigation Model

There is **no navigation**. This is a single-screen application with one modal state. The discovery list is the entire product. The drilldown is a bottom sheet overlay. There are no pages, no tabs, no sidebar.

The only persistent navigation element is the app header with a search icon (to find a specific token by name) and a notification bell (for vol regime alerts).

---

## PART 4 — SCREEN SPECIFICATION

### 4.1 APP HEADER

**Height:** 52px fixed
**Position:** Sticky top, always visible

**Left:** "VOL SIGNAL" wordmark in monospace display font. Below it in 10px: "CoinDCX Futures · Updated 2m ago" with a pulsing green dot when live.

**Center:** Nothing. Let the wordmark breathe.

**Right:** Two icons — search (magnifying glass) and notification bell with badge count for unread vol alerts.

**Behavior:**
- "Updated Xm ago" counts up from last data refresh
- Dot pulses green when data is <2 min old, amber when 2-5 min, red when >5 min
- On tap of notification bell: bottom sheet shows recent vol regime change alerts (token + old regime → new regime + timestamp)

---

### 4.2 CONTEXT BAR

**Height:** 44px
**Position:** Below header, sticky

**Content:** One line that reads the current market-wide pulse. Example:
```
🔥 Market: 14 tokens HOT or above · Vol expanding in 68% of tracked pairs · BTC vol at 3-month high
```

Or in quiet conditions:
```
😴 Market: Mostly Calm · 3 tokens active · Consider waiting for regime shift
```

**How it's computed:**
- Count tokens in each regime band
- If Hot+Extreme > 30% of universe: "Active market session"
- If Surging vol direction > 40% of universe: "Vol expanding broadly"
- Pull the highest vol_score token for the BTC/ETH reference
- Compare current market_avg_vol_score to 30d average: express as percentile

**Design:** Single line, muted background (slightly lighter than page bg), left-padded 16px. Scrolls away on scroll down, reappears on scroll up.

---

### 4.3 INTENT FILTER STRIP

**Position:** Below context bar, sticky on scroll
**Layout:** Two rows of horizontally scrollable chips. No dropdowns. No inputs. No sliders.

**Row 1 — Regime (What's happening now):**
```
[ALL] [😴 Calm] [🟢 Low] [🔶 Active] [🔥 Hot] [💀 Extreme]
```

**Row 2 — Vol Direction (Where is it going):**
```
[ALL] [⚡ Surging] [↑ Rising] [→ Stable] [↓ Falling] [🌀 Contracting]
```

**Chip behavior:**
- Default state: ALL selected in both rows
- Single select per row (not multi-select — this is intent, not filtering)
- When a regime chip is selected, the token list filters to only that regime
- When a direction chip is selected, it further narrows
- Chips show count in parentheses: `🔥 Hot (7)` so user knows before tapping
- Smooth horizontal scroll with no scrollbar visible
- Selected chip has filled background + bold text; unselected has border only

**Row 3 (collapsed by default, expand icon on right) — Advanced:**
```
[Sort: Vol Score ↓] [Min Liq: $500K] [Min Bars: 50]
```
These are the power-user parameters hidden by default. Tapping each opens a mini bottom sheet picker. The key design decision: advanced filters are subordinate to intent. Most users never need them.

---

### 4.4 TOKEN CARD — COMPLETE SPECIFICATION

This is the most important component. It must answer all four trader questions without requiring a tap.

**Card height:** ~112px (compact but complete)
**Width:** Full width - 32px horizontal margin

**Card anatomy — exact layout:**

```
┌─────────────────────────────────────────────────────┐
│ [SYMBOL] [RegimeChip]          $0.8423  +12.4% ▲    │
│ CGPT/USDT  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │  ← vol bar
│                                                       │
│ BVOL 0.087  ⚡ Surging    [sparkline 24pts]           │
│ OI +8.3%  Fund 0.041%  Vol 2.4× avg                  │
│                                                       │
│ OI building fast + vol expanding vs 7d avg  [M↑]     │
└─────────────────────────────────────────────────────┘
```

**Row 1 — Identity + Price:**
- Left: Symbol in bold (e.g., "CGPT") in display monospace, 15px
- Immediately right of symbol: RegimeChip (see chip spec below)
- Far right: Current price in 14px + 24h % change in 13px, colored green/red, with tiny arrow
- Price and % change right-aligned

**Vol Bar (thin strip below row 1):**
- Full-width colored bar, height 3px
- Color and fill length both encode regime:
  - Calm: 10% fill, muted gray
  - Low: 25% fill, muted blue
  - Active: 50% fill, amber
  - Hot: 75% fill, orange
  - Extreme: 100% fill, red (pulsing animation)
- This gives instant visual scanning — the eye processes color and length in parallel

**Row 2 — Vol Metrics + Sparkline:**
- Left: "BVOL [value]" in monospace — the headline number, 13px
- Middle: Vol direction chip (see direction chip spec) — ⚡ Surging / ↑ Rising / → Stable / ↓ Falling / 🌀 Contracting
- Right: 24-point sparkline of vol_score over last 24h, 60px wide × 24px tall
  - Color of sparkline line matches current regime color
  - No axes, no labels — just the shape
  - Sparkline shape is the most important forward signal at a glance

**Row 3 — Supporting Metrics:**
- "OI [±%]" — 24h OI change, green if positive, red if negative
- "Fund [value]%" — current funding rate, neutral/amber/red depending on extreme-ness
- "Vol [N]× avg" — current 24h vol vs 30d average, highlighted if >2x

These three metrics are the "confirmation layer". They tell the trader whether the vol move has depth behind it.

**Row 4 — Rationale + Signal:**
- Left: One-line computed rationale string (14px, muted but readable). Max 55 characters. Ellipsis if longer. Examples:
  - "OI building + vol expanding vs 7d avg"
  - "Funding crowded + RSI oversold = caution"
  - "Price coiling near key level, vol compressing"
  - "Liquidation cascade, vol spike = fade candidate"
- Far right: TradeSignal chip (see trade signal spec)

**Card tap behavior:**
- Entire card is tappable
- Light haptic on tap (mobile)
- Opens drilldown bottom sheet (see 4.5)
- Card stays visible behind the bottom sheet with a slight blur/dim overlay

**Card animation on load:**
- Staggered entrance: cards enter from bottom with 50ms delay between each
- On data refresh: changed values flash briefly (green for improved, amber for worsened)
- Extreme regime cards: subtle red border pulse animation (1.5s period, low opacity)

---

### 4.4.1 REGIME CHIP SPEC

Used inside token cards and filter strip.

| Regime | Label | Background | Text | Border | Icon |
|---|---|---|---|---|---|
| Calm | CALM | #1a1a1a | #6b7280 | #374151 | 😴 |
| Low | LOW | #0f1a0f | #22c55e | #166534 | 🟢 |
| Active | ACTIVE | #1c1500 | #f59e0b | #92400e | 🔶 |
| Hot | HOT | #1c0a00 | #f97316 | #9a3412 | 🔥 |
| Extreme | EXTREME | #1a0000 | #ef4444 | #7f1d1d | 💀 |

Font: monospace, 10px, letter-spacing 0.08em, uppercase. Pill shape, 4px border-radius, 4px vertical padding, 8px horizontal padding.

---

### 4.4.2 DIRECTION CHIP SPEC

| Direction | Label | Color | Icon | Meaning |
|---|---|---|---|---|
| Surging | SURGING | #f97316 orange | ⚡ | Vol accelerating hard — top 10% expansion rate |
| Rising | RISING | #f59e0b amber | ↑ | Vol above recent average and trending up |
| Stable | STABLE | #6b7280 gray | → | Vol within 10% of 7d average |
| Falling | FALLING | #3b82f6 blue | ↓ | Vol declining from recent elevated level |
| Contracting | CONTRACTING | #8b5cf6 purple | 🌀 | Vol compressing below historical median = setup forming |

Direction is the most important single piece of information for timing. "Hot + Contracting" means something completely different from "Hot + Surging" — the former is a setup, the latter is a trade already in progress.

---

### 4.4.3 TRADE SIGNAL CHIP SPEC

| Signal | Short Label | Full Label | Color | Logic |
|---|---|---|---|---|
| Momentum Long | M↑ | MOMENTUM LONG | #22c55e green | Price up + vol surging + OI rising + jump_frac low |
| Momentum Short | M↓ | MOMENTUM SHORT | #ef4444 red | Price down + vol surging + OI rising + jump_frac low |
| Reversal Long | R↑ | REVERSAL LONG | #06b6d4 cyan | Price extreme low + vol spike + OI falling + funding negative |
| Reversal Short | R↓ | REVERSAL SHORT | #f43f5e pink | Price extreme high + vol spike + OI falling + funding extreme positive |
| Breakout Watch | BW | BREAKOUT WATCH | #a855f7 purple | Vol contracting + OI building + price tightening = setup, not trigger yet |
| Range | RG | RANGE | #6b7280 gray | Vol stable/falling + price oscillating + neutral funding |
| Avoid | AV | AVOID | #374151 dark | Low quality + extreme turbulence + low liquidity |

These chips appear both on the card (short 2-char label) and in the drilldown (full label + explanation).

---

### 4.5 DRILLDOWN MODAL — COMPLETE SPECIFICATION

**Trigger:** Tap any token card
**Type:** Bottom sheet, slides up from bottom with spring physics
**Handle:** Pill drag handle at top center
**Max height:** 92vh (allows ~8% of card list to show behind, confirming context)
**Scroll:** Internal scroll within the sheet

**Sections within the drilldown (top to bottom):**

---

#### SECTION A: IDENTITY HEADER

```
[CGPT]  CognitiveCraft Token  USDT Perpetual
$0.8423   +12.4% ▲   [🔥 HOT]  [⚡ SURGING]   ●LIVE
```

Left column: Symbol (large, 24px monospace bold), full name (12px muted), pair name
Price line: current price + 24h change + regime chip + direction chip
Right: live indicator (pulsing dot)

---

#### SECTION B: VOL SCORE PANEL

**Headline:** The single most important number, displayed prominently.

```
╔══════════════════════════════════════╗
║  VOL SCORE                           ║
║  0.087            24h horizon        ║
║  ████████████████░░░░░  [HOT BAND]   ║
╚══════════════════════════════════════╝
```

Below the headline, a horizontal band gauge showing where this score sits on the Calm→Extreme spectrum, with the current score marked. Bands are color-coded. The score marker has a glow in the band's color.

---

#### SECTION C: VOL COMPONENTS BREAKDOWN

Collapsible section (open by default for power users, collapsed for casual). Title: "How the score is built."

```
Component          Value    Weight    Contribution
──────────────────────────────────────────────
Yang-Zhang σ_yz    0.071    65%      0.046
EWMA σ_ewma        0.081    35%      0.028
── Blended base ──────────────────── 0.074
Jump fraction      0.23     ×1.15    × correction
── Final score ────────────────────── 0.087
```

Design: monospace table, muted colors, each row with a subtle left border in the component's associated color.

Below the table, a 4-quadrant mini radar/bar chart:
- σ_yz bar (blue)
- σ_ewma bar (amber)
- bvol_window bar (green)
- jump_fraction bar (red)

Each bar shows the component's current value relative to its own 30d range (0-100% of range). This lets the trader immediately see "is everything elevated, or just one component?"

---

#### SECTION D: SECONDARY METRICS GRID

2×3 grid of metric tiles:

```
┌─────────────┬─────────────┬─────────────┐
│  OI 24h     │  Funding    │  Vol/Avg    │
│  +8.3% ▲    │  0.041%     │  2.4×       │
│  Bullish    │  Neutral    │  Elevated   │
├─────────────┼─────────────┼─────────────┤
│  Liq 24h    │  Quality    │  Bars       │
│  $2.8M      │  0.91 ✓     │  1,440      │
│  Adequate   │  Clean      │  24h full   │
└─────────────┴─────────────┴─────────────┘
```

Each tile: metric name (10px muted), value (16px bold), interpretation label (10px, colored).

OI interpretation:
- >+10%: "Bullish momentum" (green)
- +2-10%: "Building" (amber)
- -2 to +2%: "Neutral" (gray)
- <-2%: "Unwinding" (red)

Funding interpretation:
- >0.08%: "Crowded long — risk" (red)
- 0.02-0.08%: "Mildly long" (amber)
- -0.02 to 0.02%: "Neutral" (gray)
- <-0.02%: "Crowded short" (cyan)
- <-0.08%: "Crowded short — risk" (red)

Vol/Avg interpretation:
- >3×: "Extreme activity" (red)
- 2-3×: "Elevated" (amber)
- 1-2×: "Normal" (gray)
- <1×: "Quiet" (blue)

Quality score interpretation:
- >0.85: "Clean ✓" (green)
- 0.70-0.85: "Acceptable" (amber)
- <0.70: "Degraded ⚠" (red) — with tooltip explaining what's wrong (gaps, flat bars, etc.)

---

#### SECTION E: VOL SPARKLINE + HISTORY

Full-width 180px tall chart. X axis: last 24 hours in 1h buckets. Y axis: vol_score.

Three overlaid lines:
- White/bright: current 24h vol_score (main line, thicker)
- Dim blue: 7d average vol band (upper and lower, filled between = the "normal range")
- Dim amber: 30d average as a single horizontal reference line

Colored background zones behind the chart marking band transitions (Calm=charcoal, Active=amber/5%, Hot=orange/5%, Extreme=red/5%).

Small event markers on the timeline: if jump_fraction spiked >0.35 at any point, mark with a ⚡ icon on the x-axis.

This answers: "Has this been volatile all day, or did it just spike?"

---

#### SECTION F: WHY IS THIS HAPPENING — THE RATIONALE ENGINE

Title: "Why this vol?"

This is the most important section for building trader conviction. Three sub-layers:

**F1: Quantitative Signals (Always present, auto-computed)**

Bullet list of 3-5 computed reasons. Each reason has:
- A signal icon (📊 for OI, 💰 for funding, 📈 for price, 🌊 for vol trend, ⚡ for jump)
- A plain-language statement
- A short confidence indicator (● = strong, ◐ = moderate, ○ = weak)

Examples of computed reason strings:

```
📊 OI rising +8.3% while price rises = directional conviction ●
💰 Funding neutral = longs not crowded yet, room to run ●
🌊 24h vol is 2.4× 7d average = regime expansion in progress ●
📈 Price momentum strong, no sign of distribution ◐
⚡ Jump fraction low (0.08) = smooth trend, stops likely safe ●
```

```
📊 OI falling while price rises = warning, divergence ●
💰 Funding at 0.09% = longs crowded, squeeze risk rising ●
🌊 Vol spike without OI = possible fake move ●
⚡ Jump fraction 0.45 = cascade-driven, reversal risk high ●
```

**Reason generation logic (full spec in Part 6):** A deterministic rule engine maps combinations of metric states to reason strings with confidence levels. No ML, no ambiguity — a lookup table with ~60 rules covering all major signal combinations.

**F2: Technical Signals (Computed from price action, conditionally present)**

Shows only if a meaningful signal exists. Possible entries:
```
📉 RSI approaching oversold (32) — reversal zone near
📊 Price within 1.2% of 30d resistance — breakout or rejection
🔒 Price tightening over last 6h while vol compressed — breakout setup forming
```

Computed from: RSI, ATR, distance from key levels (derived from rolling OHLC analysis). Simple enough to run client-side or in a lightweight backend function.

**F3: Research Team Layer (injected, conditionally present)**

If the equity research team has tagged this token today, show a "Research Note" card:

```
┌───────────────────────────────────────────────┐
│ 📝 Research Note — Today                      │
│ CGPT elevated due to new Binance listing      │
│ announced 6h ago. Vol likely to sustain for   │
│ 24-48h post-announcement.                     │
│                     — CoinDCX Research, 9:15AM│
└───────────────────────────────────────────────┘
```

If no research note exists: this section is entirely absent. No "No notes available" placeholder. Absence is clean.

---

#### SECTION G: FORWARD SIGNAL — VOL OUTLOOK

This is the answer to "where is vol heading?"

```
╔════════════════════════════════════════════════╗
║  VOL OUTLOOK         LIKELY TO PERSIST         ║
║                                                ║
║  OI is rising with vol → participation real    ║
║  Funding neutral → no crowding to unwind       ║
║  Vol trend: expanding across 1h, 4h, 24h      ║
║                                                ║
║  Confidence: HIGH  ████████████░░░░  78%       ║
╚════════════════════════════════════════════════╝
```

Three possible outlook states:

**LIKELY TO PERSIST** (green header)
- Conditions: OI rising + vol rising + funding neutral/moderate + jump_frac low
- Confidence: 60-90% based on how many conditions align

**LIKELY TO FADE** (amber header)
- Conditions: vol spike + OI flat/falling + funding extreme + jump_frac high
- Confidence: 60-90%

**COMPRESSION — WATCH FOR BREAKOUT** (purple header)
- Conditions: vol contracting + OI building + price tightening + quality clean
- Confidence: 50-80%
- Note: "This is a setup, not a trade yet. Trigger: first vol expansion bar."

**Outlook computation logic (full in Part 6):** A weighted scoring model that combines 8 signals into a 0-100 persistence probability, then thresholds into the three categories.

---

#### SECTION H: TRADE SIGNAL PANEL

```
┌───────────────────────────────────────┐
│  TRADE SIGNAL                         │
│                                       │
│  ⬆ MOMENTUM LONG                     │
│                                       │
│  Best when: Entry on pullback to      │
│  vol support. Wider stop = BVOL×1.5.  │
│  Avoid tight stops in HOT regime.     │
│                                       │
│  Signal Readiness: 84/100 ████████░░  │
└───────────────────────────────────────┘
```

The signal readiness score (0-100) is computed from the readiness engine (Part 6.5). It is NOT a win rate. It is a measure of how well the current conditions align with the ideal entry conditions for this signal type.

**90-100:** All conditions aligned. Highest conviction.
**75-89:** Most conditions aligned. Valid entry.
**60-74:** Mixed signals. Reduce size.
**<60:** Signal present but weak conditions. Wait or skip.

---

#### SECTION I: SIZING GUIDANCE

Not financial advice. Not a recommendation. A mechanical output of the vol framework.

```
Risk Parameters (based on your account)
─────────────────────────────────────────
Regime penalty:      HOT → 0.75× base size
Jump penalty:        Low (0.08) → 1.0× 
Quality:             Clean → 1.0×
Liquidity:           $2.8M → 1.0×
─────────────────────────────────────────
Combined modifier:   0.75×
─────────────────────────────────────────
Suggested stop:      BVOL × 1.5 = 0.131
Leverage cap:        base × (target_vol / 0.087)
```

Display note at bottom: "These are mechanical vol-derived parameters. Not financial advice. Your risk tolerance and strategy edge determine actual sizing."

This section is collapsed by default with a "⚙ Size Guidance" expand button. It does not lead with numbers — it leads with a toggle. The user opts in to seeing it.

---

#### SECTION J: TRADE BUTTON

```
┌──────────────────────────────────────┐
│                                      │
│   Trade CGPT on CoinDCX Futures →   │
│                                      │
└──────────────────────────────────────┘
```

Full-width, 52px tall, the regime color as background (e.g., orange for HOT).

**Behavior:**
- On tap: deep link to CoinDCX futures trading screen for this pair
- Mobile: `coindcx://futures/trade/CGPT-USDT`
- Web: `/trade/futures/CGPT_USDT`
- The token and direction are pre-filled if the exchange app supports it

**Design:** This is the only prominent call-to-action in the entire product. Everything else is information. This button is the one place the design is allowed to be loud.

---

#### SECTION K: METHODOLOGY FOOTER (in drilldown)

Expandable at the bottom of the drilldown sheet:

```
▾ How VOL SCORE is calculated

BVOL 24H = σ(ln returns) × √525,600
Candle: 1-minute OHLCV
Window: Rolling 1,440 bars (24 hours)
Components: Yang-Zhang σ_yz (65%) + EWMA σ_ewma (35%)
Jump correction: 1 / √(max(1 - jump_fraction, 0.05))
Horizon scaling: √(target_seconds / candle_seconds)

Bands scaled to 24h horizon from annual references:
  Calm < 0.020 | Low < 0.040 | Active < 0.075
  Hot < 0.120 | Extreme > 0.120

Data: 1-min OHLCV from CoinDCX Futures. Refreshed every 60s.
```

This exists for the 5% of users who will demand to know the math. It also builds institutional trust. Always available, never in the way.

---

### 4.6 NOTIFICATION SYSTEM (IN-APP ALERTS)

Notification types:

**Type 1: Regime Change Alert**
Triggered when a token crosses a band threshold.
```
🔥 CGPT just went HOT
Vol score crossed 0.075 threshold
OI rising + Surging direction
→ View CGPT
```

**Type 2: Breakout Setup Alert**
Triggered when vol compression is detected with OI building.
```
🌀 INJ compression setup forming
Vol at 14th percentile, OI building
Watch for first expansion bar
→ View INJ
```

**Type 3: Regime Collapse Alert**
Triggered when a previously Hot/Extreme token drops to Stable/Low.
```
↓ BTC cooling — Extreme → Active
Vol fading, OI declining
→ View BTC
```

**Notification logic:** A state machine per token. Each token has a `prev_regime` and `curr_regime`. On every data refresh, compare. If crossed, fire. Debounce: no more than 1 alert per token per 30 minutes.

**Notification settings (accessible from bell icon):**
- Toggle alerts per regime level (only alert me for Hot+Extreme)
- Toggle breakout setup alerts
- Choose tokens to watch (default: all; can pin specific tokens)

---

## PART 5 — DATA MODEL

### 5.1 Per-Token Data Contract

```typescript
interface TokenVolData {
  // Identity
  symbol: string;              // "CGPT"
  full_name: string;           // "CognitiveCraft Token"
  pair: string;                // "CGPT-USDT"
  exchange: "CoinDCX";
  
  // Price
  price: number;               // current price in USDT
  price_change_24h: number;    // fractional (-0.12 = -12%)
  price_change_1h: number;
  
  // Core Vol Metrics (24h horizon-scaled)
  vol_score: number;           // final jump-adjusted score
  sigma_yz: number;            // Yang-Zhang component
  sigma_ewma: number;          // EWMA component
  bvol_window: number;         // 24h rolling std
  jump_fraction: number;       // BNS jump decomposition
  
  // Regime Classification
  regime: "Calm" | "Low" | "Active" | "Hot" | "Extreme";
  regime_band_pct: number;     // 0-1, position within band
  prev_regime: string;         // for change detection
  
  // Direction Classification
  vol_direction: "Surging" | "Rising" | "Stable" | "Falling" | "Contracting";
  vol_1h: number;              // 1h vol score (for cross-TF comparison)
  vol_4h: number;              // 4h vol score
  vol_24h: number;             // 24h vol score (= vol_score default)
  vol_7d: number;              // 7d vol score
  vol_30d_avg: number;         // 30d average vol score (baseline)
  vol_percentile_30d: number;  // where current vol sits in 30d history
  
  // Sparkline
  vol_sparkline_24h: number[]; // 24 hourly vol_score values, oldest first
  
  // Supporting Market Metrics
  oi_24h_change: number;       // fractional OI change
  oi_usd: number;              // current OI in USD
  funding_rate: number;        // current 8h funding rate
  volume_24h_usd: number;      // 24h volume in USD
  volume_vs_30d_avg: number;   // ratio (2.4 = 2.4× average)
  dollar_vol_24h: number;      // dollar volume proxy for liquidity
  
  // Quality
  quality_score: number;       // 0-1
  quality_flags: string[];     // ["missing_bars_2", "flat_candles_1"]
  bar_count: number;           // actual bars used
  
  // Technical Context
  rsi_14: number;              // 14-period RSI
  distance_to_key_level: number; // % distance to nearest significant level
  price_tightening: boolean;   // price range narrowing last 6h
  
  // Computed Signals
  trade_signal: "MomentumLong" | "MomentumShort" | "ReversalLong" | "ReversalShort" | "BreakoutWatch" | "Range" | "Avoid";
  signal_readiness: number;    // 0-100
  
  // Vol Outlook
  vol_outlook: "Persist" | "Fade" | "Compression";
  vol_outlook_confidence: number; // 0-100
  vol_outlook_reasons: string[]; // 2-3 plain language reasons
  
  // Why Layer
  quant_reasons: QuantReason[]; // auto-computed
  research_note: ResearchNote | null; // injected by research team
  
  // Card Rationale (pre-computed summary string)
  card_rationale: string;      // ≤55 chars
  
  // Meta
  last_updated: ISO8601;
  data_source: string;
}

interface QuantReason {
  icon: string;                // emoji
  text: string;                // plain language
  confidence: "strong" | "moderate" | "weak";
  signal_type: "oi" | "funding" | "vol_trend" | "price" | "jump" | "technical";
}

interface ResearchNote {
  text: string;                // ≤200 chars
  author: string;
  timestamp: ISO8601;
  tags: string[];              // ["listing", "unlock", "macro", "technical"]
}
```

### 5.2 Market-Level Context Data

```typescript
interface MarketContext {
  hot_extreme_count: number;       // tokens in Hot or Extreme
  total_tracked: number;           // total tokens in universe
  surging_pct: number;             // % with Surging direction
  market_avg_vol_score: number;    // average vol_score across universe
  market_vol_percentile_3m: number; // where avg is vs 3m history
  highest_vol_token: string;        // symbol with max vol_score
  highest_vol_value: number;
  market_context_text: string;     // generated one-liner for context bar
  last_updated: ISO8601;
}
```

### 5.3 Sort Options

Default sort: `vol_score DESC`

Available sorts:
- Vol Score ↓ (default) — highest absolute vol first
- Vol Surging ↓ — highest vol_direction acceleration first (EWMA vs 7d avg)
- Signal Readiness ↓ — best-aligned setups first
- OI Change ↓ — highest OI building first
- Vol Percentile ↓ — highest vs own history first (catches emerging breakouts in low-base assets)
- Price Change ↓ / ↑

---

## PART 6 — SIGNAL ENGINE SPECIFICATION

This is the computation layer. Everything here is deterministic and rule-based. No ML, no black boxes. Every output must be explainable in plain language.

### 6.1 Regime Classification

```python
def classify_regime(vol_score, target_horizon="24h"):
    # Bands are annual thresholds scaled to target horizon
    SECONDS_PER_YEAR = 31_536_000
    target_seconds = HORIZON_MAP[target_horizon]
    scale = sqrt(target_seconds / SECONDS_PER_YEAR)
    
    calm_thresh    = 0.20 * scale   # ≈ 0.020 for 24h
    low_thresh     = 0.40 * scale   # ≈ 0.040
    active_thresh  = 0.75 * scale   # ≈ 0.075
    high_thresh    = 1.20 * scale   # ≈ 0.120
    
    if vol_score < calm_thresh:   return "Calm"
    elif vol_score < low_thresh:  return "Low"
    elif vol_score < active_thresh: return "Active"
    elif vol_score < high_thresh: return "Hot"
    else:                         return "Extreme"
```

### 6.2 Vol Direction Classification

Direction is computed from the relationship between current vol and recent history across multiple timeframes.

```python
def classify_direction(vol_1h, vol_4h, vol_24h, vol_7d_avg, vol_percentile_30d):
    
    # Surging: all shorter timeframes accelerating above longer
    if vol_1h > vol_4h * 1.3 and vol_4h > vol_24h * 1.2:
        return "Surging"
    
    # Rising: current above recent average and trending up
    if vol_24h > vol_7d_avg * 1.15 and vol_4h > vol_24h * 0.95:
        return "Rising"
    
    # Contracting: vol compressing below historical median
    if vol_percentile_30d < 0.20 and vol_4h < vol_24h * 0.90:
        return "Contracting"
    
    # Falling: was elevated, now declining
    if vol_24h > vol_7d_avg * 1.1 and vol_4h < vol_24h * 0.85:
        return "Falling"
    
    # Default: Stable
    return "Stable"
```

### 6.3 Trade Signal Classification

This is the most complex signal. It combines price action, vol metrics, and market metrics.

```
RULE SET (evaluated in priority order):

AVOID:
  IF quality_score < 0.65 → AVOID
  IF dollar_vol_24h < min_liquidity_threshold → AVOID

MOMENTUM LONG:
  IF price_change_24h > 0
  AND vol_direction IN [Surging, Rising]
  AND oi_24h_change > 0.02
  AND jump_fraction < 0.20
  AND funding_rate < 0.06
  AND regime IN [Active, Hot]
  → MOMENTUM LONG

MOMENTUM SHORT:
  IF price_change_24h < 0
  AND vol_direction IN [Surging, Rising]
  AND oi_24h_change > 0.02
  AND jump_fraction < 0.20
  AND funding_rate > -0.06
  AND regime IN [Active, Hot]
  → MOMENTUM SHORT

REVERSAL LONG:
  IF price_change_24h < -0.08 (down >8% in 24h)
  AND vol_direction IN [Falling, Stable]  (vol spike fading)
  AND oi_24h_change < -0.03 (OI unwinding = longs liquidated)
  AND funding_rate < -0.03 (funding negative = shorts crowded)
  AND jump_fraction > 0.30 (was a cascade, now settling)
  → REVERSAL LONG

REVERSAL SHORT:
  IF price_change_24h > 0.08
  AND vol_direction IN [Falling, Stable]
  AND oi_24h_change < -0.03
  AND funding_rate > 0.06 (longs crowded)
  AND jump_fraction > 0.30
  → REVERSAL SHORT

BREAKOUT WATCH:
  IF vol_direction == Contracting
  AND vol_percentile_30d < 0.20
  AND oi_24h_change > 0.01 (OI quietly building)
  AND price_tightening == True
  AND quality_score > 0.75
  → BREAKOUT WATCH

RANGE:
  IF vol_direction IN [Stable, Falling]
  AND regime IN [Low, Active]
  AND abs(oi_24h_change) < 0.03
  AND abs(funding_rate) < 0.02
  → RANGE

DEFAULT → RANGE if nothing above matches
```

### 6.4 Vol Outlook Computation

A weighted persistence probability model:

```python
def compute_vol_outlook(token):
    score = 0
    reasons = []
    
    # OI confirmation (weight: 30)
    if token.oi_24h_change > 0.05:
        score += 30
        reasons.append("OI rising — participation is real")
    elif token.oi_24h_change > 0.01:
        score += 15
    elif token.oi_24h_change < -0.03:
        score -= 20
        reasons.append("OI falling — vol may be exhausting")
    
    # Funding state (weight: 20)
    if abs(token.funding_rate) < 0.03:
        score += 20
        reasons.append("Funding neutral — no crowding to unwind")
    elif abs(token.funding_rate) > 0.07:
        score -= 15
        reasons.append("Funding extreme — crowded side at risk")
    
    # Vol trend cross-timeframe (weight: 25)
    if token.vol_1h > token.vol_4h > token.vol_24h:
        score += 25
        reasons.append("Vol expanding across all timeframes")
    elif token.vol_1h < token.vol_4h and token.vol_4h > token.vol_24h:
        score -= 10
        reasons.append("Short-term vol decelerating")
    
    # Jump fraction (weight: 15)
    if token.jump_fraction < 0.15:
        score += 15
        reasons.append("Smooth trend — diffusion-driven, not cascade")
    elif token.jump_fraction > 0.35:
        score -= 25
        reasons.append("Jump-heavy — cascade vol tends to revert")
    
    # Quality (weight: 10)
    if token.quality_score > 0.85:
        score += 10
    
    # Threshold to outlook
    if token.vol_direction == "Contracting" and token.oi_24h_change > 0:
        return "Compression", min(80, score + 20), reasons
    elif score >= 55:
        return "Persist", min(95, score), reasons[:3]
    elif score <= 30:
        return "Fade", min(90, 100 - score), reasons[:3]
    else:
        return "Fade", 45, ["Mixed signals — low conviction outlook"]
```

### 6.5 Signal Readiness Score Computation

```python
def compute_readiness(token, trade_signal):
    score = 0
    
    if trade_signal in ["MomentumLong", "MomentumShort"]:
        if token.vol_direction in ["Surging", "Rising"]:       score += 20
        if token.sigma_ewma > token.sigma_yz * 1.1:            score += 15
        if token.bvol_window_trend == "expanding":             score += 15
        if token.jump_fraction < 0.15:                         score += 15
        if token.oi_24h_change > 0.03:                         score += 15
        if token.quality_score > 0.80:                         score += 10
        if token.dollar_vol_24h > LIQUIDITY_THRESHOLD:         score += 10
    
    elif trade_signal in ["ReversalLong", "ReversalShort"]:
        if token.jump_fraction > 0.30:                         score += 20  # was a spike
        if token.vol_direction in ["Falling", "Stable"]:       score += 20  # fading
        if abs(token.oi_24h_change) < -0.03:                   score += 15  # OI unwinding
        if abs(token.funding_rate) > 0.05:                     score += 15  # crowded
        if token.quality_score > 0.85:                         score += 15  # clean data
        if token.rsi_14 < 30 or token.rsi_14 > 70:            score += 15  # extreme RSI
    
    elif trade_signal == "BreakoutWatch":
        if token.vol_percentile_30d < 0.15:                    score += 25
        if token.vol_direction == "Contracting":               score += 20
        if token.oi_24h_change > 0.01:                         score += 20  # quiet OI build
        if token.price_tightening:                             score += 20
        if token.quality_score > 0.85:                         score += 15
    
    return min(100, score)
```

### 6.6 Quantitative Reason Generation

A deterministic rule table that generates plain-language reasons from metric states.

The rule table has ~60 entries. Each entry is: `(metric, state, text, confidence, signal_type)`.

Sample rules:

```
OI state:
  oi_24h_change > 0.10  → "OI surging +{val}% — strong directional conviction", strong, oi
  oi_24h_change > 0.03  → "OI building steadily — participation real", strong, oi
  oi_24h_change < -0.05 → "OI falling — positions being closed", strong, oi
  oi_div_price_up       → "OI falling while price rises — divergence warning", strong, oi

Funding state:
  rate > 0.08           → "Funding at {val}% — longs heavily crowded, squeeze risk", strong, funding
  rate > 0.04           → "Funding elevated — some crowding in longs", moderate, funding
  -0.02 < rate < 0.02   → "Funding neutral — no crowding risk", strong, funding
  rate < -0.04          → "Funding negative — shorts crowded, long squeeze risk", strong, funding

Vol trend:
  vol_1h > vol_4h * 1.3 → "Short-term vol accelerating hard — regime intensifying", strong, vol_trend
  vol_24h > vol_7d * 2   → "24h vol is {N}× its 7d average — elevated regime", strong, vol_trend
  contracting + low_pctile → "Vol compressing to {pctile}th percentile — coiling", strong, vol_trend

Jump:
  jump_frac < 0.10      → "Smooth diffusion-driven move — stops likely safe", strong, jump
  jump_frac > 0.35      → "Cascade behavior detected — stop slippage risk high", strong, jump
  jump_frac 0.15-0.35   → "Some discontinuous moves — widen stops modestly", moderate, jump

Price:
  rsi < 30              → "RSI at {val} — oversold zone, watch for bounce", moderate, technical
  rsi > 70              → "RSI at {val} — overbought zone, watch for pullback", moderate, technical
  price_vs_key_level    → "Price within {pct}% of {level} key level", moderate, technical
```

### 6.7 Card Rationale String Generation

The one-line card rationale (≤55 chars) is the most impactful text in the product — it's visible without any tap. It must be the single most important thing happening to this token right now.

Priority rules (first match wins):
1. If jump_fraction > 0.40: "Cascade behavior — {outlook} signal"
2. If vol_direction == Contracting AND oi building: "Vol coiling + OI building"
3. If oi_24h_change > 0.08 AND vol_direction == Surging: "OI surging + vol expanding"
4. If funding > 0.07 AND vol rising: "Funding crowded + vol rising — risk"
5. If vol_24h > vol_7d * 2.5: "Vol {N}× normal — {regime} regime"
6. If rsi < 30 AND vol falling: "Oversold + vol fading — reversal watch"
7. Default: "{vol_direction} vol, {oi_direction} OI"

---

## PART 7 — VISUAL DESIGN SYSTEM

### 7.1 Aesthetic Direction

**Concept:** "Precision Dark" — the aesthetic of a high-frequency trading desk, not a consumer app. The information is the design. Every element earns its place or is removed. The only luxury is precision.

Reference aesthetic: not Bloomberg (too dense), not Robinhood (too consumer-friendly), not Binance (too busy). The target is closer to a well-designed CLI tool made visual — clean, structured, information-dense, emotionally cool.

**What makes it unforgettable:** The color temperature shifts with market temperature. As the market moves from Calm to Extreme, the overall app color temperature subtly shifts from cool blue-gray to warm amber to red. The background is not a static black — it has a subtle chromatic relationship with the dominant regime of the current visible tokens.

### 7.2 Color System

```css
:root {
  /* Backgrounds */
  --bg-base:        #080c0f;  /* near black with cold blue tint */
  --bg-card:        #0d1117;  /* cards slightly lighter */
  --bg-card-hover:  #111820;
  --bg-elevated:    #141c24;  /* modal, sheet backgrounds */
  --bg-context:     #0a0f14;  /* context bar */
  
  /* Regime Colors — used consistently across all components */
  --calm:           #4b5563;  /* gray */
  --calm-dim:       #1f2937;
  --low:            #22c55e;  /* green */
  --low-dim:        #052e16;
  --active:         #f59e0b;  /* amber */
  --active-dim:     #451a03;
  --hot:            #f97316;  /* orange */
  --hot-dim:        #431407;
  --extreme:        #ef4444;  /* red */
  --extreme-dim:    #450a0a;
  
  /* Direction Colors */
  --surging:        #f97316;
  --rising:         #f59e0b;
  --stable:         #6b7280;
  --falling:        #3b82f6;
  --contracting:    #a855f7;
  
  /* Signal Colors */
  --signal-momentum-long:  #22c55e;
  --signal-momentum-short: #ef4444;
  --signal-reversal-long:  #06b6d4;
  --signal-reversal-short: #f43f5e;
  --signal-breakout:       #a855f7;
  --signal-range:          #6b7280;
  --signal-avoid:          #374151;
  
  /* Text */
  --text-primary:   #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted:     #475569;
  --text-disabled:  #334155;
  
  /* Borders */
  --border-dim:     #1e293b;
  --border-normal:  #334155;
  --border-bright:  #475569;
  
  /* Positive / Negative */
  --positive:       #22c55e;
  --negative:       #ef4444;
  --neutral:        #94a3b8;
}
```

### 7.3 Typography

**Display font (headers, symbols, vol scores):** `IBM Plex Mono` — technical, precise, monospace authority. Used for BVOL values, prices, symbols, vol scores, all numbers.

**Body font (rationale, reasons, explanations):** `DM Sans` — slightly condensed, readable at small sizes, modern without being trendy. Used for all prose, chip labels, metadata.

**Size scale:**
```
Symbol (card):     15px, bold, Plex Mono
Price (card):      14px, medium, Plex Mono
BVOL (card):       13px, regular, Plex Mono
Rationale (card):  12px, regular, DM Sans, --text-secondary
Chip label:        10px, semibold, DM Sans, uppercase, letter-spacing 0.08em
Section header:    11px, semibold, DM Sans, uppercase, letter-spacing 0.12em, --text-muted
Drilldown value:   20px, bold, Plex Mono (for headline vol score)
Body copy:         13px, regular, DM Sans
```

### 7.4 Motion Spec

**Card entrance animation:**
- translateY(20px) → translateY(0) + opacity 0 → 1
- Duration: 300ms, ease-out
- Stagger: 50ms per card

**Value update flash:**
- Background flash: --active-dim at 60% opacity → transparent
- Duration: 800ms
- Only triggers on value change, not on every refresh

**Bottom sheet entrance:**
- translateY(100%) → translateY(0) with spring physics (stiffness 400, damping 40)
- Duration: 350ms

**Regime color transitions:**
- All regime-colored elements use `transition: background-color 600ms ease, border-color 600ms ease`
- Smooth, never jarring

**Extreme pulse animation:**
```css
@keyframes extreme-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.4); }
  70%  { box-shadow: 0 0 0 8px rgba(239, 68, 68, 0); }
  100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
}
/* Applied to cards in Extreme regime */
```

**Contracting shimmer (Breakout Watch):**
```css
@keyframes contracting-shimmer {
  0%   { border-color: rgba(168, 85, 247, 0.3); }
  50%  { border-color: rgba(168, 85, 247, 0.8); }
  100% { border-color: rgba(168, 85, 247, 0.3); }
}
/* Applied to cards with Contracting direction */
/* Signals "something is building" */
```

### 7.5 Component Spec: Sparkline

```
- 60px wide × 24px tall
- No axes, no labels
- SVG path with smooth bezier curves (not stepped)
- Line color matches current regime color
- Fill gradient below line: regime_color at 20% opacity fading to transparent
- If last 4 points are rising: endpoint has a small dot in surging color
- If last 4 points are falling: endpoint dot in falling color
```

### 7.6 Component Spec: Vol Bar (on card)

```
- Full card width × 3px height
- Background: --bg-card
- Fill: linear-gradient(left, regime-dim to regime-bright)
- Fill width: proportional to position within band (not absolute score)
  → A Hot token at 80% through the Hot band shows 80% fill width
  → This makes the bar comparable across regimes
- Transition: width 600ms ease on data update
```

---

## PART 8 — BACKEND REQUIREMENTS

### 8.1 Data Pipeline

**Input:** 1-minute OHLCV candles for all CoinDCX futures pairs.

**Processing (per token, every 60s):**
1. Load last 1,440 bars (24h window)
2. Compute Yang-Zhang σ_yz
3. Compute EWMA σ_ewma (λ=0.94, last 100 bars for seed)
4. Compute bvol_window (rolling std, 1440 bars)
5. Compute jump_fraction (BNS decomposition on last 1440 bars)
6. Compute vol_score (blended + jump correction)
7. Compute 1h, 4h, 7d, 30d vol scores (same formula, different window)
8. Compute vol_percentile_30d (vol_score vs last 30d of daily vol_scores)
9. Compute quality_score (check for gaps, flat bars, duplicates)
10. Store: all above + price + OI + funding (from exchange API)

**Derived signals (compute after raw metrics):**
11. Classify regime
12. Classify vol_direction
13. Classify trade_signal
14. Compute vol_outlook + confidence
15. Compute signal_readiness
16. Generate quant_reasons (rule engine)
17. Generate card_rationale (one-liner)

**Output:** JSON array of TokenVolData sorted by vol_score DESC, refreshed every 60s.

### 8.2 API Endpoints

```
GET /api/v1/vol/tokens
  → full TokenVolData[] for all tokens, sorted by vol_score DESC
  → includes market_context as top-level field
  → cache: 60s
  → response time: <200ms (pre-computed, not on-demand)

GET /api/v1/vol/tokens/:symbol
  → single TokenVolData for one token
  → includes extended history (vol_sparkline_7d: 168 hourly points)
  → cache: 30s

GET /api/v1/vol/alerts
  → recent regime change events, last 4h
  → for notification system

POST /api/v1/research/notes  (admin only)
  → inject research note for a token
  → fields: symbol, text, author, tags

WebSocket /ws/vol/live
  → pushes delta updates when any token's regime or vol_score changes significantly
  → payload: {symbol, field, old_value, new_value, timestamp}
  → used for real-time card updates and notification triggering
```

### 8.3 Latency Budget

```
Data collection from exchange:     <5s
Vol computation (all tokens):      <10s (parallelized per token)
Signal engine (all tokens):        <3s
API response:                      <200ms
Frontend render (first paint):     <1s
Total: market event to visible:    <20s
```

### 8.4 Universe Management

Default universe: All active CoinDCX USDT-perpetual futures with:
- Minimum 30 days of 1-min data history (for quality baseline)
- Minimum $100K daily dollar volume (liquidity floor)
- Active (not expired/delisted)

Universe refreshed daily. Token additions are automatic when criteria are met.

---

## PART 9 — MOBILE UX PATTERNS

### 9.1 Gesture Model

**Vertical scroll:** Token list scrolling
**Horizontal scroll:** Filter chips only
**Tap:** Card opens drilldown
**Swipe up:** Expands bottom sheet from partial to full height
**Swipe down on sheet handle:** Dismisses drilldown
**Long press on card:** Share sheet (copy token name + signal + vol score for sharing)

### 9.2 Loading States

**Initial load:**
- 3 skeleton cards with animated shimmer
- Context bar shows "Loading market data..."
- Chips visible immediately (no data needed to render)

**Refresh:**
- No loading spinner (would be annoying at 60s intervals)
- Instead: values flash when updated (see animation spec)
- "Updated Xm ago" timestamp in header updates silently

**Error state:**
- If data is >5 minutes old: amber warning bar below header: "⚠ Data may be stale — last updated 7m ago"
- If completely offline: "📡 No connection — showing last known data from 12m ago"
- Never completely blank — always show last good data with a freshness warning

### 9.3 Empty States

**All filters selected, no tokens match:** Impossible — ALL is always valid.

**Specific filter selected, no tokens match:**
```
No [HOT] tokens right now.
Most tokens are [ACTIVE] — showing those instead.
[View Active Tokens] button
```

Auto-suggest the nearest non-empty filter.

### 9.4 Pull to Refresh

Standard pull-to-refresh gesture force-triggers a data refresh outside the 60s cycle. Show a subtle animation during manual refresh.

---

## PART 10 — FUTURE EXTENSIONS (Roadmap Context)

These are not in scope for V1 but must be architecturally supported.

### 10.1 Push Notifications
When a token transitions to Hot or Extreme (or a Breakout Watch fires), push a notification to the CoinDCX app. Requires: backend notification service, user preference API, deep link integration.

### 10.2 Historical Signal Performance
Track when each trade signal was generated and what price did over the next 4h, 8h, 24h. Display hit rate per signal type on the drilldown. Requires: signal log storage, performance computation pipeline.

### 10.3 Watchlist
Users can pin specific tokens to always appear at the top of the list regardless of sort/filter. Requires: user auth integration, preferences storage.

### 10.4 Vol Calendar
A view showing scheduled events that historically correlate with vol spikes for each token: token unlocks, project announcement dates, derivatives expiry. Fed by the research team's calendar database.

### 10.5 Pairs Mode
Select two tokens and see a comparison view: vol score, direction, signal, and correlation of vol history. Useful for spread traders and relative-value opportunities.

### 10.6 Web Version
The mobile-first design works on web at 480px max-width centered. A true desktop version would show the discovery list and drilldown side-by-side (list on left 380px, drilldown on right), with a multi-token comparison panel on the far right. Same data model, different layout.

---

## PART 11 — WHAT NOT TO BUILD IN V1

Explicit exclusions to prevent scope creep:

- ❌ Charting (use TradingView for this)
- ❌ Order placement (deep link to exchange, don't replicate)
- ❌ Portfolio tracking
- ❌ News feed / social sentiment
- ❌ Alerts builder / complex condition editor
- ❌ Multi-asset corr matrix
- ❌ Options / Greeks (different product)
- ❌ Custom strategy backtesting
- ❌ ML/AI price prediction
- ❌ Anything requiring user login in V1 (anonymous read-only is fine for launch)

Every one of these is a good idea for V2+. None of them should appear in V1 because they dilute the discovery-first architecture.

---

## PART 12 — SUMMARY: THE EXPERIENCE IN 30 SECONDS

A trader opens VOL SIGNAL at 9:05 AM.

The context bar reads: "🔥 14 tokens HOT or above · Vol expanding in 68% of pairs"

The top card shows:
```
CGPT  [🔥 HOT]                    $0.8423  +12.4% ▲
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  (75% fill, orange)
BVOL 0.087  ⚡ SURGING        [sparkline trending up]
OI +8.3%  Fund 0.041%  Vol 2.4× avg
OI building fast + vol expanding vs 7d avg  [M↑]
```

The trader taps. The drilldown slides up.

They see: vol score in HOT band, σ_yz = 0.071, σ_ewma = 0.081, jump fraction = 0.08 (clean), OI confirmation, funding neutral, outlook says "LIKELY TO PERSIST" at 78% confidence.

They see the signal: MOMENTUM LONG, readiness 84/100.

They tap "Trade CGPT on CoinDCX Futures →".

**12 seconds. Insight to action.**

That is the product.

---

*This spec covers the complete V1 build. All signal logic is deterministic and rule-based. All UI patterns are mobile-native. The product is opinionated by design — it makes choices so the trader doesn't have to.*
