"use strict";
const assert = require("node:assert");
const S = require("../frontend/src/pulse/pulse_socket.js");

class FakeWS {
  static all = [];
  constructor(url) { this.url = url; this.sent = []; this.closed = false; FakeWS.all.push(this); }
  send(m) { this.sent.push(m); }
  close() { if (this.closed) return; this.closed = true; this.onclose && this.onclose({}); }
  // test helpers
  receive(m) { this.onmessage({ data: m }); }
  open() { this.receive('0{"sid":"x","upgrades":[],"pingInterval":25000,"pingTimeout":20000,"maxPayload":1000000}'); }
  connect() { this.receive('40{"sid":"y"}'); }
  tick(T, p) { this.receive('42["price-change",{"event":"price-change","data":' + JSON.stringify(JSON.stringify({ T, p, pr: "f" })) + "}]"); }
}
function rig(extra = {}) {
  FakeWS.all = [];
  let t = 1_800_000_000_000, id = 1;
  const timers = new Map();
  const ticks = [], statuses = [];
  const sock = S.create({
    WebSocketImpl: FakeWS, now: () => t, random: () => 0.5, ...extra,
    setTimer: (fn, ms) => { const k = id++; timers.set(k, { fn, at: t + ms }); return k; },
    clearTimer: (k) => timers.delete(k),
    onTick: (pair, tick) => ticks.push([pair, tick]), onStatus: (s) => statuses.push(s),
  });
  return { sock, ticks, statuses, timers, advance(ms) {
    t += ms;
    for (const [k, v] of Array.from(timers.entries()).sort((a, b) => a[1].at - b[1].at)) if (v.at <= t && timers.has(k)) { timers.delete(k); v.fn(); }
  }, now: () => t };
}

