# Pulse wire contract review kit

Proposals only; no endpoint or frontend activation. Work split9da5f3d.
Generated JSON files, not Python generator imports, are the frontend handoff.
URN refs are local registry identifiers, never HTTP downloads.

## Reproduce

Use a separate Python3.12 venv; do not modify the shared/backend environment:

```sh
python3.12 -m venv /tmp/pulse-contract-validator
/tmp/pulse-contract-validator/bin/pip install --require-hashes --only-binary=:all: -r requirements-contracts.lock
/tmp/pulse-contract-validator/bin/python tools/contracts/build.py --check
/tmp/pulse-contract-validator/bin/python tools/contracts/validate.py
```

Generate with`python tools/contracts/build.py` (stdlib only). Commit generated
files with the proposal; drift must fail --check. Production requirements and
the existing CI/runtime lock are untouched. Validator wheel hashes cover3.12
macOS/Linux arm64/x86_64, not arbitrary platforms/Python versions.

## Common records

[common.schema.json](schema/common.schema.json), URI`urn:pulse:schema:v1:common`.
[Field fixtures](fixtures/metric_fields.v1.json) include valid and unavailable
values for all five L1 metrics plus price, price changes and volume. Fictional
values with a fixed fixture clock, explicitly not live/evidence/calibration data.
Do not drop a pair because one metric is unavailable. Unknown status or numeric
unit fails validation; an unknown lower_snake_case reason remains an opaque
unavailable diagnostic. Numeric0 is allowed only with status available.

Draft2020-12 enforces shapes/types, not cross-field arithmetic or freshness at
the current clock. Semantic validation and browser expiry gates are separate.
No unapproved L2 thresholds are activated. ADR-0111 extends the proposed schema
to current global L/S ratio, native funding estimate, settled d_last and OI
returns, alongside existing v1 volatility. Non-volatility fields require exact
canonical basis/unit and a policy version beginning `<definition>.l2.`; OI also
pins the horizon before the policy id. Semantic guards refuse classified top,
context and cumulative fields. `snapshot.classified_all_l2.json` and its seven
wire parts are fictional consumer fixtures, NOT calibrated thresholds. Live
non-volatility activation remains gated by the measured report and acceptance.
No legacy report join in the browser is planned: the row proposal follows.

## Review status

ADR-0078: [snapshot schema](schema/snapshot.schema.json) and six whole-document
[fixtures](fixtures/) (1H/4H/24H valid, unavailable, partial1H/4H/8H contracts,
expired).`funding_change` parent status means verified operand, not complete7D
history. Available settlement fields expire for visibility at due; retained
backend reports remain until R6. Exact row paths are in the machine schema.

Backend carries price/volume/signal/changes/context/history/outlook/since-state;
no extra legacy fetch in the browser. Adapter flattens this one generation into
the design model; missing history has empty points/not_retained. Old native
signal codes and display categories are both supplied. RSI/log momentum are v1
values, not newly computed indicators or substituted zeros.

Run additional checks:

```sh
/tmp/pulse-contract-validator/bin/python -m unittest discover -s tools/contracts -p 'test_*.py' -v
node tools/contracts/node_check.mjs
```

Mapping SHA-256 covers canonical dictionary. Generation SHA-256 covers canonical
snapshot excluding generation and its reference echoes, independent of br/gzip.
Object keys are ASCII/sorted; values UTF-8, arrays ordered, financial numbers
strings, remaining numbers safe integers. Node/Python parity is tested. A newly
validated main dictionary establishes a mapping; live data cannot establish one.
Unknown live mappings fail closed. A changed dictionary must get a new mapping;
mapping changes switch rows/index caches atomically. Never join by row order alone.

The expired fixture was valid at its generated_ms but expired at the fixed test
now_ms=1800000000000. Structural/semantic validation does not make it fresh again;
loader must gate envelope and field expiry using current time.

Adapter flattening (no extra fetch): price.value→row.price;
volume.quote_24h.value→row.volume; signal.category.value→row.signal;
changes[window].value→row.changes[window]; history[metric].points→sparkline only
when available. Wire`ls`/`oi` map to design registry`long_short`/`open_interest`.
Map native volatility direction Stable→UI Steady as a display label only.
Context interval/quantity/shares/top-ratio come from the same row's universe/OI/L/S,
not copied from a second report. All display projections must check status/expiry.

Keep decimal strings for funding calculations, exact threshold filters and tie
breaking in funding sorts. A binary64 typed-array projection may accelerate a
candidate pass, but must not decide a borderline funding threshold alone. The
semantic gate additionally enforces decimal scale<=60, matching f8721fe's parser;
JSON Schema syntax validation alone does not replace that gate.

ADR-0077: common field proposal; peer reviewer acceptance pending. Snapshot row and live
price extensions follow as separate commits so either can be reviewed independently.

## Compact transport proposal (26 Oct)

Read [compact wire v1](compact_wire_v1.md), ADR-0081, `schema/columns.schema.json`
and `fixtures/wire/`. The named logical model above remains unchanged. Reproduce
the size evidence with `tools/contracts/benchmark_wire.py`; the two measured JSON
artifacts retain both batch-source and independent-version cases. No live-data or
deployment claim follows from these synthetic load measurements.
