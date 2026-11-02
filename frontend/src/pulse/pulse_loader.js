/* Snapshot and live-funding loader: single-flight fetches with ETag, atomic generation switch,
   backoff with jitter, Retry-After, visibility pause, explicit stale/error states. No DOM, no globals:
   fetch, clock, timers and visibility are injected, so every path is testable. A 304 never extends expiry:
   freshness is judged from field expiry (adapter + model.effective), not from the poll time. */
(function (root, factory) {
  const isNode = typeof module === "object" && module.exports;
  const api = factory(isNode ? require("./pulse_adapter.js") : root.PulseAdapter);
  if (isNode) module.exports = api;
  else root.PulseLoader = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (adapter) {
  "use strict";
  const DEFAULTS = {
    snapshotMs: 30000,
    liveMs: 5000,
    priceMs: 2000, // live price pointer poll: price and price-change must feel live
    backoffBaseMs: 2000,
    backoffMaxMs: 60000,
    maxBodyChars: 8 * 1024 * 1024,
    staleAfterFailures: 3,
    requestTimeoutMs: 12000,
  };

  function create(options) {
    const o = { ...DEFAULTS, ...options };
    for (const k of ["fetch", "now", "setTimer", "clearTimer", "snapshotUrl", "liveUrl"]) if (typeof o[k] !== "function") throw new TypeError("missing_" + k);
    // wire transport: snapshotUrl returns a pulse.manifest.v1; parts are immutable addressed artifacts (ADR-0081/0082/0085)
    const wire = o.wire || null;
    if (wire && typeof o.artifactUrl !== "function") throw new TypeError("missing_artifactUrl");
    const random = o.random || Math.random;
    const visible = o.isVisible || (() => true);
    const state = {
      phase: "loading", horizon: null, snapshot: null, wire: null, live: { status: "idle", reason: null, applied: 0 },
      snapshotError: null, failures: { snapshot: 0, live: 0, price: 0 }, price: { status: "off", reason: null, applied: 0 }, generation: null, mapping_version: null, updated_at: 0, notices: [],
      parts: { wanted: ["core"], loaded: [], pending: [] },
    };
    const etag = { snapshot: null, live: null, price: null };
    const timers = { snapshot: null, live: null, price: null };
    const inflight = { snapshot: null, live: null, price: null };
    const cancellations = new Map();
    let liveDoc = null;
    let priceDoc = null;
    let volumeFrame = null; // ADR-0193: only accepted scalar observations, with original expiry.
    let overlay = false; // live-price return overlay (ADR-0088): needs the references part of the same generation
    let manifest = null; // the COMMITTED manifest only; a candidate is promoted by a successful atomic commit
    let buildSeq = 0; // every build gets a token; a superseded build can never touch state, caches or the ETag
    const partCache = new Map(); // logical_sha256 -> decoded part (immutable)
    let verifiedMapping = null;
    let base = null; // adapted snapshot before live merge
    let epoch = 0; // bumps on horizon change so late responses are discarded
    let stopped = false;
    const listeners = new Set();
    const emit = () => {
      state.updated_at = o.now();
      for (const l of listeners) l(state);
    };
    const jitter = (ms) => Math.round(ms * (0.85 + 0.3 * random()));
    const backoff = (n, retryAfter) =>
      Math.max(retryAfter || 0, Math.min(o.backoffMaxMs, jitter(o.backoffBaseMs * 2 ** Math.min(n, 10))));

    function recompute() {
      if (!base) return;
      const now = o.now();
      let snap = base;
      if (liveDoc) {
        const r = adapter.applyLiveFunding(base, state.wire, liveDoc, now);
        snap = r.snapshot;
        state.live = { ...state.live, applied: r.result.applied, reason: r.result.reason };
      }
      snap = adapter.retainVolume(snap, volumeFrame, now);
      if (overlay && priceDoc && state.wire) {
        snap = adapter.applyLiveVolume(snap, priceDoc, now);
        if (typeof o.priceSink === "function") {
          const applied = o.priceSink(priceDoc, {...state,snapshot:snap});
          state.price = { ...state.price, applied, reason: null };
        } else {
          const r = adapter.applyLivePrice(snap, state.wire, priceDoc, now);
          snap = r.snapshot;
          state.price = { ...state.price, applied: r.result.applied, reason: r.result.reason };
        }
      }
      volumeFrame = { mapping_version: snap.mapping_version, rows: snap.rows.map(r => ({
        id:r.id, wire_id:r.wire_id, volume:r.volume, volume_source_ms:r.volume_source_ms, volume_expires_ms:r.volume_expires_ms,
        volume_definition_version:r.volume_definition_version,
      })) };
      state.snapshot = snap;
      const expired = snap.envelope_expired || now >= snap.expires_at;
      state.phase = snap.rows.length === 0 ? "empty" : expired || state.failures.snapshot >= o.staleAfterFailures ? "stale" : "ready";
    }

    function classify(response) {
      const status = response.status;
      const retry = Number(response.headers && response.headers.get && response.headers.get("retry-after"));
      return { status, retryAfterMs: Number.isFinite(retry) && retry > 0 ? Math.min(retry, 3600) * 1000 : 0 };
    }

    async function request(kind, url) {
      if (inflight[kind]) return inflight[kind]; // single flight
      const myEpoch = epoch;
      const headers = { Accept: "application/json" };
      if (etag[kind]) headers["If-None-Match"] = etag[kind];
      const controller=typeof AbortController==="function"?new AbortController():null;
      let timeout, cancel;
      const bounded=new Promise(resolve=>{
        cancel=()=>{controller?.abort();resolve({discarded:true});};
        timeout=o.setTimer(()=>{controller?.abort();resolve({error:"request_timeout"});},o.requestTimeoutMs,"request");
      });
      const work = (async () => {
        try {
          // ADR-0104: retain same-origin Preview protection, never cross-origin credentials.
          const response = await o.fetch(url, { headers, credentials: "same-origin", cache: "no-cache", signal:controller?.signal });
          if (myEpoch !== epoch) return { discarded: true };
          const c = classify(response);
          if (c.status === 304) return { notModified: true };
          if (c.status !== 200) return { error: "http_" + c.status, retryAfterMs: c.retryAfterMs };
          const text = await response.text();
          if (myEpoch !== epoch) return { discarded: true };
          if (text.length > o.maxBodyChars) return { error: "response_too_large" };
          let body;
          try {
            body = JSON.parse(text);
          } catch (e) {
            return { error: "invalid_json" };
          }
          const tag = response.headers && response.headers.get && response.headers.get("etag");
          return { body, etag: tag || null };
        } catch (e) {
          return { error: "network" };
        }
      })();
      const p=Promise.race([work,bounded]).finally(()=>{
        o.clearTimer(timeout);
        if(inflight[kind]===p){delete inflight[kind];cancellations.delete(kind);}
      });
      inflight[kind] = p;
      cancellations.set(kind,cancel);
      return p;
    }
    function cancelRequests(){
      for(const cancel of cancellations.values())cancel();
      cancellations.clear();
      for(const kind of Object.keys(inflight))delete inflight[kind];
    }

    function schedule(kind, ms) {
      if (stopped) return;
      if (timers[kind] != null) o.clearTimer(timers[kind]);
      timers[kind] = o.setTimer(() => {
        timers[kind] = null;
        (kind === "snapshot" ? pollSnapshot : kind === "live" ? pollLive : pollPrice)();
      }, ms, kind);
    }

    // atomic switch: wire, adapted rows and the live merge move together, one generation at a time
    function commit(next, logical, tag, committedManifest) {
      const mappingChanged = base && base.mapping_version !== next.mapping_version;
      base = next;
      if (committedManifest) manifest = committedManifest;
      state.wire = logical;
      state.generation = next.snapshot_id;
      state.mapping_version = next.mapping_version;
      etag.snapshot = tag;
      if (mappingChanged) {
        liveDoc = null;
        priceDoc = null;
        etag.live = null;
        etag.price = null;
        state.live = { status: "idle", reason: "mapping_changed", applied: 0 };
        state.notices = ["mapping_changed"];
      }
    }

    function validateManifest(m, horizon) {
      const HASH = /^sha256:[0-9a-f]{64}$/;
      if (!m || m.schema !== "pulse.manifest.v1") throw new Error("unknown_manifest_schema");
      if (m.horizon !== horizon) throw new Error("horizon_mismatch");
      if (!HASH.test(m.mapping_version || "") || !HASH.test(m.generation || "")) throw new Error("invalid_identity");
      if (!Number.isSafeInteger(m.generated_ms) || !Number.isSafeInteger(m.expires_ms) || m.expires_ms <= m.generated_ms) throw new Error("invalid_envelope_time");
      for (const name of wire.PARTS) {
        const d = m.parts && m.parts[name];
        if (!d || d.schema !== "pulse.artifact.v1" || !HASH.test(d.logical_sha256 || "") || d.generation !== m.generation || d.mapping_version !== m.mapping_version)
          throw new Error("invalid_manifest_part");
      }
    }

    async function fetchPart(m, name, myEpoch, seq) {
      const key = m.parts[name].logical_sha256;
      if (partCache.has(key)) return partCache.get(key);
      const r = await request("part:" + key, o.artifactUrl(key));
      if (r.discarded) return null;
      if (r.error || r.notModified) throw new Error(r.error || "artifact_not_modified");
      if (o.digest && (await o.digest(r.body)) !== key) throw new Error("artifact_digest_mismatch");
      if (myEpoch === epoch && seq === buildSeq) partCache.set(key, r.body);
      return r.body;
    }

    async function buildFrom(m, myEpoch, tag) {
      const seq = ++buildSeq;
      const stale = () => seq !== buildSeq || myEpoch !== epoch || stopped;
      // active parts are always refreshed; parts of THIS generation that are already resident join without a fetch
      const resident = wire.PARTS.filter((n) => partCache.has(m.parts[n].logical_sha256));
      const names = Array.from(new Set([...state.parts.wanted, ...resident]));
      const docs = await Promise.all(names.map((n) => fetchPart(m, n, myEpoch, seq)));
      if (stale() || docs.some((d) => d === null)) return false;
      const { snapshot } = wire.assemble(Object.fromEntries(names.map((n, i) => [n, docs[i]])));
      if (snapshot.generation !== m.generation || snapshot.mapping_version !== m.mapping_version || snapshot.horizon !== m.horizon)
        throw new Error("manifest_part_mismatch");
      if (o.digest && verifiedMapping !== snapshot.mapping_version) {
        if ((await o.digest(snapshot.universe)) !== snapshot.mapping_version) throw new Error("mapping_hash_mismatch");
        verifiedMapping = snapshot.mapping_version;
      }
      // last check before anything becomes visible: a newer build or a horizon change wins
      if (stale()) return false;
      const next = adapter.adapt(snapshot, o.now());
      commit(next, snapshot, tag, m);
      state.parts = { wanted: state.parts.wanted, loaded: names, pending: state.parts.wanted.filter((n) => !names.includes(n)) };
      // immutable parts of other generations are dropped
      const keep = new Set(names.map((n) => m.parts[n].logical_sha256));
      for (const k of partCache.keys()) if (!keep.has(k)) partCache.delete(k);
      return true;
    }

    async function acceptManifest(m, horizon, tag, myEpoch) {
      validateManifest(m, horizon);
      if (base && state.generation === m.generation && state.parts.wanted.every((n) => state.parts.loaded.includes(n))) {
        manifest = m;
        etag.snapshot = tag;
        return;
      }
      // the candidate's ETag is promoted only by a successful commit, so a failed artifact is retried on the next poll
      await buildFrom(m, myEpoch, tag);
    }

    async function pollSnapshot() {
      if (stopped || !state.horizon) return;
      if (!visible()) return schedule("snapshot", o.snapshotMs);
      const horizon = state.horizon;
      const myEpoch = epoch;
      const result = await request("snapshot", o.snapshotUrl(horizon));
      if (result.discarded || stopped) return;
      if (result.notModified) {
        state.failures.snapshot = 0;
        recompute();
        emit();
        return schedule("snapshot", jitter(o.snapshotMs));
      }
      if (result.error) {
        state.failures.snapshot += 1;
        state.snapshotError = result.error;
        if (!base) state.phase = "error";
        else recompute();
        emit();
        return schedule("snapshot", backoff(state.failures.snapshot, result.retryAfterMs));
      }
      try {
        if (wire) await acceptManifest(result.body, horizon, result.etag, myEpoch);
        else {
          const next = adapter.adapt(result.body, o.now());
          if (next.horizon !== horizon) throw new Error("horizon_mismatch");
          commit(next, result.body, result.etag);
        }
        if (stopped || myEpoch !== epoch) return;
        state.failures.snapshot = 0;
        state.snapshotError = null;
        recompute();
      } catch (e) {
        // an invalid generation never replaces a good one
        state.failures.snapshot += 1;
        state.snapshotError = String(e.message || e);
        if (!base) state.phase = "error";
        else recompute();
      }
      emit();
      schedule("snapshot", state.snapshotError ? backoff(state.failures.snapshot, 0) : jitter(o.snapshotMs));
    }

    async function pollLive() {
      if (stopped || !state.horizon) return;
      if (!visible() || !base) return schedule("live", o.liveMs);
      const result = await request("live", o.liveUrl());
      if (result.discarded || stopped) return;
      if (result.notModified) {
        state.failures.live = 0;
        recompute();
        emit();
        return schedule("live", jitter(o.liveMs));
      }
      if (result.error) {
        state.failures.live += 1;
        state.live = { ...state.live, status: "error", reason: result.error };
        recompute();
        emit();
        return schedule("live", backoff(state.failures.live, result.retryAfterMs));
      }
      const body = result.body;
      if (!body || body.schema !== adapter.LIVE_SCHEMA || body.mapping_version !== base.mapping_version) {
        // unknown or foreign mapping: fail closed, keep the settled-document values only
        liveDoc = null;
        state.failures.live += 1;
        state.live = { status: "error", reason: "live_mapping_or_schema_mismatch", applied: 0 };
        recompute();
        emit();
        return schedule("live", backoff(state.failures.live, 0));
      }
      liveDoc = body;
      etag.live = result.etag;
      state.failures.live = 0;
      state.live = { status: "ok", reason: null, applied: 0 };
      recompute();
      emit();
      schedule("live", jitter(o.liveMs));
    }

    async function pollPrice() {
      if (stopped || !state.horizon || !overlay || !o.livePriceUrl) return;
      if (!visible() || !base) return schedule("price", o.priceMs);
      const result = await request("price", o.livePriceUrl());
      if (result.discarded || stopped || !overlay) return;
      if (result.notModified) {
        state.failures.price = 0;
        recompute();
        emit();
        return schedule("price", jitter(o.priceMs));
      }
      if (result.error) {
        state.failures.price += 1;
        state.price = { ...state.price, status: "error", reason: result.error };
        recompute();
        emit();
        return schedule("price", backoff(state.failures.price, result.retryAfterMs));
      }
      const body = result.body;
      if (!body || body.schema !== adapter.PRICE_SCHEMA || body.mapping_version !== base.mapping_version) {
        priceDoc = null;
        state.failures.price += 1;
        state.price = { status: "error", reason: "live_mapping_or_schema_mismatch", applied: 0 };
        recompute();
        emit();
        return schedule("price", backoff(state.failures.price, 0));
      }
      priceDoc = body;
      etag.price = result.etag;
      state.failures.price = 0;
      state.price = { status: "ok", reason: null, applied: 0 };
      recompute();
      emit();
      schedule("price", jitter(o.priceMs));
    }

    return {
      state,
      subscribe(fn) {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
      start(horizon, parts) {
        stopped = false;
        if (wire && parts) state.parts = { wanted: Array.from(new Set(["core", ...parts, ...(overlay ? ["references"] : [])])), loaded: [], pending: [] };
        this.setHorizon(horizon);
      },
      setHorizon(horizon, parts) {
        if (!["1H", "4H", "24H"].includes(horizon)) throw new RangeError("invalid_horizon");
        if (wire && parts) state.parts = { wanted: Array.from(new Set(["core", ...parts, ...(overlay ? ["references"] : [])])), loaded: [], pending: [] };
        epoch += 1;
        cancelRequests();
        for (const k of ["snapshot", "live", "price"]) {
          if (timers[k] != null) o.clearTimer(timers[k]);
          timers[k] = null;
          inflight[k] = null;
          etag[k] = null;
        }
        base = null;
        liveDoc = null;
        priceDoc = null;
        manifest = null;
        buildSeq += 1;
        partCache.clear();
        state.parts = { wanted: state.parts.wanted.slice(), loaded: [], pending: [] };
        state.horizon = horizon;
        state.snapshot = null;
        state.wire = null;
        state.phase = "loading";
        state.live = { status: "idle", reason: null, applied: 0 };
        state.failures = { snapshot: 0, live: 0, price: 0 };
        state.snapshotError = null;
        emit();
        pollSnapshot().then(() => { pollLive(); pollPrice(); });
      },
      /* Set the ACTIVE parts (what the open view needs). Parts already resident for the current generation
         join at once; missing ones are fetched; parts no longer active are kept only until the generation changes
         and are never refreshed automatically. The current view keeps rendering meanwhile. */
      async ensureParts(names) {
        if (!wire) return true;
        for (const n of names) if (!wire.PARTS.includes(n)) throw new RangeError("unknown_part");
        const wanted = Array.from(new Set(["core", ...names, ...(overlay ? ["references"] : [])]));
        const resident = state.parts.loaded;
        state.parts = { wanted, loaded: resident, pending: wanted.filter((n) => !resident.includes(n)) };
        emit();
        if (!manifest || !state.parts.pending.length) return true;
        const myEpoch = epoch;
        try {
          const done = await buildFrom(manifest, myEpoch, etag.snapshot);
          if (done) {
            state.snapshotError = null;
            recompute();
          }
        } catch (e) {
          state.snapshotError = String(e.message || e);
        }
        emit();
        return state.parts.pending.length === 0;
      },
      // call on visibilitychange to refresh at once instead of waiting for the next timer
      wake() {
        if (stopped || !state.horizon) return;
        for (const k of ["snapshot", "live", "price"]) {
          if (timers[k] != null) o.clearTimer(timers[k]);
          timers[k] = null;
        }
        pollSnapshot().then(() => { pollLive(); pollPrice(); });
      },
      // re-evaluate expiry without any network (called from the render clock)
      tick() {
        recompute();
        emit();
      },
      stop() {
        stopped = true;
        epoch += 1;
        cancelRequests();
        for (const k of ["snapshot", "live", "price"]) if (timers[k] != null) o.clearTimer(timers[k]);
      },
      /* Switch the live-price return overlay on or off. On: the references part of the current generation joins
         (lazy, one fetch, pinned to the generation) and the price document is polled; off: the snapshot frame returns. */
      async setPriceOverlay(on) {
        if (!o.livePriceUrl) throw new TypeError("missing_livePriceUrl");
        overlay = Boolean(on);
        if (!overlay) {
          if (timers.price != null) o.clearTimer(timers.price);
          timers.price = null;
          priceDoc = null;
          etag.price = null;
          state.price = { status: "off", reason: null, applied: 0 };
          if (wire) state.parts = { ...state.parts, wanted: state.parts.wanted.filter((n) => n !== "references") };
          recompute();
          emit();
          return true;
        }
        state.price = { status: "idle", reason: "references_pending", applied: 0 };
        pollPrice(); // ADR-0118: volume needs no reference ladder download.
        if (wire) await this.ensureParts(state.parts.wanted.filter((n) => n !== "core"));
        return true;
      },
    };
  }

  return { create, DEFAULTS };
});