// 1. protocol: handshake, namespace connect, exact join frame, tick attribution by connection
{
  const r = rig();
  r.sock.setPairs(["B-BTC_USDT", "B-ETH_USDT"]);
  assert.strictEqual(FakeWS.all.length, 2);
  assert.ok(FakeWS.all.every((w) => w.url === "wss://stream.coindcx.com/socket.io/?EIO=4&transport=websocket"));
  const [btc, eth] = FakeWS.all;
  btc.open(); assert.deepStrictEqual(btc.sent, ["40"]);
  btc.connect(); assert.strictEqual(btc.sent[1], '42["join",{"channelName":"B-BTC_USDT@prices-futures"}]');
  eth.open(); eth.connect(); assert.strictEqual(eth.sent[1], '42["join",{"channelName":"B-ETH_USDT@prices-futures"}]');
  assert.strictEqual(r.sock.state.status, "live");
  btc.tick(1800000000100, "79902.2"); eth.tick(1800000000200, "2315.45");
  assert.deepStrictEqual(r.ticks, [["B-BTC_USDT", { price: "79902.2", source_ms: 1800000000100 }], ["B-ETH_USDT", { price: "2315.45", source_ms: 1800000000200 }]]);
  btc.receive("2"); assert.strictEqual(btc.sent.at(-1), "3", "ping is answered with pong");
}
// 2. hostile or malformed frames are ignored, never thrown
{
  const r = rig(); r.sock.setPairs(["B-BTC_USDT"]);
  const ws = FakeWS.all[0]; ws.open(); ws.connect();
  const bad = ['42not json', '42["price-change"]', '42["other",{"data":"{}"}]', '42["price-change",{"data":"not json"}]',
    '42["price-change",{"data":"{\\"T\\":-1,\\"p\\":\\"1\\"}"}]', '42["price-change",{"data":"{\\"T\\":1.5,\\"p\\":\\"1\\"}"}]',
    '42["price-change",{"data":"{\\"T\\":1,\\"p\\":\\"1e5\\"}"}]', '42["price-change",{"data":"{\\"T\\":1,\\"p\\":\\"-\\"}"}]',
    '42["price-change",{"data":"{\\"T\\":1,\\"p\\":1}"}]', "", "9garbage", "4"];
  for (const m of bad) ws.receive(m);
  ws.onmessage({ data: 123 }); ws.onmessage({});
  assert.strictEqual(r.ticks.length, 0);
  ws.receive("x".repeat(5000)); assert.ok(ws.closed, "oversized frame closes the connection");
}
// 3. pair names are validated; a bad name never opens a socket
{
  const r = rig(); r.sock.setPairs(["B-BTC_USDT", "btc", "B-BTC_USDT@x", "B-A B_USDT", "", 5, null, "B-BTC_USDT"]);
  assert.strictEqual(FakeWS.all.length, 1);
  assert.strictEqual(S.PAIR.test("B-1000PEPE_USDT"), true);
}
// 4. subscription diff: removed pairs linger, then close; re-adding cancels the close; cap at maxSockets
{
  const r = rig({ maxSockets: 3, lingerMs: 4000 });
  r.sock.setPairs(["B-A_USDT", "B-B_USDT", "B-C_USDT", "B-D_USDT"]);
  assert.strictEqual(FakeWS.all.length, 3, "capped");
  r.sock.setPairs(["B-B_USDT", "B-C_USDT"]);
  assert.strictEqual(FakeWS.all[0].closed, false);
  r.advance(3000);
  r.sock.setPairs(["B-A_USDT", "B-B_USDT", "B-C_USDT"]); // A is back before its linger ends
  r.advance(2000);
  assert.strictEqual(FakeWS.all[0].closed, false, "re-adding cancelled the close");
  r.sock.setPairs(["B-B_USDT", "B-C_USDT"]);
  r.advance(4001);
  assert.strictEqual(FakeWS.all[0].closed, true);
  assert.strictEqual(FakeWS.all.length, 3, "no reconnect for a dropped pair");
}
// 5. reconnect with exponential backoff after a joined connection drops; counters reset on a good join
{
  const r = rig({ backoffBaseMs: 500 });
  r.sock.setPairs(["B-BTC_USDT"]);
  const a = FakeWS.all[0]; a.open(); a.connect();
  a.close();
  assert.strictEqual(FakeWS.all.length, 1);
  r.advance(400); assert.strictEqual(FakeWS.all.length, 1);
  r.advance(200); assert.strictEqual(FakeWS.all.length, 2, "first retry after about 500 ms");
  const b = FakeWS.all[1]; b.open(); b.connect(); b.tick(1800000000001, "1");
  assert.strictEqual(r.ticks.length, 1);
}
// 6. refused before joining (origin policy): blocked after 3 failures, quiet for 60 s, then one more attempt
{
  const r = rig({ backoffBaseMs: 100 });
  r.sock.setPairs(["B-BTC_USDT"]);
  for (let i = 0; i < 3; i++) { FakeWS.all.at(-1).close(); r.advance(3000); }
  assert.strictEqual(r.sock.state.status, "blocked");
  const attempts = FakeWS.all.length;
  r.advance(30000);
  assert.strictEqual(FakeWS.all.length, attempts, "no hammering while blocked");
  r.advance(40000);
  assert.ok(FakeWS.all.length > attempts, "tries again after the quiet period");
  const w = FakeWS.all.at(-1); w.open(); w.connect();
  assert.strictEqual(r.sock.state.status, "live", "recovers when the origin is allowed later");
}
// 7. silence watchdog replaces a connection that stops talking
{
  const r = rig({ silenceMs: 45000 });
  r.sock.setPairs(["B-BTC_USDT"]);
  const a = FakeWS.all[0]; a.open(); a.connect();
  r.advance(44000); assert.strictEqual(a.closed, false);
  a.receive("2"); r.advance(44000); assert.strictEqual(a.closed, false, "any packet re-arms the watchdog");
  r.advance(2000); assert.strictEqual(a.closed, true);
}
// 8. stop closes everything and nothing comes back; insecure URL refused
{
  const r = rig();
  r.sock.setPairs(["B-BTC_USDT", "B-ETH_USDT"]);
  r.sock.stop();
  assert.ok(FakeWS.all.every((w) => w.closed));
  const n = FakeWS.all.length; r.advance(120000); r.sock.setPairs(["B-BTC_USDT"]);
  assert.strictEqual(FakeWS.all.length, n);
  assert.strictEqual(r.sock.state.status, "idle");
  assert.throws(() => S.create({ WebSocketImpl: FakeWS, now: Date.now, setTimer: () => 1, clearTimer: () => {}, onTick: () => {}, url: "ws://stream.coindcx.com/" }), /insecure_socket_url/);
  assert.throws(() => S.create({}), /missing_WebSocketImpl/);
}
console.log("Pulse socket: protocol, attribution by connection, hostile frames, diff and linger, backoff, blocked origin, watchdog, stop passed");
