"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const Loader = require("../frontend/src/pulse/pulse_loader.js");
const W = require("../frontend/src/pulse/pulse_wire.js");
const flush = () => new Promise((r) => setImmediate(r));
const dir = path.join(__dirname, "../docs/contracts/fixtures/wire");
const golden = Object.fromEntries(W.PARTS.map((p) => [p, JSON.parse(fs.readFileSync(path.join(dir, p + ".json"), "utf8"))]));
const digest = (v) => W.digest(v, webcrypto.subtle);
const HEX = (c) => "sha256:" + c.repeat(64);

async function generation(g) {
  const parts = structuredClone(golden);
  if (g) for (const p of Object.values(parts)) { p.generation = g; if (p.header) p.header.generation = g; }
  const manifest = { schema: "pulse.manifest.v1", mapping_version: parts.core.mapping_version, generation: parts.core.generation, horizon: "1H",
    generated_ms: parts.core.generated_ms, expires_ms: parts.core.expires_ms, parts: {} };
  const bodies = new Map();
  for (const [name, doc] of Object.entries(parts)) {
    const h = await digest(doc);
    bodies.set(h, doc);
    manifest.parts[name] = { schema: "pulse.artifact.v1", logical_sha256: h, raw_bytes: 1000, mapping_version: doc.mapping_version, generation: doc.generation,
      variants: {} };
  }
  return { manifest, bodies, parts };
}

