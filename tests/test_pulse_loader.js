"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const Loader = require("../frontend/src/pulse/pulse_loader.js");
const A = require("../frontend/src/pulse/pulse_adapter.js");
const load = (n) => JSON.parse(fs.readFileSync(path.join(__dirname, "../docs/contracts/fixtures", n), "utf8"));
const flush = () => new Promise((r) => setImmediate(r));

function rig(routes, opts = {}) {
  let t = routes.start;
  const timers = new Map();
  let nextId = 1;
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, headers: init.headers, credentials: init.credentials });
    const route = routes.handler(url, init, calls.length);
    if (route instanceof Error) throw route;
    const headers = new Map(Object.entries(route.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
    return { status: route.status, headers: { get: (k) => headers.get(k.toLowerCase()) ?? null }, text: async () => route.body ?? "" };
  };
  const loader = Loader.create({
    fetch: fetchFn, now: () => t, random: () => 0.5, snapshotMs: 30000, liveMs: 5000,
    setTimer: (fn, ms, kind) => { const id = nextId++; timers.set(id, { fn, ms, kind }); return id; },
    clearTimer: (id) => timers.delete(id),
    snapshotUrl: (h) => `/api/pulse?horizon=${h}`, liveUrl: () => "/api/pulse?live=1",
    ...opts,
  });
  return {
    loader, calls, timers,
    set: (v) => (t = v),
    advance: (ms) => (t += ms),
    async fire(kind) { // fire the earliest timer
      const found = [...timers.entries()].find(([, t2]) => t2.kind === kind);
      if (!found) return;
      const [id, tm] = found;
      timers.delete(id);
      tm.fn();
      await flush(); await flush();
    },
  };
}

const wire = load("snapshot.valid.1H.json");
const T = wire.generated_ms + 1000;
const fc = wire.rows[0].funding_change;
const liveDoc = (o = {}) => JSON.stringify({ schema: A.LIVE_SCHEMA, mapping_version: wire.mapping_version, sequence: 1, generated_ms: T, expires_ms: T + 60000,
  rows: [{ id: 0, status: "available", reason: null, h: fc.h, nft: fc.nft, fr: fc.settled_rate, efr: "0.00012", source_ms: T - 2000, expires_ms: fc.nft }], ...o });
const ok = (body, etag) => ({ status: 200, body: typeof body === "string" ? body : JSON.stringify(body), headers: etag ? { etag } : {} });

