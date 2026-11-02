# Lazy retained history v1 — ADR-0126

`GET /api/pulse?history=<wire row id 0..349>` serves the prepublished ten-pair
bucket. Reads perform no collection or composition. Brotli/gzip/identity, ETag,
`Vary: Accept-Encoding`, CDN 10s bounded by pointer expiry; unavailable -> 503.
The table and its manifest remain unchanged. At most35 history documents.
ADR-0171: Preview full-release background rotation publishes at most four buckets
per minute, with a persisted cursor and zero exchange calls. Viewer reads cannot
trigger it. Full-release variants omit identity duplication; Brotli/gzip remain.

Document: `schema: pulse.history.v1`, mapping_version SHA-256, generation SHA-256
of canonical document without generation, bucket integer 0..34, generated_ms,
expires_ms = generated_ms +180000 for the original cohort or+720000 for the
full-release historical cache, rows (1..10 unique ids in that bucket). This cache
expiry is NOT source freshness and cannot extend any current field's expiry.
Each row: id, pair_id and series. Series keys: open_interest, long_short, funding,
funding_change, volatility. Volatility has 1H/4H/24H series. A series has unit,
label, points, status, reason. Points are `[source_ms, exact_decimal_string]`,
strictly increasing, at most 512, finite, no future values beyond the 5s skew.
Status available iff at least two points; otherwise unavailable with
insufficient_retained_history. No null-to-zero conversion.

OI unit quantity (5m cold ring); L/S unit ratio (5m global ring); funding fraction
(verified consecutive settlement suffix); funding change fraction (consecutive
signed differences); volatility fraction (actual scorer publications per horizon).
OI/L/S have at most 288 samples over 24h. Funding retains at most seven days
verified under the current interval. Volatility has at most 96 publications.

The browser validates schema, bounds, units, mapping, identity, source order,
expiry and generation hash, then picks the exact pair id. Single flight and
per-bucket caching reuse downloads across neighbouring sheets. History cannot
replace any live field or its freshness. Charts retain source times on the x axis.

Fixtures: tests/test_detail_history.py and tests/test_pulse_history.js include
available/empty, signed changes, conflicts, wrong mapping/unit, expired/corrupt,
cross-pair selection and single-flight vectors. These are historical samples,
not cumulative 1D/7D curves or historical 1h/4h/1d L/S period series.
