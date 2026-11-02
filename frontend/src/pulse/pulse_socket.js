/* Direct tick source: one CoinDCX per-pair price socket per visible pair.
   Messages on a CoinDCX socket carry NO pair identifier (only {T,p,pr}), so a message can be attributed to a pair
   only by the connection it arrived on: one connection per pair, never several channels on one socket.
   Minimal Socket.IO v4 / Engine.IO v4 client (no dependency). Browsers send an Origin header the server checks
   (coindcx.com only), so from any other origin the upgrade is refused: that is detected and reported as `blocked`,
   after which the page keeps using the polled price document. Pure of globals: WebSocket, timers and clock injected. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PulseSocket = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const URL_DEFAULT = "wss://stream.coindcx.com/socket.io/?EIO=4&transport=websocket";
  const PAIR = /^B-[A-Z0-9]{1,24}_USDT$/;
  const PRICE = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
  const MAX_FRAME = 4096;
  const DEFAULTS = {
    maxSockets: 16,
    lingerMs: 4000,
    backoffBaseMs: 500,
    backoffMaxMs: 30000,
    blockedAfter: 3,       // consecutive pre-join failures that mean "refused", not "flaky"
    blockedForMs: 60000,
    silenceMs: 45000,      // no packet at all for pingInterval + pingTimeout
  };

  function create(options) {
    const o = { ...DEFAULTS, url: URL_DEFAULT, ...options };
    for (const k of ["WebSocketImpl", "now", "setTimer", "clearTimer", "onTick"]) if (typeof o[k] !== "function") throw new TypeError("missing_" + k);
    if (typeof o.url !== "string" || !o.url.startsWith("wss://")) throw new TypeError("insecure_socket_url");
    const random = o.random || Math.random;
    const state = { status: "idle", connections: 0, joined: 0, ticks: 0, last_tick_ms: null, refusals: 0 };
    const conns = new Map(); // pair -> connection
    let wanted = [];
    let consecutiveFailures = 0;
    let blockedUntil = 0;
    let stopped = false;
    const setStatus = (s) => {
      if (state.status !== s) {
        state.status = s;
        if (typeof o.onStatus === "function") o.onStatus(s, state);
      }
    };
    const refresh = () => {
      state.connections = conns.size;
      state.joined = Array.from(conns.values()).filter((c) => c.joined).length;
      if (stopped || !wanted.length) return setStatus("idle");
      if (o.now() < blockedUntil) return setStatus("blocked");
      setStatus(state.joined > 0 ? "live" : "connecting");
    };
    const backoff = (n) => Math.min(o.backoffMaxMs, Math.round(o.backoffBaseMs * 2 ** Math.min(n, 10) * (0.85 + 0.3 * random())));

    function parseTick(text) {
      // 42["price-change",{"event":"price-change","data":"{\"T\":..,\"p\":\"..\",\"pr\":\"f\"}"}]
      let packet;
      try {
        packet = JSON.parse(text);
      } catch (e) {
        return null;
      }
      if (!Array.isArray(packet) || packet[0] !== "price-change" || !packet[1] || typeof packet[1].data !== "string") return null;
      if (packet[1].data.length > 512) return null;
      let data;
      try {
        data = JSON.parse(packet[1].data);
      } catch (e) {
        return null;
      }
      if (!data || !Number.isSafeInteger(data.T) || data.T < 0 || typeof data.p !== "string" || data.p.length > 40 || !PRICE.test(data.p)) return null;
      return { price: data.p, source_ms: data.T };
    }

    function connect(c) {
      if (stopped || c.closed) return;
      let ws;
      try {
        ws = new o.WebSocketImpl(o.url);
      } catch (e) {
        return failed(c);
      }
      c.ws = ws;
      c.joined = false;
      c.handshake = false;
      arm(c);
      ws.onmessage = (event) => {
        if (c.ws !== ws || c.closed) return;
        const m = event && event.data;
        if (typeof m !== "string" || m.length === 0 || m.length > MAX_FRAME) {
          if (typeof m === "string" && m.length > MAX_FRAME) ws.close();
          return;
        }
        arm(c);
        const type = m[0];
        if (type === "0") {
          c.handshake = true;
          ws.send("40"); // connect to the default namespace
        } else if (type === "2") ws.send("3");
        else if (type === "1") ws.close();
        else if (type === "4") {
          if (m[1] === "0" && !c.joined) {
            ws.send('42["join",{"channelName":"' + c.pair + '@prices-futures"}]');
            c.joined = true;
            c.attempts = 0;
            consecutiveFailures = 0;
            refresh();
          } else if (m[1] === "2") {
            const tick = parseTick(m.slice(2));
            if (tick) {
              state.ticks += 1;
              state.last_tick_ms = o.now();
              o.onTick(c.pair, tick);
            }
          }
        }
      };
      ws.onclose = () => {
        if (c.ws !== ws) return;
        c.ws = null;
        disarm(c);
        if (c.closed || stopped) return;
        failed(c);
      };
      ws.onerror = () => {}; // the close event follows; nothing to add
      refresh();
    }

    function failed(c) {
      const wasJoined = c.joined;
      c.joined = false;
      if (!wasJoined) {
        consecutiveFailures += 1;
        state.refusals += 1;
        if (consecutiveFailures >= o.blockedAfter) {
          // Refused before ever joining, repeatedly: an origin policy or an outage, not a blip. Stop hammering it.
          blockedUntil = o.now() + o.blockedForMs;
        }
      }
      c.attempts = (c.attempts || 0) + 1;
      const wait = o.now() < blockedUntil ? Math.max(o.blockedForMs, backoff(c.attempts - 1)) : backoff(c.attempts - 1);
      c.retry = o.setTimer(() => {
        c.retry = null;
        if (!c.closed && !stopped) connect(c);
      }, wait);
      refresh();
    }

    function arm(c) {
      disarm(c);
      c.silence = o.setTimer(() => {
        c.silence = null;
        if (c.ws) c.ws.close(); // a connection that stops talking is replaced
      }, o.silenceMs);
    }
    function disarm(c) {
      if (c.silence != null) o.clearTimer(c.silence);
      c.silence = null;
    }
    function drop(c) {
      c.closed = true;
      disarm(c);
      if (c.retry != null) o.clearTimer(c.retry);
      if (c.linger != null) o.clearTimer(c.linger);
      c.retry = c.linger = null;
      if (c.ws) {
        const ws = c.ws;
        c.ws = null;
        try {
          ws.close();
        } catch (e) {}
      }
      conns.delete(c.pair);
    }

    return {
      state,
      /* The pairs that should stream now (visible rows plus a small buffer), most important first. */
      setPairs(pairs) {
        if (stopped) return;
        wanted = Array.from(new Set(pairs.filter((p) => typeof p === "string" && PAIR.test(p)))).slice(0, o.maxSockets);
        for (const p of wanted) {
          let c = conns.get(p);
          if (!c) {
            c = { pair: p, ws: null, joined: false, closed: false, attempts: 0, retry: null, silence: null, linger: null };
            conns.set(p, c);
            if (o.now() >= blockedUntil) connect(c);
            else c.retry = o.setTimer(() => { c.retry = null; connect(c); }, Math.max(0, blockedUntil - o.now()));
          } else if (c.linger != null) {
            o.clearTimer(c.linger);
            c.linger = null;
          }
        }
        for (const c of Array.from(conns.values())) {
          if (!wanted.includes(c.pair) && c.linger == null) c.linger = o.setTimer(() => drop(c) || refresh(), o.lingerMs);
        }
        refresh();
      },
      stop() {
        stopped = true;
        for (const c of Array.from(conns.values())) drop(c);
        wanted = [];
        refresh();
      },
      blockedUntil: () => blockedUntil,
    };
  }

  return { create, PAIR, URL_DEFAULT };
});
