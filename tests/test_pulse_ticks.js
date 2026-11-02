"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const W = require("../frontend/src/pulse/pulse_wire.js");
const A = require("../frontend/src/pulse/pulse_adapter.js");
const Ticks = require("../frontend/src/pulse/pulse_ticks.js");
const fx = path.join(__dirname, "../docs/contracts/fixtures");
const read = (...p) => JSON.parse(fs.readFileSync(path.join(fx, ...p), "utf8"));
const parts = Object.fromEntries(W.PARTS.map((p) => [p, read("wire", p + ".json")]));
const wire = W.assemble(parts).snapshot;
const CLOCK = 1800000000000;
const NOW = CLOCK + 5000;
const snapshot = A.adapt(wire, NOW);
const universe = snapshot.rows.map((r) => r.id);

// 1. a socket-style tick inside the ladder: price and all three returns move together, exact decimal
{
  const t = Ticks.create();
  assert.strictEqual(t.setFrame(snapshot, wire), 3);
  const e = t.ingest("B-BTC_USDT", { price: "103.125", source_ms: CLOCK + 4000 }, NOW);
  assert.ok(e);
  assert.strictEqual(e.price, 103.125);
  for (const w of Ticks.WINDOWS) assert.ok(Math.abs(e.changes[w] - 0.03125) < 1e-12, w);
  assert.strictEqual(e.expires_at, Math.min(CLOCK + 4000 + 60000, wire.rows[0].references.ladder.valid_until_ms));
  assert.strictEqual(t.get("B-BTC_USDT", NOW), e);
  assert.strictEqual(t.get("B-ETH_USDT", NOW), undefined, "pairs without a tick show the snapshot frame");
}
// 2. out-of-order ticks never overwrite a newer one; equal time replaces
{
  const t = Ticks.create(); t.setFrame(snapshot, wire);
  const a = t.ingest("B-BTC_USDT", { price: "103", source_ms: CLOCK + 4000 }, NOW);
  const old = t.ingest("B-BTC_USDT", { price: "101", source_ms: CLOCK + 3000 }, NOW);
  assert.strictEqual(old, a);
  assert.strictEqual(t.get("B-BTC_USDT", NOW).price, 103);
  const same = t.ingest("B-BTC_USDT", { price: "104", source_ms: CLOCK + 4000 }, NOW);
  assert.strictEqual(same.price, 104);
}
// 3. an invalid replacement retains the complete prior frame, never a mixed numerator/return.
{
  const t = Ticks.create(); t.setFrame(snapshot, wire);
  t.ingest("B-BTC_USDT", { price: "103", source_ms: CLOCK + 4000 }, NOW);
  assert.strictEqual(t.ingest("B-BTC_USDT", { price: "103", source_ms: CLOCK - 120000 }, NOW), t.get("B-BTC_USDT", NOW) || null);
  const bad = t.ingest("B-BTC_USDT", { price: "0", source_ms: CLOCK + 4500 }, NOW);
  assert.strictEqual(bad, t.get("B-BTC_USDT", NOW));
  assert.strictEqual(bad.price, 103);
  assert.strictEqual(t.ingest("B-BTC_USDT", { price: "103", source_ms: NOW + 5001 }, NOW), bad, "future tick cannot replace valid frame");
  assert.strictEqual(t.ingest("B-BTC_USDT", { price: "0", source_ms: NOW }, bad.expires_at), null);
  assert.strictEqual(t.ingest("B-UNKNOWN_USDT", { price: "1", source_ms: CLOCK + 4000 }, NOW), null);
}
// 4. expiry: after the numerator or the ladder expires the frame falls back to the snapshot
{
  const t = Ticks.create(); t.setFrame(snapshot, wire);
  t.ingest("B-BTC_USDT", { price: "103", source_ms: CLOCK + 4000 }, NOW);
  const until = t.get("B-BTC_USDT", NOW).expires_at;
  assert.ok(t.get("B-BTC_USDT", until - 1));
  assert.strictEqual(t.get("B-BTC_USDT", until), undefined);
}
// 5. documents: fixture in-ladder doc, foreign mapping, expired doc, unavailable rows
{
  const t = Ticks.create(); t.setFrame(snapshot, wire);
  const doc = read("live_price.in_ladder.json");
  assert.deepStrictEqual(t.ingestDocument(doc, universe, NOW).sort(), universe.slice().sort());
  assert.deepStrictEqual(t.ingestDocument(doc, universe, NOW), [], "same ticks again change nothing");
  assert.deepStrictEqual(t.ingestDocument(read("live_price.mapping_mismatch.json"), universe, NOW), []);
  assert.deepStrictEqual(t.ingestDocument(doc, universe, doc.expires_ms), []);
  const gone = JSON.parse(JSON.stringify(doc)); gone.rows[1] = { ...gone.rows[1], status: "unavailable", price: null };
  const t2 = Ticks.create(); t2.setFrame(snapshot, wire);
  assert.strictEqual(t2.ingestDocument(gone, universe, NOW).length, 2);
}
// 6. generation handoff retains a complete valid frame with its original provenance.
{
  const t = Ticks.create(); t.setFrame(snapshot, wire);
  t.ingest("B-BTC_USDT", { price: "103", source_ms: CLOCK + 4000 }, NOW);
  const wire2 = structuredClone(wire); wire2.generation = "sha256:" + "9".repeat(64);
  for (const r of wire2.rows) r.references.generation_id = wire2.generation;
  t.setFrame(A.adapt(wire2, NOW), wire2);
  assert.strictEqual(t.size(), 1);
  const retained=t.get("B-BTC_USDT",NOW);
  assert.strictEqual(retained.reference_generation,wire.generation);
  const missing=structuredClone(wire2);missing.rows[0].references.status="unavailable";
  t.setFrame(A.adapt(wire2,NOW),missing);
  const priceOnly=t.ingest("B-BTC_USDT",{price:"109",source_ms:CLOCK+4500},NOW);
  assert.strictEqual(priceOnly.price,109,"verified price survives missing references");
  assert.deepStrictEqual(priceOnly.changes,{"1H":.09,"4H":.09,"24H":.09},"recompute from original valid references, never copy the old 3% return");
  assert.strictEqual(priceOnly.reference_generation,wire.generation);
  t.setFrame(A.adapt(wire2,NOW),wire2);
  assert.strictEqual(t.ingest("B-BTC_USDT",{price:"104",source_ms:CLOCK+4500},NOW).reference_generation,wire2.generation);
  const changed={...A.adapt(wire2,NOW),mapping_version:"sha256:"+"7".repeat(64)};
  t.setFrame(changed,wire2);
  assert.strictEqual(t.size(),0,"mapping changes invalidate old frames");
  // ticks computed against the old ladder identity would be refused by the pin
  const old = Ticks.create(); old.setFrame(snapshot, wire);
  const stale = structuredClone(wire); stale.rows[0].references.generation_id = "sha256:" + "8".repeat(64);
  const t3 = Ticks.create(); t3.setFrame(snapshot, stale);
  const unpinned=t3.ingest("B-BTC_USDT", { price: "103", source_ms: CLOCK + 4000 }, NOW);
  assert.strictEqual(unpinned.price,103);assert.deepStrictEqual(unpinned.changes,{});
}
// Missing references never hide a valid numerator; same-tick reference arrival heals returns.
{
  const t=Ticks.create(), missing=structuredClone(wire);
  missing.rows[0].references.status="unavailable";
  t.setFrame(snapshot,missing);
  const tick={price:"104",source_ms:CLOCK+4000};
  const partial=t.ingest("B-BTC_USDT",tick,NOW);
  assert.strictEqual(partial.price,104);assert.deepStrictEqual(partial.changes,{});
  t.setFrame(snapshot,wire,NOW);
  const healed=t.get("B-BTC_USDT",NOW);
  assert.ok(Number.isFinite(healed.changes["24H"]));
  const t2=Ticks.create();t2.setFrame(snapshot,missing);
  for(const invalid of [{price:"0"}, {price:"NaN"}, {source_ms:NOW+5001}, {expires_ms:NOW}, {expires_ms:0}])
    assert.strictEqual(t2.ingest("B-BTC_USDT",{...tick,...invalid},NOW),null);
  const short=t.ingest("B-BTC_USDT",{...tick,expires_ms:NOW+100},NOW);
  assert.strictEqual(short.expires_at,NOW+100);
  assert.strictEqual(t.get("B-BTC_USDT",NOW+100),undefined);
}
// Reference expiry clears percentages, not a still-valid price or its original expiry.
{
  const t=Ticks.create();t.setFrame(snapshot,wire);
  const until=wire.rows[0].references.ladder.valid_until_ms;
  const e=t.ingest("B-BTC_USDT",{price:"104",source_ms:until-1000},until-500);
  assert.ok(Object.keys(e.changes).length);
  const partial=t.get("B-BTC_USDT",until);
  assert.strictEqual(partial.price,104);assert.deepStrictEqual(partial.changes,{});
  assert.strictEqual(partial.expires_at,e.expires_at);
}
// 7. latency statistics
{
  const t=Ticks.create();t.setFrame(snapshot,wire);
  t.ingest("B-BTC_USDT",{price:"103",source_ms:CLOCK+4000},NOW);
  const newer=structuredClone(wire),newGen="sha256:"+"d".repeat(64);
  newer.generation=newGen;
  for(const row of newer.rows){
    row.references.generation_id=newGen;
    const l=row.references.ladder;l.built_at_ms+=60000;l.start_ms+=60000;l.valid_until_ms+=60000;
    for(const slot of l.slots)for(const [i,ref]of slot.entries()){
      ref.reference_minute_ms+=60000;ref.open_price="200";
      if(i===2){ref.status="unavailable";ref.reason="source_unavailable";ref.open_price=null;}
    }
  }
  t.setFrame(A.adapt(newer,CLOCK+65000),newer,CLOCK+65000);
  const tick=t.ingest("B-BTC_USDT",{price:"110",source_ms:CLOCK+64001},CLOCK+65000);
  assert.equal(tick.changes["1H"],-.45);assert.equal(tick.changes["4H"],-.45);assert.equal(tick.changes["24H"],.1);
  assert.equal(tick.reference_provenance["24H"].generation,wire.generation);
  assert.equal(tick.reference_provenance["1H"].generation,newGen);
  const until=wire.rows[0].references.ladder.valid_until_ms;
  t.ingest("B-BTC_USDT",{price:"110",source_ms:until-1000},until-500);
  const later=t.get("B-BTC_USDT",until);
  assert.equal(later.price,110);assert.equal(later.changes["1H"],-.45);
  assert.equal(later.changes["24H"],undefined,"one window's expired reference cannot erase others or price");
  const fallback=t.display(snapshot.rows[1],snapshot.rows[1].price_expires_ms);
  assert.equal(fallback.price,null,"expired fallback cannot revive a price");
}
// 7. latency statistics
{
  const t = Ticks.create(); t.setFrame(snapshot, wire);
  const e = t.ingest("B-BTC_USDT", { price: "103", source_ms: CLOCK + 4000 }, NOW);
  for (let i = 1; i <= 100; i++) t.recordPaint(e, e.source_ms + i);
  const l = t.latency();
  assert.strictEqual(l.samples, 100);
  assert.ok(l.p50 >= 40 && l.p50 <= 60 && l.p95 >= 90 && l.max === 100, JSON.stringify(l));
  assert.deepStrictEqual(Ticks.create().latency(), { samples: 0, p50: null, p95: null, max: null });
}
// 8. cost: 504 pairs, a document every 2 s must stay far below a frame
{
  const big = structuredClone(wire);
  const rows = [], universeBig = [];
  for (let i = 0; i < 504; i++) { const r = structuredClone(wire.rows[0]); r.id = i; rows.push(r); }
  big.rows = rows;
  big.universe = Array.from({ length: 504 }, (_, i) => ({ id: i, pair_id: `B-T${i}_USDT`, symbol: `T${i}USDT`, funding_hours: 8 }));
  const snap = { ...snapshot, rows: rows.map((r, i) => ({ ...snapshot.rows[0], id: `B-T${i}_USDT`, wire_id: i })) };
  const t = Ticks.create(); t.setFrame(snap, big);
  const doc = { schema: "pulse.live_price.v1", mapping_version: snap.mapping_version, sequence: 1, generated_ms: NOW, expires_ms: NOW + 15000,
    rows: rows.map((r, i) => ({ id: i, status: "available", reason: null, source_ms: CLOCK + 4000, expires_ms: CLOCK + 64000, input_refs: [], price: "103.125" })), inputs: {} };
  const ids = snap.rows.map((r) => r.id);
  const times = [];
  for (let k = 0; k < 20; k++) { const s = performance.now(); t.ingestDocument({ ...doc, rows: doc.rows.map((r) => ({ ...r, source_ms: CLOCK + 4000 + k + 1 })) }, ids, NOW); times.push(performance.now() - s); }
  times.sort((a, b) => a - b);
  console.log("504-row price document ingest: p50", times[10].toFixed(2), "ms, max", times[19].toFixed(2), "ms");
  assert.ok(times[10] < 25, "ingesting 504 pairs must be a small fraction of a 16 ms frame budget in a worker-free loop: " + times[10]);
}
console.log("Pulse ticks: frame pinning, ordering, expiry, documents, latency stats and 504-pair cost passed");
