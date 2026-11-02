/* ADR-0100. Preview-only fixed-source collector and passive fanout. No import-time I/O. */
"use strict";
const { createHash, randomUUID, timingSafeEqual } = require("node:crypto");
const { WebSocket, WebSocketServer } = require("ws");
const { createClient } = require("redis");

const MAX_FRAME = 16384, BATCH_MS = 100, MAX_CLIENTS = 256;
const PAIR = /^B-[A-Z0-9]{1,24}_USDT$/;
const PRICE = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const UPSTREAM = "wss://stream.coindcx.com/socket.io/?EIO=4&transport=websocket";
const FENCED_PUBLISH = `
if string.sub(KEYS[1],1,10)~='pulse:stg:' or string.sub(KEYS[2],1,10)~='pulse:stg:' then return -1 end
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('PTTL',KEYS[1])<=0 then return -1 end
if #ARGV[2]>16384 then return -1 end
redis.call('PEXPIRE',KEYS[1],5000)
if #ARGV[2]==0 then return 0 end
local n=redis.call('PUBSUB','NUMSUB',KEYS[2])
if tonumber(n[2])>4 then return -2 end
return redis.call('PUBLISH',KEYS[2],ARGV[2])`;
const READ_REGISTRY = `
if string.sub(KEYS[1],1,10)~='pulse:stg:' or redis.call('HSTRLEN',KEYS[1],'payload')>131072 then return {} end
return redis.call('HMGET',KEYS[1],'payload','digest','source','expiry')`;
// ADR-0106: expire only our completed lease; never delete data or shorten a successor's lease.
const RELEASE = `
if string.sub(KEYS[1],1,10)~='pulse:stg:' then return 0 end
if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end
return redis.call('PEXPIRE',KEYS[1],1)`;

