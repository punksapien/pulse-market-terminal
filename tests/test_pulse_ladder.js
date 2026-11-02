"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const L = require("../frontend/src/pulse/pulse_ladder.js");
const v = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/pulse_price/ladder_vectors.json"), "utf8"));
const CTX = { mapping_version: "m1", definition_version: "price_change.v1", generation_id: "g1" };
let n = 0;
for (const c of v.cases) {
  const ctx = c.generation_id === CTX.generation_id ? CTX : { ...CTX, generation_id: c.generation_id };
  // the Python oracle pinned generation "g1" in the ladder and passed the snapshot's (possibly different) id
  const r = L.priceChange(c.refs, c.tick, c.horizon, ctx, c.now_ms);
  assert.strictEqual(r.status, c.expected.status, c.id);
  assert.strictEqual(r.reason, c.expected.reason, c.id);
  if (c.expected.fraction !== null) {
    const expected = Number(c.expected.fraction);
    assert.ok(Math.abs(r.number - expected) <= Number(v.bound_factor) * Math.max(1, Math.abs(expected)), c.id);
  } else assert.strictEqual(r.number, null, c.id);
  n++;
}
// rollover before the next snapshot: old minute 100, new minute 110 style, tick equals new reference -> 0
const roll = v.cases.find((c) => c.id === "rollover_1H");
assert.strictEqual(L.priceChange(roll.refs, roll.tick, "1H", CTX, roll.now_ms).status, "available");

// fallback path uses the snapshot price at its own source time
const refs = JSON.parse(JSON.stringify(v.cases[0].refs));
const MIN = 60000;
const T0 = v.t0;
const source = T0 - 40000; // 12:00 minus 40 s: before the ladder start
const ref = (h, ms) => ({ status: "available", reason: null, reference_minute_ms: Math.floor((source - ms) / MIN) * MIN, open_price: "100" });
refs.fallback = { source_ms: source, price: "101", expires_ms: source + 300000,
  references: { "1H": ref("1H", 3600000), "4H": ref("4H", 14400000), "24H": ref("24H", 86400000) } };
let f = L.fallbackChange(refs, "1H", CTX, T0 + 30000);
assert.strictEqual(f.status, "available");
assert.ok(Math.abs(f.number - 0.01) < 1e-12);
assert.strictEqual(f.delayed, false);
f = L.fallbackChange(refs, "1H", CTX, source + 120001);
assert.strictEqual(f.delayed, true);
assert.strictEqual(f.status, "available");
assert.strictEqual(L.fallbackChange(refs, "1H", CTX, source + 300000).reason, "fallback_expired");
refs.fallback.references["1H"].reference_minute_ms += MIN;
assert.strictEqual(L.fallbackChange(refs, "1H", CTX, T0 + 30000).reason, "reference_missing_or_invalid");
assert.strictEqual(L.fallbackChange(refs, "1H", { ...CTX, mapping_version: "m2" }, T0 + 30000).reason, "version_mismatch");
// best(): a valid tick wins, an invalid tick falls back
const ok = v.cases.find((c) => c.id === "slot0_start_1H");
assert.strictEqual(L.best(ok.refs, ok.tick, "1H", CTX, ok.now_ms).source, "live");
refs.fallback.references["1H"].reference_minute_ms -= MIN;
assert.strictEqual(L.best(refs, { ...ok.tick, price: "0" }, "1H", CTX, T0 + 30000).source, "fallback");
assert.throws(() => L.priceChange(ok.refs, ok.tick, "2H", CTX, ok.now_ms), RangeError);
console.log(`Pulse ladder price change: ${n} Python-oracle vectors + fallback checks passed`);
