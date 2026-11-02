"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const D = require("../frontend/src/pulse/pulse_decimal.js");
const L = require("../frontend/src/pulse/pulse_live.js");

// 1. shared exact-decimal vectors generated independently with Python Decimal
const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/pulse_live/decimal_vectors.json"), "utf8"));
for (const c of vectors.cases) {
  const diff = D.sub(D.parse(c.efr), D.parse(c.settled));
  assert.strictEqual(D.toString(diff), c.diff, `diff ${c.efr}-${c.settled}`);
  assert.strictEqual(D.toString(D.mulPow10(diff, 4)), c.bps, `bps ${c.efr}-${c.settled}`);
}
// grammar and bounds
for (const bad of ["", "NaN", "Infinity", "1,5", "0x10", ".5", "5.", "--1", "1e999", "1e-999", "1".repeat(65), null, 5, undefined])
  assert.strictEqual(D.parse(bad), null, String(bad));
assert.strictEqual(D.toString(D.parse("-0.000")), "0");
assert.strictEqual(D.toString(D.parse("+1.2300")), "1.23");
assert.strictEqual(D.toString(D.parse("1e-8")), "0.00000001");
assert.strictEqual(D.compare(D.parse("0.1000"), D.parse("0.1")), 0);

// 2. Current Delta pairing contract
const NFT = 1792987200000;
const envelope = { mapping_version: "m1" };
const settled = { status: "available", mapping_version: "m1", id: "B-BTC_USDT", h: 8, nft: NFT, settlement_ms: NFT - 8 * 3600000, settled_rate: "0.00019" };
const live = (o = {}) => ({ id: "B-BTC_USDT", status: "available", h: 8, nft: NFT, fr: "0.00019", efr: "0.00012",
  source_ms: NFT - 600000, expires_ms: NFT, ...o });
const now = NFT - 590000;
let r = L.currentDelta(envelope, live(), settled, now);
assert.strictEqual(r.status, "available");
assert.strictEqual(r.value, "-0.00007");
assert.strictEqual(r.bps, "-0.7");
assert.strictEqual(r.classification_status, "unclassified");
assert.strictEqual(r.expires_ms, NFT - 600000 + 120000);
// the tiny-difference case that Number subtraction gets wrong
r = L.currentDelta(envelope, live({ efr: "0.100000000000000000001", fr: "0.1" }), { ...settled, settled_rate: "0.1" }, now);
assert.strictEqual(r.value, "0.000000000000000000001");
// every mismatch is unavailable, never an old value
const cases = [
  ["mapping_mismatch", live(), { ...settled, mapping_version: "m2" }],
  ["identity_mismatch", live({ id: "B-ETH_USDT" }), settled],
  ["cycle_identity_mismatch", live({ nft: NFT + 28800000 }), settled],
  ["cycle_identity_mismatch", live({ h: 4 }), settled],
  ["settled_rate_mismatch", live({ fr: "0.00018" }), settled],
  ["live_unavailable", live({ status: "unavailable", reason: "live_unavailable" }), settled],
  ["invalid_decimal", live({ efr: "1e999" }), settled],
  ["future_estimate", live({ source_ms: now + 5001 }), settled],
];
for (const [reason, row, s] of cases) {
  r = L.currentDelta(envelope, row, s, now);
  assert.strictEqual(r.status, "unavailable", reason);
  assert.strictEqual(r.reason, reason);
  assert.strictEqual(r.value, null);
}
// freshness: source-derived expiry; 304/receipt time never extends it
assert.strictEqual(L.currentDelta(envelope, live(), settled, NFT - 600000 + 119999).status, "available");
assert.strictEqual(L.currentDelta(envelope, live(), settled, NFT - 600000 + 120000).reason, "stale_estimate");
// due: the cycle ended, estimate belongs to the old cycle
assert.strictEqual(L.currentDelta(envelope, live({ source_ms: NFT - 1000 }), settled, NFT).reason, "settlement_pending");
assert.throws(() => L.currentDelta(envelope, live(), settled, -1), RangeError);
// ---- Review boundary witnesses (canonical Python oracle) ----
{
  const t = L.currentDelta(envelope, live({ source_ms: settled.settlement_ms - 2000, expires_ms: NFT }), settled, settled.settlement_ms + 1000);
  assert.strictEqual(t.status, "unavailable", "estimate before the retained settlement");
  assert.strictEqual(t.reason, "cycle_identity_mismatch");
  // positive nft jitter: actual due passed, nft not yet
  const due = settled.settlement_ms + 8 * 3600000;
  const j = L.currentDelta(envelope, live({ nft: due + 3000, source_ms: due + 500, expires_ms: due + 3000 }), { ...settled, nft: due + 3000 }, due + 1000);
  assert.strictEqual(j.status, "unavailable");
  assert.strictEqual(j.reason, "settlement_pending");
  // estimate stamped at or after due is not this cycle even if now is earlier (skew)
  const k = L.currentDelta(envelope, live({ nft: due + 3000, source_ms: due + 2000, expires_ms: due + 3000 }), { ...settled, nft: due + 3000 }, due - 1000);
  assert.strictEqual(k.reason, "cycle_identity_mismatch");
  // row expiry bounds the estimate
  const e = L.currentDelta(envelope, live({ expires_ms: now + 1000 }), settled, now + 1000);
  assert.strictEqual(e.status, "unavailable");
}
console.log("Pulse exact decimal and Current Delta: " + (vectors.cases.length + 15) + " checks passed");
