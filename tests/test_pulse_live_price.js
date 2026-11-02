"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const W = require("../frontend/src/pulse/pulse_wire.js");
const A = require("../frontend/src/pulse/pulse_adapter.js");
const fx = path.join(__dirname, "../docs/contracts/fixtures");
const read = (...p) => JSON.parse(fs.readFileSync(path.join(fx, ...p), "utf8"));
const parts = (dir) => Object.fromEntries(W.PARTS.map((p) => [p, read(dir, p + ".json")]));
const clone = (v) => structuredClone(v);

const all = parts("wire");
const core = W.assemble({ core: all.core, oi: all.oi }).snapshot; // references NOT loaded
const full = W.assemble(all).snapshot; // references loaded
const CLOCK = 1800000000000;
const NOW = CLOCK + 5000;

// 1. in-ladder price: exact 100 baselines at 103.125 give 0.03125 in every window; price and return move together
{
  const doc = read("live_price.in_ladder.json");
  const base = A.adapt(full, NOW);
  const { snapshot, result } = A.applyLivePrice(base, full, doc, NOW);
  assert.strictEqual(result.applied, 3, JSON.stringify(result));
  for (const row of snapshot.rows) {
    assert.strictEqual(row.price, 103.125);
    for (const w of ["1H", "4H", "24H"]) assert.ok(Math.abs(row.changes[w] - 0.03125) < 1e-12, w);
    assert.deepStrictEqual(row.price_live, { source_ms: 1800000004000, expires_ms: 1800000064000 });
  }
  assert.strictEqual(snapshot.price_expires_at, 1800000064000);
  assert.notStrictEqual(base.rows[0].price, 103.125, "input snapshot not mutated");
}
// 2. tick preceding ladder: a verified price still shows, with unavailable returns
{
  const doc = read("live_price.valid.json");
  const base = A.adapt(full, NOW);
  const { snapshot, result } = A.applyLivePrice(base, full, doc, NOW);
  assert.strictEqual(result.applied, 3);
  assert.strictEqual(snapshot.rows[0].price,Number(doc.rows[0].price));
  assert.ok(Object.values(snapshot.rows[0].changes).every(v=>v===null));
}
// 3. references not loaded: fresh numerator, unavailable returns, explicit pending reason
{
  const base = A.adapt(core, NOW);
  const { snapshot, result } = A.applyLivePrice(base, core, read("live_price.in_ladder.json"), NOW);
  assert.strictEqual(result.reason, "references_pending");
  assert.strictEqual(snapshot.rows[0].price, 103.125);
  assert.ok(Object.values(snapshot.rows[0].changes).every(v=>v===null));
  assert.strictEqual(result.applied, 3);
}
// 4. fail closed: foreign mapping, wrong schema, expired document, unavailable rows, stale numerator
{
  const base = A.adapt(full, NOW);
  const doc = read("live_price.in_ladder.json");
  assert.strictEqual(A.applyLivePrice(base, full, read("live_price.mapping_mismatch.json"), NOW).result.reason, "live_mapping_or_schema_mismatch");
  assert.strictEqual(A.applyLivePrice(base, full, { ...doc, schema: "pulse.live_price.v2" }, NOW).result.reason, "live_mapping_or_schema_mismatch");
  assert.strictEqual(A.applyLivePrice(base, full, doc, doc.expires_ms).result.reason, "live_document_expired");
  const unavailable = read("live_price.unavailable.json");
  unavailable.mapping_version = base.mapping_version;
  assert.strictEqual(A.applyLivePrice(base, full, unavailable, NOW).result.applied, 0);
  const stale = clone(doc);
  for (const r of stale.rows) r.expires_ms = NOW; // numerator expired
  const out = A.applyLivePrice(base, full, stale, NOW);
  assert.strictEqual(out.result.applied, 0);
  assert.strictEqual(out.snapshot.rows[0].price, base.rows[0].price);
}
// 5. a different generation's references never denominate (the ladder pins the generation)
{
  const other = clone(full);
  const base = A.adapt(other, NOW);
  const wrong = clone(other);
  for (const r of wrong.rows) r.references.generation_id = "sha256:" + "0".repeat(64);
  const { snapshot, result } = A.applyLivePrice(base, wrong, read("live_price.in_ladder.json"), NOW);
  assert.strictEqual(result.applied, 3);
  assert.ok(Object.values(snapshot.rows[0].changes).every(v=>v===null));
}
// 6. one row unavailable does not disturb the others
{
  const doc = clone(read("live_price.in_ladder.json"));
  doc.rows[1] = { ...doc.rows[1], status: "unavailable", reason: "source_unavailable", price: null };
  const base = A.adapt(full, NOW);
  const { snapshot, result } = A.applyLivePrice(base, full, doc, NOW);
  assert.strictEqual(result.applied, 2);
  assert.strictEqual(snapshot.rows[1].price, base.rows[1].price);
  assert.strictEqual(snapshot.rows[0].price, 103.125);
}
// 7. classified wire fixture: v1 level and native Stable reconstruct and adapt (the UI label Steady is display only)
{
  const classified = W.assemble(parts("wire_classified_stable")).snapshot;
  const logical = read("snapshot.classified_stable.json");
  assert.strictEqual(W.canonical(classified), W.canonical(logical), "classified wire rebuilds the logical fixture exactly");
  const snap = A.adapt(classified, classified.generated_ms + 1000);
  const levels = snap.rows.flatMap((r) => ["1H", "4H", "24H"].map((w) => r.metrics.volatility[w].level));
  assert.ok(levels.every((l) => [1, 2, 3].includes(l)), "levels " + levels);
  assert.ok(snap.rows.some((r) => r.metrics.volatility["1H"].direction === "Steady"), "native Stable shown as Steady");
}
console.log("Pulse live price: in-ladder, fail-closed, references pending, generation pin, classified wire checks passed");