function canonical(x) {
  if (Array.isArray(x)) return "[" + x.map(canonical).join(",") + "]";
  if (x && typeof x === "object") return "{" + Object.keys(x).sort().map(k => JSON.stringify(k) + ":" + canonical(x[k])).join(",") + "}";
  return JSON.stringify(x);
}
const sha = x => createHash("sha256").update(x).digest("hex");
function configuration(env) {
  const production=env.VERCEL_ENV==='production'&&env.PULSE_PRODUCTION_ENABLED==='1'
    &&env.PULSE_UNIVERSE_MODE==='all'&&env.PULSE_RELEASE_TOP350==='1';
  if ((env.VERCEL_ENV !== "preview"&&!production) || env.PULSE_STREAM_ENABLED !== "1") throw Error("stream_disabled");
  const run = env.PULSE_RUNTIME_RUN_ID, mapping = env.PULSE_MAPPING_VERSION;
  if (!/^[a-f0-9]{32}$/.test(run || "") || !/^sha256:[a-f0-9]{64}$/.test(mapping || "")) throw Error("stream_scope");
  const url = new URL(env.PULSE_REDIS_URL);
  const plaintext=env[production?'PULSE_PRODUCTION_PLAINTEXT_APPROVED':'PULSE_STAGING_PLAINTEXT_APPROVED']==='1';
  if (!(url.protocol === "rediss:" || url.protocol === "redis:" && plaintext) ||
      !url.hostname || !url.password || !url.port || url.search || url.hash || !["", "/", "/0"].includes(url.pathname)) throw Error("stream_redis_scope");
  const scope=production?'pulse:prod:':'pulse:stg:';
  const prefix = `${scope}{${run}}:stream:`;
  // ADR-0192: match Python's approved ceiling and preserve explicit lower limits.
  const rawStop = env.PULSE_MEMORY_STOP_BYTES;
  const stopBytes = rawStop === undefined ? 120000000 : Number(rawStop);
  if (rawStop !== undefined && (typeof rawStop !== 'string' || rawStop.length === 0 || /[^0-9]/.test(rawStop) ||
      !Number.isSafeInteger(stopBytes) || stopBytes < 40000000 || stopBytes > 120000000))
    throw Error('invalid_memory_stop_bytes');
  return { url: url.href, mapping, production, lease: prefix + "lease", bus: prefix + "ticks:v2",
    maxPairs: env.PULSE_RELEASE_TOP350 === "1" ? 350 : 50,
    bytesPerSecond: env.PULSE_RELEASE_TOP350 === "1" ? 524288 : 12288,
    registry: `${scope}{${run}}:runtime:source:${env.PULSE_RELEASE_TOP350 === "1" ? "release350" : "registry"}:data`,
    stopBytes };
}
function scopedScript(script,c){
  if(!c.production)return script;
  // ADR-0190: translate only the fixed Lua prefix guards, never payload bytes.
  const result=script.replace(/string\.sub\((KEYS\[\d+\]),1,10\)~='pulse:stg:'/g,
    (_,key)=>`string.sub(${key},1,11)~='pulse:prod:'`);
  if(result.includes('pulse:stg:'))throw Error('stream_script_scope');
  return result;
}
function authorised(actual, secret) {
  if (typeof secret !== "string" || !/^[\x21-\x7e]{32,256}$/.test(secret) || typeof actual !== "string") return false;
  const expected = Buffer.from("Bearer " + secret), value = Buffer.from(actual);
  return expected.length === value.length && timingSafeEqual(expected, value);
}
function registry(fields, mapping, now, expectedPairs = 50) {
  if (![50,350].includes(expectedPairs)) throw Error("registry_mapping");
  if (!Array.isArray(fields) || fields.length !== 4 || fields.some(x => x == null)) throw Error("registry_missing");
  const [payload, digest, source, expiry] = fields;
  if (Buffer.byteLength(payload) > 131072 || sha(payload) !== digest || !Number.isSafeInteger(+source) ||
      !Number.isSafeInteger(+expiry) || +source > now + 5000 || +source < 0 || +expiry <= now) throw Error("registry_expired_or_corrupt");
  const v = JSON.parse(payload);
  if (expectedPairs===350 && v.policy!=="top350.quote_24h_usdt.v1") throw Error("registry_mapping");
  if (v.mapping_version !== mapping || "sha256:" + sha(canonical(v.universe)) !== mapping ||
      !Array.isArray(v.universe) || v.universe.length !== expectedPairs ||
      v.universe.some((r,i) => r.id !== i || !PAIR.test(r.pair_id) || r.symbol !== r.pair_id.slice(2).replace("_", "")) ||
      new Set(v.universe.map(r => r.pair_id)).size !== expectedPairs) throw Error("registry_mapping");
  return v.universe.map(r => r.pair_id);
}
function parseTick(raw, now) {
  if (typeof raw !== "string" || raw.length > 4096 || !raw.startsWith("42")) return null;
  try {
    const a = JSON.parse(raw.slice(2));
    if (!Array.isArray(a) || a[0] !== "price-change" || typeof a[1]?.data !== "string" || a[1].data.length > 512) return null;
    const t = JSON.parse(a[1].data);
    if (!Number.isSafeInteger(t.T) || t.T > now + 1000 || t.T < now - 60000 ||
        typeof t.p !== "string" || t.p.length > 40 || !PRICE.test(t.p) || !Number.isFinite(+t.p) || +t.p <= 0) return null;
    return { source_ms: t.T, price: t.p };
  } catch { return null; }
}
function batchValid(v, mapping, now) {
  return v && v.schema === "pulse.ticks.v1" && v.mapping_version === mapping &&
    Number.isSafeInteger(v.sent_ms) && v.sent_ms <= now + 1000 && v.sent_ms > now - 2000 &&
    Array.isArray(v.rows) && v.rows.length <= 50 && v.rows.every(r =>
      Array.isArray(r) && r.length === 3 && PAIR.test(r[0]) &&
      parseTick('42["price-change",{"data":' + JSON.stringify(JSON.stringify({ T:r[1], p:r[2] })) + '}]', now));
}

function pendingRows(latest) {
  const rows=[];
  for (const [pair,tick] of latest) {
    rows.push([pair,tick.source_ms,tick.price]);
    if (rows.length===50) break;
  }
  return rows;
}

