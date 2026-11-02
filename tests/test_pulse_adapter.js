"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const model = require("../frontend/src/pulse/pulse_model.js");
const A = require("../frontend/src/pulse/pulse_adapter.js");
const load = (n) => JSON.parse(fs.readFileSync(path.join(__dirname, "../docs/contracts/fixtures", n), "utf8"));

// valid fixtures adapt, index and select without error, on the exact design model
for (const h of ["1H", "4H", "24H"]) {
  const wire = load(`snapshot.valid.${h}.json`);
  const now = wire.generated_ms + 1000;
  const snap = A.adapt(wire, now);
  assert.strictEqual(snap.horizon, h);
  assert.strictEqual(snap.rows.length, 1);
  const row = snap.rows[0];
  assert.strictEqual(row.id, "B-BTC_USDT");
  assert.strictEqual(row.price, 102);
  assert.strictEqual(row.metrics.open_interest["1H"].value, 0.12);
  // Funding windows are Current / 1D / 7D: three different wire fields, not the estimate repeated
  assert.strictEqual(row.metrics.funding["1H"].text, "0.0002");
  assert.strictEqual(row.metrics.funding["24H"].text, "0.00215");
  assert.notStrictEqual(row.metrics.funding["4H"].text, row.metrics.funding["1H"].text);
  assert.notStrictEqual(row.metrics.funding["4H"].text, row.metrics.funding["24H"].text);
  assert.strictEqual(row.signal, "Uptrend");
  assert.strictEqual(row.context.interval, 8);
  const index = new model.Index(snap);
  for (const m of model.REGISTRY) for (const w of model.WINDOWS) {
    const res = index.select({ metric: m.id, window: w }, now);
    assert.strictEqual(res.ids.length, 1);
    assert.strictEqual(model.effective(row.metrics[m.id][w], now), "available", m.id + w);
  }
}
// expired fixture: the envelope is expired; each field is judged by its own expiry (contract)
{
  const wire = load("snapshot.expired.json");
  const now = 1800000000000;
  const snap = A.adapt(wire, now);
  assert.strictEqual(snap.envelope_expired, true);
  const row = snap.rows[0];
  for (const w of model.WINDOWS) {
    for (const id of ["volatility", "long_short", "open_interest"]) {
      assert.strictEqual(row.metrics[id][w].value, null, id);
      assert.strictEqual(model.effective(row.metrics[id][w], now), "unavailable");
    }
    // estimate expired at 1799999939000: never shown as a number
    if (w === "1H") assert.strictEqual(row.metrics.funding[w].value, null);
    // settled-derived values are valid until due (nft 1800028619000) even when the cache copy is old
    assert.strictEqual(row.metrics.funding_change[w].status, "available");
    assert.strictEqual(model.effective(row.metrics.funding_change[w], now), "available");
  }
  assert.strictEqual(row.price, null);
  assert.strictEqual(row.volume, null);
}
// unavailable fixture: pair stays, nothing numeric, no zero placeholders
{
  const wire = load("snapshot.unavailable.json");
  const snap = A.adapt(wire, wire.generated_ms + 1);
  assert.strictEqual(snap.rows.length, 1);
  for (const m of model.REGISTRY) for (const w of model.WINDOWS) {
    const c = snap.rows[0].metrics[m.id][w];
    assert.strictEqual(c.value, null);
    assert.ok(c.reason);
  }
}
// partial fixture: three contracts (8H, 4H, 1H), per-field reasons kept
{
  const wire = load("snapshot.partial.json");
  const snap = A.adapt(wire, wire.generated_ms + 1000);
  assert.deepStrictEqual(snap.rows.map((r) => r.context.interval), [8, 4, 1]);
  const doge = snap.rows[2];
  const reasons = new Set();
  for (const m of model.REGISTRY) for (const w of model.WINDOWS) reasons.add(doge.metrics[m.id][w].reason);
  assert.ok(reasons.has("insufficient_history") || reasons.has(null));
  new model.Index(snap);
}
// settlement due: settled fields stop at nft; pending, never the prior number
{
  const wire = load("snapshot.valid.1H.json");
  const fc = wire.rows[0].funding_change;
  const snap = A.adapt(wire, fc.nft - 1);
  assert.notStrictEqual(snap.rows[0].metrics.funding_change["24H"].value, null);
  const later = A.adapt({ ...wire, expires_ms: fc.nft + 10 }, fc.nft);
  assert.strictEqual(later.rows[0].metrics.funding_change["24H"].value, null);
  assert.strictEqual(later.rows[0].metrics.funding_change["24H"].reason, "settlement_pending");
}
// fail closed
const good = load("snapshot.valid.1H.json");
const t = good.generated_ms + 1;
const bad = (mut, code) => {
  const w = JSON.parse(JSON.stringify(good));
  mut(w);
  assert.throws(() => A.adapt(w, t), new RegExp(code), code);
};
bad((w) => (w.schema = "pulse.snapshot.v2"), "unknown_schema");
bad((w) => (w.mapping_version = "x"), "invalid_identity");
bad((w) => (w.horizon = "2H"), "invalid_horizon");
bad((w) => (w.universe[0].id = 5), "invalid_dictionary");
bad((w) => w.rows.push(w.rows[0]), "invalid_dictionary");
bad((w) => (w.rows[0].id = 3), "row_dictionary_mismatch");
bad((w) => (w.universe[0].pair_id = "BTC"), "invalid_dictionary");
bad((w) => (w.rows[0].price.status = "bogus"), "unknown_status");
bad((w) => (w.expires_ms = w.generated_ms), "invalid_envelope_time");

