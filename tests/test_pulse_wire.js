"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const W = require("../frontend/src/pulse/pulse_wire.js");
const A = require("../frontend/src/pulse/pulse_adapter.js");
const model = require("../frontend/src/pulse/pulse_model.js");
const dir = path.join(__dirname, "../docs/contracts/fixtures/wire");
const parts = Object.fromEntries(W.PARTS.map((p) => [p, JSON.parse(fs.readFileSync(path.join(dir, p + ".json"), "utf8"))]));
const oracle = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/pulse_wire_oracle/logical.json"), "utf8"));
const canon = (v) => JSON.stringify(v, (k, x) => (x && typeof x === "object" && !Array.isArray(x) ? Object.fromEntries(Object.keys(x).sort().map((q) => [q, x[q]])) : x));
const copy = (v) => structuredClone(v);

// 1. all seven parts reconstruct the logical snapshot exactly (oracle = the producer's Python decoder)
const full = W.assemble(parts);
assert.deepStrictEqual(full.parts, W.PARTS);
assert.strictEqual(canon(full.snapshot), canon(oracle), "JS reconstruction differs from the Python reference decoder");

// 2. the reconstructed snapshot passes the adapter's fail-closed validation and builds the real index
const now = parts.core.generated_ms + 1000;
const adapted = A.adapt(full.snapshot, now);
assert.strictEqual(adapted.rows.length, parts.core.count);
new model.Index(adapted);

// 3. core + one metric: unloaded metrics stay unavailable (never zero), loaded metric is exact
const partial = W.assemble({ core: parts.core, oi: parts.oi });
assert.deepStrictEqual(partial.parts, ["core", "oi"]);
const snap = A.adapt(partial.snapshot, now);
const oiCell = snap.rows[0].metrics.open_interest["1H"];
assert.notStrictEqual(oiCell.value, null);
assert.strictEqual(oiCell.value, adapted.rows[0].metrics.open_interest["1H"].value);
const lsCell = snap.rows[0].metrics.long_short["1H"];
assert.strictEqual(lsCell.value, null);
assert.strictEqual(lsCell.reason, "missing");
// compact core carries only the selected horizon price change; other windows are absent until references load
assert.notStrictEqual(snap.rows[0].changes["1H"], null);
assert.strictEqual(snap.rows[0].changes["24H"], null);
assert.notStrictEqual(adapted.rows[0].changes["24H"], null, "references supply the other windows");

// 4. negative vectors fail closed with a short code (mirrors the producer's reference decoder)
const bad = (mutate, code, names = ["core"]) => {
  const p = Object.fromEntries(Object.entries(parts).map(([k, v]) => [k, copy(v)]));
  mutate(p);
  assert.throws(() => W.assemble(p), new RegExp(code), code);
};
bad((p) => (p.core.schema = "pulse.columns.v2"), "unknown_schema");
for (const c of [0, 8193, -1, 1.5]) { const d = copy(parts.core); d.count = c; assert.throws(() => W.decodeRows(d), /invalid_count/, "count " + c); }
bad((p) => (p.core.columns[0][0] = ["__proto__"]), "unsafe_path");
bad((p) => (p.core.columns[0][0] = []), "unsafe_path");
bad((p) => p.core.columns.push(copy(p.core.columns[0])), "overlapping_path");
bad((p) => (p.core.columns[0][1] = [1, 1]), "invalid_presence");
bad((p) => (p.core.columns[0][1] = [5]), "invalid_presence");
bad((p) => (p.core.columns[0][3] = ["d", [1]]), "invalid_vector|invalid_presence");
bad((p) => (p.core.columns[0][3] = ["s", 0, [[9, 1]]]), "invalid_sparse_index");
bad((p) => (p.core.columns[0][3] = ["x", 1]), "invalid_vector");
bad((p) => (p.oi.generation = "sha256:" + "0".repeat(64)), "mixed_generation");
bad((p) => (p.oi.horizon = "4H"), "mixed_generation");
bad((p) => (p.oi.part = "ls"), "mixed_generation");
bad((p) => (p.core.header.horizon = "4H"), "header_identity_mismatch");
bad((p) => (p.core.header.universe = []), "header_identity_mismatch");
bad((p) => (p.core.header = undefined), "header_missing");
bad((p) => { const f = p.oi.columns.find((c) => c[2] === "f"); f[4] = ["c", 99999]; }, "invalid_table_index");
bad((p) => { const f = p.oi.columns.find((c) => c[2] === "f"); p.oi.metadata[0].value = "1"; }, "invalid_field_tables");
bad((p) => { for (const k of Object.keys(p.core.inputs)) p.oi.inputs[k] = { version: "tampered" }; }, "input_version_mismatch");
assert.throws(() => W.assemble({ oi: parts.oi }), /core_required/);
assert.throws(() => W.assemble({ core: parts.core, extra: parts.oi }), /unknown_part/);
// the same input appearing in two parts must compare equal and then merges
const shared = copy(parts);
for (const k of Object.keys(shared.core.inputs)) shared.oi.inputs[k] = copy(shared.core.inputs[k]);
assert.strictEqual(W.assemble(shared).parts.length, 7);
// presence overlap: object in one row, null in another is allowed; same-row overlap is not
const disjoint = copy(parts.core);
disjoint.columns = [[["a"], [0], "j", ["c", null]], [["a", "b"], [1], "j", ["c", 1]]];
assert.doesNotThrow(() => W.decodeRows(disjoint));
disjoint.columns = [[["a"], [0], "j", ["c", {}]], [["a", "b"], [0], "j", ["c", 1]]];
assert.throws(() => W.decodeRows(disjoint), /overlapping_path/);
// pollution attempts do not touch prototypes
assert.strictEqual(({}).polluted, undefined);
console.log("Pulse wire: exact reconstruction vs Python reference, partial parts, " + 24 + " negative vectors passed");
