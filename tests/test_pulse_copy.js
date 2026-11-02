"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { liveCopy } = require("../frontend/src/pulse/pulse_copy.js");
const root = path.join(__dirname, "../frontend/src/pulse");
const banned = /sample|illustrative|fictional|fixture/i;
// every user-visible phrase in the design controller and HTML is rewritten or free of sample wording
const sources = ["pulse_design.js", "pulse.html"].map((f) => fs.readFileSync(path.join(root, f), "utf8"));
const literals = new Set();
for (const text of sources) {
  for (const m of text.matchAll(/"([^"\n]{3,300})"/g)) literals.add(m[1]);
  for (const m of text.matchAll(/'([^'\n]{3,300})'/g)) literals.add(m[1]);
}
for (const text of sources[1].split(/<[^>]+>/)) {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length > 2) literals.add(t);
}
// fragments joined later (the whole string is rewritten) and static popover text replaced at boot in live mode
const replacedAtBoot = new Set([
  "Fixture created ",
  "Illustrative data · not live",
  "Layout and interaction review only. Levels are sample assignments, not calibrated signals. Trading is disabled.",
]);
assert.ok(sources[0].includes("Live preview · staging data") && sources[0].includes("openGuide();"), "boot code replaces the popover copy and the info icon opens the guide");
const offenders = [];
for (const s of literals) {
  if (!banned.test(s) || replacedAtBoot.has(s)) continue;
  const out = liveCopy(s);
  // identifiers, ids, comments and class names are not user-visible copy
  if (banned.test(out) && !/^[#.\w-]+$/.test(s) && !/preview-|retry-sample|Preview state|createPulseDesignFixture|sample:|\bsample\b: /.test(s)) offenders.push([s, out]);
}
assert.deepStrictEqual(offenders, [], "live copy still contains sample wording: " + JSON.stringify(offenders, null, 1));
// exact rewrites
assert.strictEqual(liveCopy("Sample native funding estimate: 2 bps."), "Native funding estimate: 2 bps.");
assert.strictEqual(liveCopy("Sample · Uptrend since $1"), "Uptrend since $1");
assert.strictEqual(liveCopy("Sample data · not live. Intended source: Binance"), "Source: Binance");
assert.strictEqual(liveCopy("Next settlement · sample"), "Next settlement");
assert.strictEqual(liveCopy("BTC"), "BTC");
assert.strictEqual(liveCopy("SAMPLEUSDT"), "SAMPLEUSDT");
assert.strictEqual(liveCopy(5), 5);
console.log("Pulse live copy: " + literals.size + " literals scanned; no sample wording remains in live mode");