// live funding: merge, mapping mismatch fails closed, expiry is source-derived
{
  const wire = good;
  const base = A.adapt(wire, t);
  const fc = wire.rows[0].funding_change;
  const doc = (o = {}) => ({
    schema: A.LIVE_SCHEMA, mapping_version: wire.mapping_version, sequence: 1, generated_ms: t, expires_ms: t + 60000,
    rows: [{ id: 0, status: "available", reason: null, h: fc.h, nft: fc.nft, fr: fc.settled_rate, efr: "0.00012", source_ms: t - 500, expires_ms: fc.nft, ...o }],
  });
  let r = A.applyLiveFunding(base, wire, doc(), t);
  assert.strictEqual(r.result.applied, 1);
  const f = r.snapshot.rows[0].metrics.funding["1H"];
  assert.strictEqual(f.text, "0.00012");
  assert.strictEqual(f.live, true);
  assert.strictEqual(r.snapshot.rows[0].metrics.funding["24H"].text, "0.00215", "7D cumulative is untouched by the live estimate");
  const d = r.snapshot.rows[0].metrics.funding_change["24H"].current_delta;
  assert.strictEqual(d.status, "available");
  assert.strictEqual(d.bps, "-0.3");
  assert.strictEqual(d.classification_status, "unclassified");
  assert.strictEqual(base.rows[0].metrics.funding["1H"].text, "0.0002", "input snapshot not mutated");
  r = A.applyLiveFunding(base, wire, { ...doc(), mapping_version: "sha256:" + "0".repeat(64) }, t);
  assert.strictEqual(r.result.reason, "live_mapping_or_schema_mismatch");
  assert.strictEqual(r.snapshot, base);
  r = A.applyLiveFunding(base, wire, doc({ source_ms: t - 130000 }), t);
  assert.strictEqual(r.result.unavailable, 1);
  assert.strictEqual(r.snapshot.rows[0].metrics.funding["1H"].text, "0.0002", "stale live row keeps the settled-document estimate only until its own expiry");
  assert.strictEqual(r.snapshot.rows[0].metrics.funding_change["24H"].current_delta.status, "unavailable");
  r = A.applyLiveFunding(base, wire, doc({ nft: fc.nft + 1 }), t);
  assert.strictEqual(r.snapshot.rows[0].metrics.funding_change["24H"].current_delta.reason, "cycle_identity_mismatch");
}
console.log("Pulse adapter: fixtures, fail-closed and live merge checks passed");
// Independently timed valid shares cannot replace the verified common-bucket divergence.
{
  const wire=load("snapshot.valid.1H.json"),now=wire.generated_ms+1000;
  wire.rows[0].ls.divergence={...wire.rows[0].ls.divergence,status:"available",value:"1.04"};
  assert.strictEqual(A.adapt(wire,now).rows[0].context.shareGapPp,1.04);
  wire.rows[0].ls.divergence={...wire.rows[0].ls.divergence,status:"unavailable",value:null};
  assert.strictEqual(A.adapt(wire,now).rows[0].context.shareGapPp,null);
}
// funding_meta carries the real settlement operands for the detail view
{
  const wire = load("snapshot.valid.1H.json");
  const snap = A.adapt(wire, wire.generated_ms + 1000);
  const m = snap.rows[0].funding_meta;
  assert.strictEqual(m.status, "available");
  assert.strictEqual(m.nft, wire.rows[0].funding_change.nft);
  assert.strictEqual(m.settled_rate, wire.rows[0].funding_change.settled_rate);
  const late = A.adapt(wire, wire.rows[0].funding_change.expires_ms + 1);
  assert.strictEqual(late.rows[0].funding_meta.status, "unavailable");
}
console.log("Pulse adapter: funding_meta checks passed");

// Review boundary: a live row's own expires_ms bounds the estimate; a valid retained base is never replaced by an expired live value
{
  const wire = load("snapshot.partial.json");
  const live = load("live_funding.valid.json");
  const now = 1800000001001;
  const base = A.adapt(wire, now - 1000);
  const baseline = base.rows[0].metrics.funding["1H"].text;
  const doc = JSON.parse(JSON.stringify(live));
  doc.mapping_version = wire.mapping_version;
  doc.rows[0].efr = "0.0003";
  doc.rows[0].expires_ms = 1800000001000;
  const r = A.applyLiveFunding(base, wire, doc, now);
  const f = r.snapshot.rows[0].metrics.funding["1H"];
  assert.notStrictEqual(f.text, "0.0003", "expired live estimate must not replace the base");
  assert.strictEqual(f.text, baseline, "valid retained base is kept");
  assert.notStrictEqual(f.live, true);
  assert.strictEqual(r.result.unavailable >= 1, true);
  // the same row inside its expiry does apply
  const inside = A.applyLiveFunding(base, wire, doc, 1800000000900);
  assert.strictEqual(inside.snapshot.rows[0].metrics.funding["1H"].text, "0.0003");
}
// Funding Chg windows are Last / 1D / 7D settled changes, each its own wire field
{
  const wire = structuredClone(load("snapshot.partial.json"));
  const fc = wire.rows[0].funding_change;
  fc.settled_last.value = "0.00005"; fc.settled_1d.value = "0.00012"; fc.settled_7d.value = "-0.0003";
  const snap = A.adapt(wire, 1800000000500);
  const texts = model.WINDOWS.map((w) => snap.rows[0].metrics.funding_change[w].text);
  assert.deepStrictEqual(texts, ["0.00005", "0.00012", "-0.0003"]);
  assert.strictEqual(snap.rows[0].metrics.funding_change["4H"].level, null, "1D settled change has no pilot band");
}
console.log("Pulse adapter: live row expiry boundary passed");
