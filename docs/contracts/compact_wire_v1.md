# Compact wire v1 — ADR-0081 (proposal)

The logical model is still `pulse.snapshot.v1`. Nothing is rounded, quantised,
reclassified or zero-filled. `pulse/wire/compact.py` is the producer/reference
decoder; `tools/contracts/wire_spec.py` generates the formal Draft2020-12 schema.
All fixtures are fictional. Never serve them as exchange data.

## Documents and joins

Seven parts: `core`, `volatility`, `ls`, `funding`, `funding_change`, `oi`,
`references`. Every part includes schema=`pulse.columns.v1`, part, count,
mapping_version, generation, horizon, generated_ms, expires_ms, metadata, groups,
columns, inputs. Core additionally has `header`: the logical envelope except rows
and inputs, including the complete universe. Other parts must not have header.

Core holds id, price, **the selected-horizon change only**, volume, signal,
signal_since, context, outlook and non-L1 history. Metric parts hold their matching
row object and history. References holds references plus the other two logical
price-change windows. Thus core + one metric renders that horizon's L1; references
is an additional fetch for event-time live-price changes or all three return
windows. No missing window may be presented as zero or reconstructed using an
unrelated generation. A horizon switch loads its matching core/metric pair.

Inputs are partitioned by input_refs used in a part, with orphans retained in core.
An input used in two parts appears in both and must compare equal when merged.
All seven parts reconstruct the original logical JSON exactly, including optional
absent properties versus null, false, zero and empty arrays/objects. Local tests
compare canonical UTF-8 bytes, not just float-equivalent values.

## Columns (normative)

Path is an array of literal object key strings. Objects are recursively flattened;
arrays and empty objects are atomic JSON values. A field record is recognised by
the simultaneous keys `unit`, `value`, `status`; it is one atomic field column.
Path segments `__proto__`, `prototype`, `constructor` are forbidden everywhere.

General column: `[path, presence, "j", vector]`.
Field column: `[path, presence, "f", values, metadataIds, groupIds, states]`.
Presence=null means every row; otherwise it is a strictly increasing, non-empty
array of row ids where the property exists. All vectors have that cardinality.

Vector formats:

- `["c", value]`: same JSON value for every present row.
- `["d", values]`: dense array, exact cardinality.
- `["s", default, [[index, value], ...]]`: defaults plus strictly increasing
  overrides. Indices address the *present-row vector*, not the global row id.

Each field reconstructs as union of `metadata[metadataId]`, `groups[groupId]`, the
state object, then `{value: values[i]}`. Metadata contains all original properties
except value/status/reason/asof_ms/expires_ms/input_refs. Group contains only the
original asof_ms/expires_ms/input_refs keys. State contains only status/reason.
Optional keys are not invented. Repeated metadata/groups are interned by canonical
JSON bytes. State vectors select constant/sparse/dense automatically. Tables and
vectors are local to each immutable part, never an implicit previous generation.

## Validation and integrity

Before allocation: cap encoded body, count<=8192, columns<=1024, path depth<=32,
and tables<=100000; verify schema. Reject duplicate paths, prefix-overlapping paths
whose presence sets intersect (ADR-0086), bad table
indices, malformed vectors, duplicate presence indices and cross-generation joins.
The reassembled logical document must pass ADR-0077/78 structural/semantic checks.
Manifest hashes cover each wire document; mapping SHA256 can be verified in the
browser at load. Full logical-generation recomputation is required in producer and
tests; not on every browser poll. This accepts peer reviewer's proposed browser policy.
Per-field expiry still beats cache and envelope; expired envelope is a stale
banner, not authority to blank a separately valid retained settlement before due.

## Measurement and limits

Run `PYTHONDONTWRITEBYTECODE=1 /private/tmp/pulse-r5-venv/bin/python
tools/contracts/benchmark_wire.py --output docs/contracts/wire_504_measurement.json`.
Add `--independent-versions` for the stress artifact. CPU timings are one local
macOS observation, not a hosted guarantee. Both cohorts vary values, states,
1H/4H/8H native intervals, missing 7D histories, source ages and v1 directions.
Financial formulas/price operands are checked by the logical semantic validator.

The primary corpus requires **materialised, content-addressed source batches**:
each batch retains all original per-pair response witnesses, and its version is
shared by rows derived from that batch. This is a composer requirement, not an
already implemented claim. The stress corpus has independent 256-bit versions
per response. Its larger bytes are retained as an honest limit, not discarded.
Reference transfer is additional to core+metric; do not claim the initial live
view including references fits 60 KB unless measured. Gzip fallback also has its
own measured budget; passing Brotli does not imply gzip passes.

Measured bytes for the batch-witness corpus (logical raw 9,785,464 B):

| Part | gzip 6 | Brotli 5 | Brotli 11 |
|---|---:|---:|---:|
| Core | 40,652 | 28,872 | 23,866 |
| Volatility | 13,211 | 9,447 | 7,910 |
| L/S | 28,876 | 25,018 | 19,793 |
| Funding | 26,876 | 17,186 | 13,636 |
| Funding change | 20,752 | 14,745 | 12,007 |
| OI | 15,443 | 11,744 | 9,368 |
| References (additional) | 96,851 | 86,869 | 66,858 |

Largest core+metric: 43,659 B Brotli11 / 53,890 B Brotli5 / 69,528 B gzip6.
Including references: 110,517 B Brotli11. Independent-version stress core+metric:
93,384 B Brotli11. Therefore the batch-identity compressed core+metric case passes
60,000 B with Brotli, **not** gzip, full initial live load, or the independent-hash
stress case. These remain explicit delivery-budget gates. No threshold is raised.