function rig(state, opts = {}) {
  let t = golden.core.generated_ms + 1000;
  const calls = [];
  const timers = new Map();
  let id = 1;
  const fetchFn = async (url, init) => {
    calls.push({ url, headers: init.headers, credentials: init.credentials });
    const r = await state.handler(url, init, calls);
    const headers = new Map(Object.entries(r.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
    return { status: r.status, headers: { get: (k) => headers.get(k.toLowerCase()) ?? null }, text: async () => r.body ?? "" };
  };
  const loader = Loader.create({ fetch: fetchFn, now: () => t, random: () => 0.5, snapshotMs: 30000, liveMs: 5000, wire: W, digest,
    setTimer: (fn, ms, kind) => { const k = id++; timers.set(k, { fn, ms, kind }); return k; }, clearTimer: (k) => timers.delete(k),
    snapshotUrl: (h) => `/api/pulse?horizon=${h}`, liveUrl: () => "/api/pulse?live=1", livePriceUrl: () => "/api/pulse?live=price",
    artifactUrl: (h) => "/api/pulse?artifact=" + encodeURIComponent(h), ...opts });
  return { loader, calls, timers, set: (v) => (t = v),
    async fire(kind) { const f = [...timers.entries()].find(([, x]) => x.kind === kind); if (!f) return; timers.delete(f[0]); f[1].fn(); await flush(); await flush(); await flush(); } };
}
const json = (b, h = {}) => ({ status: 200, body: JSON.stringify(b), headers: h });

(async () => {
  const g1 = await generation();
  const CLOCK = golden.core.generated_ms;
  const priceDoc = JSON.parse(fs.readFileSync(path.join(__dirname, "../docs/contracts/fixtures/live_price.in_ladder.json"), "utf8"));
  let priceResponse = () => json(priceDoc);
  const route = (cur) => (url, init) => {
    const u = new URL(url, "http://x");
    if (u.searchParams.get("horizon")) {
      const tag = '"' + cur().manifest.generation.slice(7, 15) + '"';
      return init.headers["If-None-Match"] === tag ? { status: 304 } : json(cur().manifest, { etag: tag });
    }
    if (u.searchParams.get("live") === "price") return priceResponse();
    if (u.searchParams.get("artifact")) {
      const body = cur().bodies.get(u.searchParams.get("artifact"));
      return body ? json(body) : { status: 404 };
    }
    return { status: 503 };
  };
  const settle = async (r) => {
    // WebCrypto runs on a worker pool; six event-loop turns do not prove it finished.
    const end = Date.now() + 2000;
    while (![...r.timers.values()].some((timer) => timer.kind === "snapshot")) {
      if (Date.now() >= end) throw Error("snapshot_test_did_not_settle");
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    await flush();
  };

  // 1. core + one metric render; others stay unavailable, never zero
  {
    let cur = g1;
    const r = rig({ handler: route(() => cur) });
    r.loader.start("1H", ["oi"]);
    await settle(r);
    const st = r.loader.state;
    assert.strictEqual(st.snapshotError, null, String(st.snapshotError));
    assert.strictEqual(st.phase, "ready");
    assert.deepStrictEqual(st.parts.loaded.sort(), ["core", "oi"]);
    assert.notStrictEqual(st.snapshot.rows[0].metrics.open_interest["1H"].value, null);
    assert.strictEqual(st.snapshot.rows[0].metrics.long_short["1H"].value, null);
    const artifactCalls = r.calls.filter((c) => c.url.includes("artifact=")).length;
    assert.strictEqual(artifactCalls, 2, "only core and oi are downloaded");
    assert.ok(r.calls.every((c) => c.credentials === "same-origin"), "manifest, artifact and live reads retain same-origin protection");

    // 2. opening another tab fetches just that part and joins atomically
    const ok = await r.loader.ensureParts(["ls"]);
    assert.strictEqual(ok, true);
    assert.strictEqual(r.calls.filter((c) => c.url.includes("artifact=")).length, 3);
    assert.notStrictEqual(r.loader.state.snapshot.rows[0].metrics.long_short["1H"].value, null);
    assert.strictEqual(r.loader.state.phase, "ready");
    await assert.rejects(() => r.loader.ensureParts(["nope"]), RangeError);

    // 3. unchanged manifest (304): nothing is downloaded again
    const before = r.calls.length;
    await r.fire("snapshot");
    assert.strictEqual(r.calls.slice(before).filter((c) => c.url.includes("artifact=")).length, 0);
    assert.strictEqual(r.loader.state.phase, "ready");
    r.loader.stop();
  }
  // 4. a new generation replaces atomically; unneeded old parts are dropped; wanted parts refetched
  {
    const g2 = await generation(HEX("2"));
    let cur = g1;
    const r = rig({ handler: route(() => cur) });
    r.loader.start("1H", ["oi", "ls"]);
    await settle(r);
    const first = r.loader.state.generation;
    cur = g2;
    const before = r.calls.length;
    await r.fire("snapshot");
    await settle(r);
    assert.notStrictEqual(r.loader.state.generation, first);
    assert.strictEqual(r.loader.state.generation, g2.manifest.generation);
    assert.strictEqual(r.calls.slice(before).filter((c) => c.url.includes("artifact=")).length, 3, "core, oi and ls of the new generation");
    assert.strictEqual(r.loader.state.snapshotError, null);
    r.loader.stop();
  }
  // 5. integrity and identity failures never replace a good generation
  for (const [label, mutate, expected] of [
    ["tampered artifact body", (g) => { const doc = [...g.bodies.values()].find((d) => d.part === "oi"); doc.count = doc.count; doc.columns[0][1] = doc.columns[0][1]; doc.groups.push({ asof_ms: 1 }); }, "artifact_digest_mismatch"],
    ["missing artifact", (g) => { g.bodies.delete(g.manifest.parts.oi.logical_sha256); }, "http_404"],
    ["manifest identity", (g) => { g.manifest.parts.oi.generation = HEX("9"); }, "invalid_manifest_part"],
    ["foreign schema", (g) => { g.manifest.schema = "pulse.manifest.v2"; }, "unknown_manifest_schema"],
    ["horizon", (g) => { g.manifest.horizon = "4H"; }, "horizon_mismatch"],
  ]) {
    const bad = await generation(HEX("3"));
    mutate(bad);
    let cur = g1;
    const r = rig({ handler: route(() => cur) });
    r.loader.start("1H", ["oi"]);
    await settle(r);
    const good = r.loader.state.generation;
    cur = bad;
    await r.fire("snapshot");
    await settle(r);
    assert.strictEqual(r.loader.state.generation, good, label);
    assert.strictEqual(r.loader.state.snapshotError, expected, label);
    assert.strictEqual(r.loader.state.phase, "ready", label);
    r.loader.stop();
  }
  // 6. mixed-generation parts under a coherent manifest are refused
  {
    const g2 = await generation(HEX("4"));
    const g3 = await generation(HEX("5"));
    // manifest of g2 points core at g2 and oi at g3's body (digest matches its own body, generation differs)
    g2.manifest.parts.oi = { ...g3.manifest.parts.oi };
    for (const [h, b] of g3.bodies) g2.bodies.set(h, b);
    let cur = g2;
    const r = rig({ handler: route(() => cur) });
    r.loader.start("1H", ["oi"]);
    await settle(r);
    assert.strictEqual(r.loader.state.phase, "error");
    assert.match(String(r.loader.state.snapshotError), /invalid_manifest_part|mixed_generation|manifest_part_mismatch/);
    r.loader.stop();
  }
  // 7. a mapping whose universe does not hash to mapping_version is refused
  {
    const bad = await generation(HEX("6"));
    const core = [...bad.bodies.values()].find((d) => d.part === "core");
    // keep digest bookkeeping consistent with the edit, so only the mapping hash check can catch it
    const old = bad.manifest.parts.core.logical_sha256;
    core.header.universe[0].symbol = "TAMPERED";
    const h = await digest(core);
    bad.bodies.delete(old);
    bad.bodies.set(h, core);
    bad.manifest.parts.core.logical_sha256 = h;
    let cur = bad;
    const r = rig({ handler: route(() => cur) });
    r.loader.start("1H", ["oi"]);
    await settle(r);
    assert.strictEqual(r.loader.state.snapshotError, "mapping_hash_mismatch");
    r.loader.stop();
  }
  // 8. Review must-fix 1: a failed candidate's ETag is not promoted, so the missing artifact is retried and heals
  {
    const g2 = await generation(HEX("7"));
    const oiHash = g2.manifest.parts.oi.logical_sha256;
    const held = g2.bodies.get(oiHash);
    g2.bodies.delete(oiHash);
    let cur = g1;
    const r = rig({ handler: route(() => cur) });
    r.loader.start("1H", ["oi"]);
    await settle(r);
    const good = r.loader.state.generation;
    cur = g2;
    await r.fire("snapshot");
    await settle(r);
    assert.strictEqual(r.loader.state.generation, good);
    assert.strictEqual(r.loader.state.snapshotError, "http_404");
    g2.bodies.set(oiHash, held); // the artifact appears; the manifest is unchanged
    const calls = r.calls.length;
    await r.fire("snapshot");
    await settle(r);
    const manifestRequest = r.calls.slice(calls).find((c) => c.url.includes("horizon="));
    assert.notStrictEqual(manifestRequest.headers["If-None-Match"], '"' + g2.manifest.generation.slice(7, 15) + '"', "candidate ETag must not be sent");
    assert.strictEqual(r.loader.state.generation, g2.manifest.generation, "recovered without any other change");
    assert.strictEqual(r.loader.state.snapshotError, null);
    r.loader.stop();
  }
  // 9. Review must-fix 2: a delayed lazy fetch of an older generation cannot roll back a newer one
  {
    const g2 = await generation(HEX("8"));
    let cur = g1;
    let release;
    const gate = new Promise((res) => (release = res));
    const lsOld = g1.manifest.parts.ls.logical_sha256;
    const base = route(() => cur);
    const r = rig({ handler: (url, init) => {
      if (new URL(url, "http://x").searchParams.get("artifact") === lsOld) return gate.then(() => base(url, init));
      return base(url, init);
    } });
    r.loader.start("1H", ["oi"]);
    await settle(r);
    const lazy = r.loader.ensureParts(["ls"]); // G1's ls response is held
    await flush();
    cur = g2;
    await r.fire("snapshot"); // G2 core + ls (active) commit meanwhile
    await settle(r);
    assert.strictEqual(r.loader.state.generation, g2.manifest.generation);
    release();
    await lazy;
    await settle(r);
    assert.strictEqual(r.loader.state.generation, g2.manifest.generation, "late G1 part must not replace G2");
    assert.deepStrictEqual(r.loader.state.parts.loaded.slice().sort(), ["core", "ls"]);
    r.loader.stop();
  }
  // 10. active versus resident: parts no longer in view are not refreshed on the next generation
  {
    const g2 = await generation(HEX("a"));
    let cur = g1;
    const r = rig({ handler: route(() => cur) });
    r.loader.start("1H", ["oi"]);
    await settle(r);
    await r.loader.ensureParts(["ls"]); // the viewer moved from OI to L/S
    assert.deepStrictEqual(r.loader.state.parts.loaded.slice().sort(), ["core", "ls", "oi"], "visited tab stays resident for instant switching");
    cur = g2;
    const before = r.calls.length;
    await r.fire("snapshot");
    await settle(r);
    const fetched = r.calls.slice(before).filter((c) => c.url.includes("artifact="));
    assert.strictEqual(fetched.length, 2, "only core and the active L/S part are fetched: " + fetched.length);
    assert.deepStrictEqual(r.loader.state.parts.loaded.slice().sort(), ["core", "ls"]);
    // going back to OI fetches it once for the current generation
    await r.loader.ensureParts(["oi"]);
    assert.deepStrictEqual(r.loader.state.parts.loaded.slice().sort(), ["core", "ls", "oi"]);
    r.loader.stop();
  }
  // 11. live price overlay: lazy references of the same generation, price and returns move together, expiry reverts the frame
  {
    let cur = g1;
    const r = rig({ handler: route(() => cur) });
    r.set(CLOCK + 5000);
    r.loader.start("1H", ["oi"]);
    await settle(r);
    const baseline = r.loader.state.snapshot.rows[0].price;
    assert.ok(!r.loader.state.parts.loaded.includes("references"), "references are not part of first paint");
    const before = r.calls.filter((c) => c.url.includes("artifact=")).length;
    await r.loader.setPriceOverlay(true);
    await settle(r);
    assert.strictEqual(r.calls.filter((c) => c.url.includes("artifact=")).length, before + 1, "exactly one references fetch");
    assert.strictEqual(r.loader.state.price.status, "ok");
    assert.strictEqual(r.loader.state.snapshot.rows[0].price, 103.125);
    assert.ok(Math.abs(r.loader.state.snapshot.rows[0].changes["24H"] - 0.03125) < 1e-12);
    assert.notStrictEqual(baseline, 103.125);
    // numerator expiry: the next recompute returns the coherent snapshot frame (no stale live price kept)
    r.set(1800000064000 + 1);
    r.loader.tick();
    assert.notStrictEqual(r.loader.state.snapshot.rows[0].price, 103.125);
    // turning it off drops references from the active set and the price document
    await r.loader.setPriceOverlay(false);
    assert.strictEqual(r.loader.state.price.status, "off");
    assert.ok(!r.loader.state.parts.wanted.includes("references"));
    r.loader.stop();
  }
  // 12. overlay: foreign mapping fails closed; a new generation fetches its own matching references
  {
    let cur = g1;
    const foreign = JSON.parse(fs.readFileSync(path.join(__dirname, "../docs/contracts/fixtures/live_price.mapping_mismatch.json"), "utf8"));
    priceResponse = () => json(foreign);
    const r = rig({ handler: route(() => cur) });
    r.set(CLOCK + 5000);
    r.loader.start("1H", ["oi"]);
    await settle(r);
    await r.loader.setPriceOverlay(true);
    await settle(r);
    assert.strictEqual(r.loader.state.price.status, "error");
    assert.strictEqual(r.loader.state.price.reason, "live_mapping_or_schema_mismatch");
    priceResponse = () => json(priceDoc);
    const g2 = await generation(HEX("b"));
    cur = g2;
    const before = r.calls.length;
    await r.fire("snapshot");
    await settle(r);
    const fetched = r.calls.slice(before).filter((c) => c.url.includes("artifact="));
    assert.strictEqual(fetched.length, 3, "core, oi and the new generation's references: " + fetched.length);
    assert.ok(r.loader.state.parts.loaded.includes("references"));
    r.loader.stop();
  }
  console.log("Pulse wire loader: manifest/parts transport, atomic generation switch, integrity and identity failures passed");
})().catch((e) => { console.error(e); process.exit(1); });