(async () => {
  // Fast volume is merged even when a price-only paint sink handles the tick.
  {
    const doc={schema:A.PRICE_SCHEMA,mapping_version:wire.mapping_version,generated_ms:T,expires_ms:T+15000,
      rows:[{id:0,status:"unavailable",price:null,volume:{status:"available",reason:null,value:"98765",unit:"USDT",source_ms:T,expires_ms:T+60000}}]};
    let sinks=0;
    const r=rig({start:T,handler:url=>url.includes("horizon")?ok(wire):url.includes("live=price")?ok(doc):ok(liveDoc())},
      {livePriceUrl:()=>"/api/pulse?live=price",priceSink:()=>{sinks++;return 0;}});
    r.loader.start("1H");await flush();await flush();await flush();
    await r.loader.setPriceOverlay(true);await flush();await flush();
    assert.equal(r.loader.state.snapshot.rows[0].volume,98765);
    assert.ok(sinks>0);
    r.advance(16000);r.loader.tick();
    assert.equal(r.loader.state.snapshot.rows[0].volume,98765,"accepted volume survives expired 15s envelope");
    r.set(T+60000);r.loader.tick();
    assert.equal(r.loader.state.snapshot.rows[0].volume,null,"old v1 expiry is not retroactively extended");
    r.loader.stop();
  }
  {
    const doc={schema:A.PRICE_SCHEMA,mapping_version:wire.mapping_version,generated_ms:T,expires_ms:T+15000,
      rows:[{id:0,status:"unavailable",volume:{status:"available",reason:null,value:"0",unit:"USDT",source_ms:T,expires_ms:T+300000,definition_version:"volume.rt.v2"}}]};
    const r=rig({start:T,handler:url=>url.includes("horizon")?ok(wire):url.includes("live=price")?ok(doc):ok(liveDoc())},
      {livePriceUrl:()=>"/api/pulse?live=price",priceSink:()=>0});
    r.loader.start("1H");await flush();await flush();await r.loader.setPriceOverlay(true);await flush();await flush();
    for(const age of [16000,60000,299999]){r.set(T+age);r.loader.tick();assert.equal(r.loader.state.snapshot.rows[0].volume,0);}
    r.set(T+300000);r.loader.tick();assert.equal(r.loader.state.snapshot.rows[0].volume,null);
    r.loader.stop();
  }
  // 1. happy path, live merge, ETag revalidation, 304 never extends expiry
  {
    const r = rig({ start: T, handler: (url, init) => {
      if (url.includes("horizon")) return init.headers["If-None-Match"] === '"g1"' ? { status: 304 } : ok(wire, '"g1"');
      return ok(liveDoc());
    } });
    r.loader.start("1H");
    await flush(); await flush(); await flush();
    assert.strictEqual(r.loader.state.phase, "ready");
    assert.strictEqual(r.loader.state.snapshot.rows.length, 1);
    assert.strictEqual(r.loader.state.live.status, "ok");
    assert.strictEqual(r.loader.state.snapshot.rows[0].metrics.funding["1H"].live, true);
    assert.notStrictEqual(r.loader.state.snapshot.rows[0].metrics.funding["24H"].live, true, "the live estimate is the Current window only");
    assert.strictEqual(r.calls.length, 2);
    assert.ok(r.calls.every((c) => c.credentials === "same-origin"), "Preview protection cookies reach same-origin reads only");
    // snapshot poll sends If-None-Match and a 304 keeps data but not freshness
    await r.fire("snapshot");
    assert.strictEqual(r.calls.find((c, i) => i >= 2 && c.url.includes("horizon")).headers["If-None-Match"], '"g1"');
    assert.strictEqual(r.loader.state.phase, "ready");
    r.set(wire.expires_ms + 1);
    r.loader.tick();
    assert.strictEqual(r.loader.state.phase, "stale", "expiry judged from the document, not the poll");
    assert.strictEqual(r.calls.every((c) => c.headers.Accept === "application/json"), true);
    r.loader.stop();
  }
  // 2. live document with a foreign mapping fails closed
  {
    const r = rig({ start: T, handler: (url) => url.includes("horizon") ? ok(wire) : ok(liveDoc({ mapping_version: "sha256:" + "1".repeat(64) })) });
    r.loader.start("1H");
    await flush(); await flush(); await flush();
    assert.strictEqual(r.loader.state.live.status, "error");
    assert.strictEqual(r.loader.state.live.reason, "live_mapping_or_schema_mismatch");
    assert.strictEqual(r.loader.state.snapshot.rows[0].metrics.funding["24H"].live, undefined);
    assert.strictEqual(r.loader.state.phase, "ready");
    r.loader.stop();
  }
  // 3. error before any snapshot; backoff grows; Retry-After wins
  {
    let n = 0;
    const r = rig({ start: T, handler: () => (++n === 1 ? { status: 503, headers: { "Retry-After": "40" } } : { status: 500 }) });
    r.loader.start("1H");
    await flush(); await flush();
    assert.strictEqual(r.loader.state.phase, "error");
    assert.strictEqual(r.loader.state.snapshotError, "http_503");
    const first = [...r.timers.values()].find((x) => x.kind === "snapshot").ms;
    assert.ok(first >= 40000, "Retry-After honoured: " + first);
    await r.fire("snapshot");
    const second = [...r.timers.values()].find((x) => x.kind === "snapshot").ms;
    assert.ok(second > 0 && second <= 60000);
    r.loader.stop();
  }
  // 4. invalid generation never replaces a good one
  {
    let served = 0;
    const r = rig({ start: T, handler: (url) => {
      if (!url.includes("horizon")) return ok(liveDoc());
      return ++served === 1 ? ok(wire) : ok({ ...wire, schema: "pulse.snapshot.v2" });
    } });
    r.loader.start("1H");
    await flush(); await flush(); await flush();
    const good = r.loader.state.generation;
    await r.fire("snapshot");
    assert.strictEqual(r.loader.state.generation, good);
    assert.strictEqual(r.loader.state.snapshotError, "unknown_schema");
    assert.strictEqual(r.loader.state.phase, "ready");
    r.loader.stop();
  }
  // 5. mapping change drops the live document and flags it
  {
    let served = 0;
    const w2 = JSON.parse(JSON.stringify(wire));
    w2.mapping_version = "sha256:" + "2".repeat(64);
    w2.generation = "sha256:" + "3".repeat(64);
    const r = rig({ start: T, handler: (url) => url.includes("horizon") ? ok(++served === 1 ? wire : w2) : ok(liveDoc()) });
    r.loader.start("1H");
    await flush(); await flush(); await flush();
    assert.strictEqual(r.loader.state.live.status, "ok");
    await r.fire("snapshot");
    assert.strictEqual(r.loader.state.mapping_version, w2.mapping_version);
    assert.deepStrictEqual(r.loader.state.notices, ["mapping_changed"]);
    assert.strictEqual(r.loader.state.live.status, "idle");
    r.loader.stop();
  }
  // 6. a late response from a previous horizon is discarded
  {
    let release;
    const gate = new Promise((res) => (release = res));
    const r = rig({ start: T, handler: () => ok(wire) });
    const original = r.loader.state;
    const slow = rig({ start: T, handler: (u) => ok(wire) });
    // custom: delay first response
    let first = true;
    const timers = new Map();
    const loader = Loader.create({
      fetch: async (url) => { if (first) { first = false; await gate; } return { status: 200, headers: { get: () => null }, text: async () => JSON.stringify(wire) }; },
      now: () => T, random: () => 0.5, setTimer: (fn, ms) => 1, clearTimer: () => {},
      snapshotUrl: (h) => `/s?h=${h}`, liveUrl: () => "/l",
    });
    loader.start("1H");
    loader.setHorizon("4H");
    release();
    await flush(); await flush(); await flush();
    assert.strictEqual(loader.state.horizon, "4H");
    assert.notStrictEqual(loader.state.generation, wire.generation, "late 1H response must not be applied to 4H");
    loader.stop(); r.loader.stop(); slow.loader.stop();
  }
  // 7. single flight and visibility
  {
    let fetches = 0, hidden = false;
    const r = rig({ start: T, handler: () => { fetches++; return ok(wire); } }, { isVisible: () => !hidden });
    r.loader.start("1H");
    r.loader.wake(); r.loader.wake();
    await flush(); await flush(); await flush();
    const before = fetches;
    hidden = true;
    await r.fire("snapshot");
    assert.strictEqual(fetches, before, "no network while hidden");
    r.loader.stop();
  }
  // 8. repeated failures turn a good snapshot stale, then recovery clears it
  {
    let mode = "ok";
    const r = rig({ start: T, handler: (url) => url.includes("horizon") ? (mode === "ok" ? ok(wire) : new Error("offline")) : ok(liveDoc()) });
    r.loader.start("1H");
    await flush(); await flush(); await flush();
    mode = "down";
    for (let i = 0; i < 3; i++) { await r.fire("snapshot"); }
    assert.strictEqual(r.loader.state.phase, "stale");
    assert.strictEqual(r.loader.state.snapshotError, "network");
    mode = "ok";
    await r.fire("snapshot");
    assert.strictEqual(r.loader.state.phase, "ready");
    assert.strictEqual(r.loader.state.snapshotError, null);
    r.loader.stop();
  }
  // 9. oversized and invalid bodies are rejected
  {
    const r = rig({ start: T, handler: () => ok("{not json") });
    r.loader.start("1H");
    await flush(); await flush();
    assert.strictEqual(r.loader.state.snapshotError, "invalid_json");
    assert.strictEqual(r.loader.state.phase, "error");
    r.loader.stop();
    const r2 = rig({ start: T, handler: () => ok("x".repeat(100)) }, { maxBodyChars: 50 });
    r2.loader.start("1H");
    await flush(); await flush();
    assert.strictEqual(r2.loader.state.snapshotError, "response_too_large");
    r2.loader.stop();
  }
  assert.throws(() => Loader.create({}), /missing_fetch/);
  console.log("Pulse loader: ETag, single flight, backoff, generation/mapping switch, stale and failure paths passed");
})().catch((e) => { console.error(e); process.exit(1); });
