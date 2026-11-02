# Metric calculations: formulas, reasoning and research

27Oct2026: [ADR-0111](../decisions/ADR-0111-l2-calibration-and-classified-contract.md)
adds a proposed L2 wire contract using the unchanged canonical bases below and
a bounded September archive pilot (20 training days, one purge, nine holdout).
This is older event-time data, with settled-rate/5min-OI proxy limitations;
no live numeric cutoffs are accepted or activated by the pilot. Equality at a
proposed cutoff enters the higher band. Current Delta and cumulative funding
remain unclassified. peer reviewer review and project owner numeric acceptance are pending.

**One document per metric:** what is calculated, from which inputs, why this way (alternatives with their ADRs), the research behind it, edge cases and the fixtures that must pass. Readers: project owner, peer reviewer, peer reviewer.

**Rules:**
- **These documents are the canonical calculation definitions.** `pulse_screener_plan.md` §3 summarises them; if the two disagree, this folder wins and the plan gets fixed.
- **A formula, unit, window, source or threshold change needs a new ADR** (`docs/decisions/`) plus an update here, both reviewed by the other agent and approved by project owner.
- **Implementations cite the doc and ADR** in a one-line comment, and their tests use the fixtures listed in each doc.
- **Every document has a Review block.** peer reviewer reviews peer reviewer's documents and vice versa.

| Metric | Document | Main ADRs | Status |
|---|---|---|---|
| Price and price change (1H/4H/24H) | [price_change.md](price_change.md) | 0005, 0006, 0019 | Round 3: formula supported; ladder/fallback/size gates pending |
| 24H USDT quote volume | [volume.md](volume.md) | 0194 | User-approved5min validity; independent price clocks |
| Volatility | [volatility.md](volatility.md) | 0013 | Round 3: v1 fixture scope supported; executable parity pending; v2 Proposed |
| Long / Short | [long_short.md](long_short.md) | 0007, 0004 | Round 3: reconcile/approve top expiry; cut-offs pending |
| Funding rate | [funding.md](funding.md) | 0008, 0004, 0021 proposed | Round 3: corrected math supported; historical coverage/cut-offs pending |
| Funding change | [funding_change.md](funding_change.md) | 0009, 0004, 0020, 0021 proposed | Round 3: accepted settled basis; coverage/expiry/cut-offs pending |
| Open interest | [open_interest.md](open_interest.md) | 0010, 0004 | Round 3: quantity return supported; synchronized notional evidence/cut-offs pending |

**Shared conventions:**
- **Typed units:** rates and returns are fractions; share changes display as `100 × Δ` pp; L/S is a ratio; OI is a quantity.
- **Every value** carries `status`, `asof`, `baseline_asof` where relevant, `expires_at` and `definition_version`.
- **Missing is never 0.**
- **Direction follows the sign;** the level is magnitude only.
- **Cut-offs** live in the registry, calibrated per ADR-0015.