function transientRetryDelay(status,headers,at,attempt,random=Math.random) {
  // ADR-0189: transient origin errors are not authorisation/rate-limit refusals.
  if (![500,502,503,504].includes(status)) return null;
  let provider=0;
  const raw=headers?.["retry-after"];
  if(raw!==undefined){
    if(typeof raw!=="string"||raw.length>64)return null;
    if(/^\d{1,10}$/.test(raw))provider=Number(raw)*1000;
    else {
      const datePattern=/^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$/;
      if(!datePattern.test(raw)||!Number.isFinite(Date.parse(raw)))return null;
      const date=headers?.date;
      const clock=typeof date==="string"&&datePattern.test(date)&&Number.isFinite(Date.parse(date))
        ?Math.min(at,Date.parse(date)):at;
      provider=Math.max(0,Date.parse(raw)-clock+1000);
    }
  }
  const delay=Math.max(provider,30000*2**Math.min(attempt,1))+Math.floor(random()*5000);
  return Number.isSafeInteger(delay)&&delay>=30000&&delay<=2147483647?delay:null;
}

function createBridge({ env = process.env, redisFactory = createClient, Socket = WebSocket, now = Date.now } = {}) {
  let clientPromise = null, subscriberPromise = null, collector = false, idleTimer = null, closed = false;
  const clients = new Set();
  const stats = { ticks: 0, batches: 0, published_bytes: 0, dropped: 0, errors: 0, upstream: 0, transient_refusals: 0 };
  function connection() {
    if (!clientPromise) {
      const c = configuration(env);
      clientPromise = (async () => {
        const r = redisFactory({ url:c.url, disableOfflineQueue:true, commandOptions:{timeout:2000},
          socket:{connectTimeout:2000, reconnectStrategy:false}, commandsQueueMaxLength:64 });
        r.on("error", () => { stats.errors++; });
        try { await r.connect(); return r; } catch { r.destroy(); clientPromise = null; throw Error("stream_redis_unavailable"); }
      })();
    }
    return clientPromise;
  }
  async function attach(ws) {
    const c = configuration(env);
    if (closed) return ws.close(1013,"stream_unavailable");
    clearTimeout(idleTimer);idleTimer=null;
    if (clients.size >= MAX_CLIENTS) return ws.close(1013, "capacity");
    clients.add(ws);
    const detached=()=>{
      clients.delete(ws);
      if(clients.size||closed||idleTimer||!subscriberPromise)return;
      const pending=subscriberPromise;
      // ADR-0162: an idle Preview instance must release its broker subscriber.
      idleTimer=setTimeout(async()=>{
        idleTimer=null;
        if(clients.size||subscriberPromise!==pending)return;
        subscriberPromise=null;
        (await pending)?.destroy();
      },1000);
      idleTimer.unref();
    };
    ws.on("close", detached);
    ws.on("error", detached);
    ws.on("message", () => ws.close(1008, "receive_only"));
    if (!subscriberPromise) {
      let pending;
      pending = (async () => {
        const r = (await connection()).duplicate();
        r.on("error", () => { stats.errors++; });
        r.on("end", () => {
          if(subscriberPromise!==pending)return;
          subscriberPromise = null;
          for (const peer of clients) peer.close(1013,"stream_unavailable");
        });
        await r.connect();
        await r.subscribe(c.bus, raw => {
          if(subscriberPromise!==pending)return;
          if (Buffer.byteLength(raw) > MAX_FRAME) return;
          let b; try { b = JSON.parse(raw); } catch { return; }
          if (!batchValid(b, c.mapping, now())) return;
          for (const peer of clients) {
            if (peer.readyState !== 1) continue;
            if (peer.bufferedAmount > 65536) peer.close(1013, "slow_client");
            else peer.send(raw);
          }
        });
        return r;
      })().catch(() => {
        if(subscriberPromise!==pending)return;
        subscriberPromise = null;
        for (const peer of clients) peer.close(1013, "stream_unavailable");
      });
      subscriberPromise=pending;
    }
    await subscriberPromise;
  }
  async function collect(durationMs = 55000, {continuous = false, signal} = {}) {
    const c = configuration(env);
    // Only the standalone staging service can opt out of the HTTP duration bound.
    if (continuous && (env.PULSE_STREAM_DAEMON_ENABLED !== "1" || !signal || typeof signal.aborted !== "boolean")) throw Error("daemon_scope_refused");
    // ADR-0159: 350 paced handshakes take70s; a55s invocation cannot own this feed.
    if (c.maxPairs > 50 && !continuous) throw Error("stream_persistent_runtime_required");
    if (collector) return { status:"lease_busy", retry_after_ms:5000 };
    collector = true;
    let r, owner, acquired = false, interval;
    const sockets = new Set(), timers = new Set(), latest = new Map(), highWater = new Map();
    let stopped = false, busy = false, failure = null, upstreamStatus = null, retryAfterMs = null;
    let nextHandshake = 0;
    const started = now(), before = {...stats};
    // 12 KiB/s including broker subscriber copies, with at most one frame of burst.
    let allowance = MAX_FRAME, lastAllowance = now(), lastPublish = now();
    let subscribers = 4;
    const end = continuous ? Infinity : started + Math.min(55000, Math.max(1, durationMs));
    let nextHealth = started + 60000;
    const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
    function schedule(fn, ms) { const t = setTimeout(() => { timers.delete(t); fn(); }, ms); timers.add(t); }
    function upstream(pair, attempt = 0) {
      if (stopped || now() >= end - 1000) return;
      if (now() < nextHandshake) {
        schedule(() => upstream(pair,attempt), nextHandshake-now());
        return;
      }
      nextHandshake = now()+200;
      const w = new Socket(UPSTREAM, { headers:{"User-Agent":"CoinDCX-Pulse/0.1"}, handshakeTimeout:3000,
        maxPayload:4096, followRedirects:false, perMessageDeflate:false });
      sockets.add(w); stats.upstream = sockets.size;
      let transientDelay=null;
      let watchdog = setTimeout(() => w.terminate(),45000);
      w.on("message", (data, binary) => {
        if (stopped || binary || data.length > 4096) return;
        clearTimeout(watchdog); watchdog = setTimeout(() => w.terminate(),45000);
        const raw = data.toString();
        if (raw[0] === "0") w.send("40");
        else if (raw === "2") w.send("3");
        else if (raw.startsWith("40")) w.send('42["join",{"channelName":' + JSON.stringify(pair + "@prices-futures") + '}]');
        else {
          const t = parseTick(raw, now());
          const old = highWater.get(pair);
          if (t && (!old || t.source_ms > old.source_ms)) {
            highWater.set(pair,t); latest.set(pair,t); stats.ticks++;
          } else if (t && old && t.source_ms === old.source_ms && t.price !== old.price) stats.dropped++;
        }
      });
      w.on("error", () => { stats.errors++; });
      w.on("unexpected-response", (_,response) => {
        // Preserve bounded HTTP diagnostics; never log response bodies, URLs or headers.
        transientDelay=transientRetryDelay(response.statusCode,response.headers,now(),attempt);
        if(transientDelay!==null){
          stats.transient_refusals++;response.resume();w.terminate();return;
        }
        upstreamStatus=Number.isInteger(response.statusCode)?response.statusCode:null;
        const raw=response.headers?.["retry-after"];
        if(typeof raw==="string"&&/^\d{1,7}$/.test(raw))retryAfterMs=Number(raw)*1000;
        response.resume(); failure = "upstream_refused"; stopped = true; w.terminate();
      });
      w.on("close", () => {
        clearTimeout(watchdog);
        sockets.delete(w); stats.upstream = sockets.size;
        if(!stopped&&transientDelay!==null){
          schedule(()=>upstream(pair,Math.min(attempt+1,30)),transientDelay);return;
        }
        // Fixed upper bound on retries; no tight loop after an upstream refusal.
        if (!stopped && attempt < 2) schedule(() => upstream(pair, attempt + 1), 1000 * 2 ** attempt + Math.random()*500);
        else if (!stopped && continuous) schedule(() => upstream(pair,0),60000+Math.random()*10000);
      });
    }
    async function flush() {
      if (busy || stopped) return;
      busy = true;
      try {
        const t = now();
        allowance = Math.min(MAX_FRAME*5, allowance + (t-lastAllowance)*c.bytesPerSecond/1000); lastAllowance = t;
        // ADR-0159: retain insertion order across small frames so no pair starves.
        const rows = pendingRows(latest);
        const body = rows.length ? JSON.stringify({schema:"pulse.ticks.v1", mapping_version:c.mapping, sent_ms:t, rows}) : "";
        if (Buffer.byteLength(body) > MAX_FRAME) throw Error("stream_batch_limit");
        const payload = Buffer.byteLength(body)*(1+subscribers) <= allowance ? body : "";
        if (!payload && t-lastPublish < 1000) return;
        const count = await r.eval(scopedScript(FENCED_PUBLISH,c), {keys:[c.lease,c.bus],arguments:[owner,payload]});
        if (count === -2) throw Error("stream_subscriber_capacity");
        if (count < 0) throw Error("stream_fenced_out");
        lastPublish = t;
        if (payload) {
          subscribers = count;
          allowance -= Buffer.byteLength(payload)*(1+count); stats.batches++; stats.published_bytes += Buffer.byteLength(payload);
          for (const row of rows) if (latest.get(row[0])?.source_ms === row[1]) latest.delete(row[0]);
        }
      } catch(error) {
        failure=["stream_batch_limit","stream_fenced_out","stream_subscriber_capacity"].includes(error.message)
          ?error.message:"stream_publish_failed";
        stopped = true;
      }
      finally { busy = false; }
    }
    try {
      r = await connection();
      const memory = await r.info("memory");
      const used = Number(memory.match(/(?:^|\r?\n)used_memory:(\d+)/)?.[1]);
      if (!Number.isSafeInteger(used) || used > c.stopBytes) throw Error("stream_memory_stop");
      const pairs = registry(await r.eval(scopedScript(READ_REGISTRY,c),{keys:[c.registry],arguments:[]}),c.mapping,now(),c.maxPairs);
      owner = randomUUID();
      acquired = await r.set(c.lease,owner,{NX:true,PX:5000}) === "OK";
      if (!acquired) return {status:"lease_busy", retry_after_ms:5000};
      interval = setInterval(flush,BATCH_MS);
      // At most five handshakes/s, independent of viewers. No REST/exchange credentials.
      pairs.forEach((pair,i) => schedule(() => upstream(pair),i*200));
      while (!stopped && !signal?.aborted && now() < end) {
        if (continuous && now() >= nextHealth) {
          // No exchange refresh or guessed replacement mapping. Expired/changed registry stops the writer.
          registry(await r.eval(scopedScript(READ_REGISTRY,c),{keys:[c.registry],arguments:[]}),c.mapping,now(),c.maxPairs);
          const health = await r.info("memory");
          const bytes = Number(health.match(/(?:^|\r?\n)used_memory:(\d+)/)?.[1]);
          if (!Number.isSafeInteger(bytes) || bytes > c.stopBytes) throw Error("stream_memory_stop");
          nextHealth = now()+60000;
        }
        await delay(Math.min(100,end-now()));
      }
      stopped = true;
      while (busy) await delay(5);
      return {status: failure ? "unavailable" : "completed", reason:failure, elapsed_ms:now()-started,
        upstream_status:upstreamStatus,retry_after_ms:retryAfterMs,
        pairs:pairs.length, ticks:stats.ticks-before.ticks, batches:stats.batches-before.batches,
        published_bytes:stats.published_bytes-before.published_bytes, errors:stats.errors-before.errors};
    } finally {
      stopped = true;
      clearInterval(interval); for (const t of timers) clearTimeout(t);
      for (const w of sockets) w.terminate();
      try { if (acquired) await r.eval(scopedScript(RELEASE,c),{keys:[c.lease],arguments:[owner]}); }
      catch { stats.errors++; } // On connection loss the original five-second expiry still bounds the lease.
      finally { collector = false; }
    }
  }
  return { attach, collect, stats, configuration:() => configuration(env),
    async close() { closed=true;clearTimeout(idleTimer);idleTimer=null;
      for (const peer of clients) peer.close();
      const pending=subscriberPromise;subscriberPromise=null;
      if (pending) (await pending)?.destroy();
      if (clientPromise) (await clientPromise).destroy(); } };
}
module.exports = {createBridge, configuration, scopedScript, authorised, registry, parseTick, batchValid, pendingRows, canonical, transientRetryDelay, READ_REGISTRY, FENCED_PUBLISH, RELEASE, MAX_FRAME};
