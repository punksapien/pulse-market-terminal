# 24H quote volume

ADR-0194, project owner approved29Oct2026. Definition: volume.rt.v2.

`volume_USDT = Decimal(CoinDCX_futures_bulk.prices[pair].v)`; no multiplication
by price and no reconstruction from trade size. Pair identity must match the
fixed USDT-perpetual universe. Amount must be finite and >=0; zero is valid.
Source time is the original bulk `ts`, no more than5s future. Expires exactly
`source_ms + 300000`. Old v1 live observations retain exactly60000ms validity.

Volume is independent of last-price validity, funding, reference coverage and
the selected L1/horizon. A valid15s live envelope admits the value once; its later
expiry cannot revoke already accepted volume before the volume's OWN deadline.
Retain scalar value/source/expiry/version only, across same-mapping generations
and horizons. Prefer a newer valid source; never redate, zero-fill or keep it
after expiry. Price and return expiry never inherit the volume deadline, and
volume expiry never erases price/returns. Browser timers enforce each deadline.

Transport currently shares the2s bulk live path at no additional exchange cost;
roughly one-minute volume refresh is acceptable, leaving5min validity headroom.
The table uses compact USDT display. No classification or trading signal.

Fixtures: tests/test_fast_live_volume.py; tests/test_pulse_live_volume.js and
tests/test_pulse_loader.js. Cases: valid positive/zero, absent/negative/NaN/float,
future/expired source, exact expiry, old60s input, unknown version, shortened or
extended deadline, missing price/reference,15s envelope expiry, new generation,
mapping change, older candidate and price/return independence.

## Review

project owner approved validity; peer reviewer review waived. Source amount/formula unchanged.
Not checked: provider rollout and long-lived browser behaviour until checkpoint.
