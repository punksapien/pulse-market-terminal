/* Live tick store: verified prices update independently; returns require the same tick and pinned ladder.
   ADR-0158: missing references never hide a valid price or retain an older percentage.
   Pure and synchronous: callers pass now_ms; no snapshot/index rebuild per tick. */
(function (root, factory) {
  const isNode = typeof module === "object" && module.exports;
  const api = factory(
    isNode ? require("./pulse_decimal.js") : root.PulseDecimal,
    isNode ? require("./pulse_ladder.js") : root.PulseLadder,
  );
  if (isNode) module.exports = api;
  else root.PulseTicks = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (D, ladder) {
  "use strict";
  const WINDOWS = ["1H", "4H", "24H"];
  const DEFINITION = "price_change.v1";
  const PRICE_SCHEMA = "pulse.live_price.v1";
  const TICK_TTL_MS = 60000; // a socket tick has no document expiry; the same 60 s numerator expiry the producer uses
  const SAMPLES = 256;

  function create() {
    let ctx = null;
    let mapping = null;
    const frame = new Map(); // pair id -> { wire_id, refs }
    const live = new Map(); // pair id -> entry
    const references = new Map(); // ADR-0193: at most two successful candidates per window/pair.
    const latencies = new Float64Array(SAMPLES);
    let sampleCount = 0;
    let revision = 0;

    /* ADR-0123: a complete prior frame keeps its own pinned provenance and validity. */
    function setFrame(snapshot, wire, now_ms) {
      const generation = wire && wire.generation;
      const mappingChanged = mapping !== snapshot.mapping_version;
      mapping = snapshot.mapping_version;
      ctx = { mapping_version: snapshot.mapping_version, definition_version: DEFINITION, generation_id: generation };
      frame.clear();
      for (const row of snapshot.rows) {
        const refs = wire.rows[row.wire_id] && wire.rows[row.wire_id].references;
        frame.set(row.id, { wire_id: row.wire_id, refs });
      }
      if (mappingChanged) { live.clear(); references.clear(); }
      const present = new Set(snapshot.rows.map(row => row.id));
      for (const [pair] of live) if (!present.has(pair)) live.delete(pair);
      for (const [pair] of references) if (!present.has(pair)) references.delete(pair);
      if (Number.isSafeInteger(now_ms)) for (const [pair, entry] of live)
        ingest(pair, { price:entry.text, source_ms:entry.source_ms, expires_ms:entry.expires_at }, now_ms);
      return frame.size;
    }

    /* tick: { price: decimal string, source_ms, expires_ms? }. Returns the entry, or null when no live frame. */
    function ingest(pairId, tick, now_ms) {
      const f = frame.get(pairId);
      const previous = get(pairId, now_ms);
      if (!ctx || !f || !tick) return previous || null;
      const full = { price: tick.price, source_ms: tick.source_ms, expires_ms: tick.expires_ms == null ? tick.source_ms + TICK_TTL_MS : tick.expires_ms };
      let numeric;
      try {
        if(typeof tick.price!=="string" || tick.price.length>64) return previous || null;
        const decimal=D.parse(tick.price); numeric=D.toNumber(decimal);
        if(D.compare(decimal,D.parse("0"))<=0 || !Number.isFinite(numeric)) return previous || null;
      } catch { return previous || null; }
      if(!Number.isSafeInteger(full.source_ms) || full.source_ms<0 || full.source_ms>now_ms+5000 ||
          !Number.isSafeInteger(full.expires_ms) || full.expires_ms<=full.source_ms ||
          full.expires_ms>full.source_ms+TICK_TTL_MS || now_ms>=full.expires_ms) return previous || null;
      if (previous && tick.source_ms < previous.source_ms) return previous; // out of order: keep the newer tick
      if (previous && previous.source_ms === tick.source_ms && previous.text === tick.price &&
          previous.expires_at===full.expires_ms && previous.frame_generation===ctx.generation_id && previous.reference_frame===f.refs) return previous;
      const changes = {};
      const provenance = {};
      const history = references.get(pairId) || {};
      let expires = full.expires_ms;
      for (const w of WINDOWS) {
        const candidates = [{ refs:f.refs, ctx }, ...(history[w] || [])];
        history[w] = (history[w] || []).filter(c => now_ms < (c.refs?.ladder?.valid_until_ms ?? 0));
        for (const candidate of candidates) {
          const r = ladder.priceChange(candidate.refs, full, w, candidate.ctx, now_ms);
          if (r.status !== "available") continue;
          changes[w] = r.number;
          provenance[w] = { generation:candidate.ctx.generation_id, expires_at:candidate.refs.ladder.valid_until_ms };
          if (candidate === candidates[0]) history[w] = [candidate, ...history[w].filter(c => c.ctx.generation_id !== ctx.generation_id)].slice(0,2);
          break;
        }
      }
      references.set(pairId, history);
      const generations = new Set(Object.values(provenance).map(p => p.generation));
      const entry = {
        price: numeric,
        text: tick.price,
        changes,
        source_ms: tick.source_ms,
        expires_at: expires,
        reference_expires_at: Object.keys(changes).length ? Math.min(...Object.values(provenance).map(p=>p.expires_at)) : null,
        reference_provenance: provenance,
        received_ms: previous?.source_ms === tick.source_ms && previous?.text === tick.price ? previous.received_ms : now_ms,
        reference_generation: generations.size === 1 ? [...generations][0] : null,
        frame_generation: ctx.generation_id,
        reference_frame: f.refs,
        rev: ++revision,
      };
      live.set(pairId, entry);
      return entry;
    }

    /* A pulse.live_price.v1 document: one tick per available row. Returns the pair ids whose entry changed. */
    function ingestDocument(doc, universe, now_ms) {
      if (!doc || doc.schema !== PRICE_SCHEMA || doc.mapping_version !== mapping || !Array.isArray(doc.rows)) return [];
      if (Number.isSafeInteger(doc.expires_ms) && now_ms >= doc.expires_ms) return [];
      const changed = [];
      for (const r of doc.rows) {
        const pair = universe[r && r.id];
        if (!pair || r.status !== "available" || typeof r.price !== "string") continue;
        const before = live.get(pair);
        const after = ingest(pair, { price: r.price, source_ms: r.source_ms, expires_ms: r.expires_ms }, now_ms);
        if (after && after !== before) changed.push(pair);
      }
      return changed;
    }

    /* The frame to show for a pair right now, or undefined (then the snapshot's own price and returns show). */
    function get(pairId, now_ms) {
      const e = live.get(pairId);
      if (!e) return undefined;
      if (now_ms >= e.expires_at) {
        live.delete(pairId);
        return undefined;
      }
      if(e.reference_expires_at!==null && now_ms>=e.reference_expires_at){
        const changes={...e.changes}, provenance={...e.reference_provenance};
        for (const w of WINDOWS) if (provenance[w] && now_ms>=provenance[w].expires_at) {
          delete changes[w];delete provenance[w];
        }
        const remaining=Object.values(provenance).map(p=>p.expires_at);
        const partial={...e,changes,reference_provenance:provenance,
          reference_expires_at:remaining.length?Math.min(...remaining):null,rev:++revision};
        live.set(pairId,partial);return partial;
      }
      return e;
    }

    function display(row, now_ms) {
      const tick = get(row.id, now_ms);
      if (tick) return tick;
      return { price: Number.isSafeInteger(row.price_expires_ms) && now_ms < row.price_expires_ms ? row.price : null,
        changes: Object.fromEntries(WINDOWS.map(w => [w, Number.isSafeInteger(row.change_expires_ms?.[w]) &&
          now_ms < row.change_expires_ms[w] ? row.changes[w] : null])) };
    }

    /* Source-to-paint age of a displayed tick (exchange clock vs the browser clock: skew is not removed). */
    function recordPaint(entry, now_ms) {
      latencies[sampleCount++ % SAMPLES] = now_ms - entry.source_ms;
    }

    function latency() {
      const n = Math.min(sampleCount, SAMPLES);
      if (!n) return { samples: 0, p50: null, p95: null, max: null };
      const sorted = Array.from(latencies.subarray(0, n)).sort((a, b) => a - b);
      const at = (q) => sorted[Math.min(n - 1, Math.floor(q * n))];
      return { samples: sampleCount, p50: at(0.5), p95: at(0.95), max: sorted[n - 1] };
    }

    return { setFrame, ingest, ingestDocument, get, display, recordPaint, latency, size: () => live.size, pairs: () => Array.from(frame.keys()) };
  }

  return { create, WINDOWS };
});
