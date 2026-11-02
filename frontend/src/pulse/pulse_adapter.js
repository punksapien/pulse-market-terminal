/* Frontend adapter: pulse.snapshot.v1 (docs/contracts) -> design-model snapshot.
   Pure and synchronous: no network, no clock reads (caller passes now_ms). Fails closed on an unknown
   schema, a malformed dictionary or a unit mismatch. Display numbers are explicit binary64 projections
   of decimal strings; funding arithmetic and threshold ties keep the decimal strings (field.text). */
(function (root, factory) {
  const isNode = typeof module === "object" && module.exports;
  const api = factory(
    isNode ? require("./pulse_model.js") : root.PulseModel,
    isNode ? require("./pulse_decimal.js") : root.PulseDecimal,
    isNode ? require("./pulse_live.js") : root.PulseLive,
    isNode ? require("./pulse_ladder.js") : root.PulseLadder,
  );
  if (isNode) module.exports = api;
  else root.PulseAdapter = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (model, D, live, ladder) {
  "use strict";
  const SCHEMA = "pulse.snapshot.v1";
  const LIVE_SCHEMA = "pulse.live_funding.v1";
  const PRICE_SCHEMA = "pulse.live_price.v1";
  const PRICE_DEFINITION = "price_change.v1";
  const MAX_ROWS = 50000;
  const HASH = /^sha256:[0-9a-f]{64}$/;
  const PAIR = /^B-[A-Z0-9]{1,24}_USDT$/;
  const STATUSES = ["available", "unavailable", "settlement_pending", "unsupported"];
  const WIRE_UNIT = {
    volatility: ["fraction"],
    long_short: ["ratio"],
    funding: ["fraction"],
    funding_change: ["fraction"],
    open_interest: ["fraction"],
  };
  const SIGNAL_FALLBACK = "Unavailable";
  const fail = (code) => {
    throw new Error(code);
  };
  const isTime = (v) => Number.isSafeInteger(v) && v >= 0;

  function validateEnvelope(wire) {
    if (!wire || typeof wire !== "object" || wire.schema !== SCHEMA) fail("unknown_schema");
    if (!HASH.test(wire.mapping_version || "") || !HASH.test(wire.generation || "")) fail("invalid_identity");
    if (!model.WINDOWS.includes(wire.horizon)) fail("invalid_horizon");
    if (!isTime(wire.generated_ms) || !isTime(wire.expires_ms) || wire.expires_ms <= wire.generated_ms) fail("invalid_envelope_time");
    if (!Array.isArray(wire.universe) || !Array.isArray(wire.rows) || wire.rows.length !== wire.universe.length || wire.rows.length > MAX_ROWS)
      fail("invalid_dictionary");
    const seen = new Set();
    wire.universe.forEach((u, i) => {
      if (!u || u.id !== i || !PAIR.test(u.pair_id) || seen.has(u.pair_id)) fail("invalid_dictionary");
      if (typeof u.symbol !== "string" || !u.symbol || (u.funding_hours != null && ![1, 4, 8].includes(u.funding_hours))) fail("invalid_dictionary");
      seen.add(u.pair_id);
      if (!wire.rows[i] || wire.rows[i].id !== i) fail("row_dictionary_mismatch");
    });
  }

  // wire field -> cell; unavailable or expired fields never carry a number.
  function cell(unit, field, now, extra = {}) {
    const ok = field && field.status === "available" && typeof field.value === "string";
    const value = ok ? D.parse(field.value) : null;
    const usable = Boolean(value) && isTime(field.asof_ms) && isTime(field.expires_ms) && field.expires_ms > now;
    if (field && !STATUSES.includes(field.status)) fail("unknown_status");
    return {
      value: usable ? D.toNumber(value) : null,
      text: usable ? D.toString(value) : null,
      unit,
      status: usable ? "available" : "unavailable",
      reason: usable ? null : field && field.status === "available" ? "expired" : (field && field.reason) || "missing",
      asof: usable ? field.asof_ms : 0,
      expires_at: usable ? field.expires_ms : 0,
      source: extra.source || "",
      definition_version: (field && field.definition_version) || "",
      // ADR-0110: a numeric level alone is not a current, versioned classification.
      level: usable && field.classification_status === "classified" && typeof field.classification_version === "string" && field.classification_version.length > 0 &&
        Number.isInteger(field.level) && field.level >= 1 && field.level <= 4 ? field.level : null,
      ...extra.cell,
    };
  }
  const num = (field, now) => cell("", field, now).value;
  const direction = (id, text) => {
    if (text == null) return "";
    const v = D.parse(text);
    const s = v.c === 0n ? 0 : v.c > 0n ? 1 : -1;
    return {
      long_short: ["Short dominant", "Balanced", "Long dominant"][
        D.compare(v, { c: 1n, s: 0 }) + 1
      ],
      funding: ["Shorts pay", "Zero", "Longs pay"][s + 1],
      funding_change: ["Falling", "Unchanged", "Rising"][s + 1],
      open_interest: ["Decreasing", "Unchanged", "Increasing"][s + 1],
    }[id] || "";
  };

  function history(entry, now) {
    if (!entry || entry.status !== "available" || !Array.isArray(entry.points)) return [];
    return entry.points.map((p) => D.toNumber(D.parse(p.value))).filter(Number.isFinite);
  }

  function settledVisible(fc, field, now) {
    // available settlement fields stop being visible at due (nft); then pending, not the prior number
    if (fc && isTime(fc.nft) && now >= fc.nft && field && field.status === "available")
      return { ...field, status: "settlement_pending", reason: "settlement_pending", value: null };
    return field;
  }

  function adaptRow(wire, r, now) {
    const u = wire.universe[r.id];
    const funding = r.funding || {};
    const fc = r.funding_change || {};
    const ls = r.ls || {};
    const oi = r.oi || {};
    const vol = r.volatility || {};
    const metrics = {};
    metrics.volatility = {};
    metrics.long_short = {};
    metrics.funding = {};
    metrics.funding_change = {};
    metrics.open_interest = {};
    const volDir = vol.direction && vol.direction.status === "available" ? vol.direction.value : "";
    const estimate = cell("rate_fraction", funding.native_estimate, now, { source: "CoinDCX · estimated funding" });
    for (const w of model.WINDOWS) {
      const wf = vol.windows && vol.windows[w];
      metrics.volatility[w] = cell("fraction", wf || vol.score, now, {
        source: "CoinDCX futures candles",
        cell: { direction: volDir === "Stable" ? "Steady" : volDir },
      });
      const ratio = cell("ratio", ls.period_ratios ? ls.period_ratios[w] : ls.global_ratio, now, {
        source: "Binance USDⓈ-M · global accounts",
        cell: { evolution: num(ls.global_share_change && ls.global_share_change[w], now) },
      });
      ratio.direction = direction("long_short", ratio.text);
      metrics.long_short[w] = ratio;
      // Funding windows are Current / 1D / 7D: each is its own wire field, never the current estimate repeated.
      const fundingCell = w === "1H" ? estimate : cell("rate_fraction", w === "4H" ? funding.cumulative_1d : funding.cumulative_7d, now, {
        source: "Binance USDⓈ-M · settled funding, cumulative",
      });
      metrics.funding[w] = {
        ...fundingCell,
        direction: direction("funding", fundingCell.text),
        cumulative1D: num(funding.cumulative_1d, now),
        cumulative7D: num(funding.cumulative_7d, now),
      };
      // Funding Chg windows are Last / 1D / 7D settled changes.
      const changeField = w === "1H" ? fc.settled_last : w === "4H" ? fc.settled_1d : fc.settled_7d;
      const last = cell("rate_fraction_delta", settledVisible(fc, changeField, now), now, {
        source: "Binance USDⓈ-M · regular settlements",
        cell: {
          delta1D: num(settledVisible(fc, fc.settled_1d, now), now),
          delta7D: num(settledVisible(fc, fc.settled_7d, now), now),
          basis_bps: fc.basis_bps && fc.basis_bps.status === "available" ? fc.basis_bps.value : null,
        },
      });
      last.direction = direction("funding_change", last.text);
      metrics.funding_change[w] = last;
      const oiField = oi["change_" + w.toLowerCase()];
      const change = cell("fraction", oiField, now, {
        source: "Binance USDⓈ-M · open interest",
        cell: { mode: oiField && oiField.mode, baseline_error_ms: oiField && oiField.baseline_error_ms },
      });
      change.direction = direction("open_interest", change.text);
      metrics.open_interest[w] = change;
    }
    for (const id of Object.keys(metrics))
      for (const w of model.WINDOWS) if (metrics[id][w].unit && !WIRE_UNIT[id]) fail("metric_unit_mismatch");
    const price = cell("price", r.price, now);
    const changes = {};
    for (const w of model.WINDOWS) changes[w] = num(r.changes && r.changes[w], now);
    const outlook = r.outlook || {};
    const since = r.signal_since;
    return {
      id: u.pair_id,
      wire_id: u.id,
      symbol: u.symbol.replace(/USDT$/, ""),
      price: price.value,
      price_expires_ms: r.price?.expires_ms ?? null,
      change_expires_ms: Object.fromEntries(model.WINDOWS.map(w => [w, r.changes?.[w]?.expires_ms ?? null])),
      volume: num(r.volume && r.volume.quote_24h, now),
      volume_source_ms: r.volume?.quote_24h?.asof_ms ?? null,
      volume_expires_ms: r.volume?.quote_24h?.expires_ms ?? null,
      volume_definition_version: r.volume?.quote_24h?.definition_version ?? "volume.rt.v1",
      signal: r.signal && r.signal.category && r.signal.category.status === "available" ? r.signal.category.value : SIGNAL_FALLBACK,
      changes,
      metrics,
      history: {
        volatility: history(r.history && r.history.volatility, now),
        long_short: history(r.history && r.history.ls, now),
        funding: history(r.history && r.history.funding, now),
        funding_change: history(r.history && r.history.funding_change, now),
        open_interest: history(r.history && r.history.oi, now),
      },
      outlook: {
        label: outlook.label && outlook.label.status === "available" ? outlook.label.value : "",
        confidence: num(outlook.confidence, now),
        description: outlook.description && outlook.description.status === "available" ? outlook.description.value : "",
      },
      funding_meta: {
        status: fc.status === "available" && isTime(fc.expires_ms) && fc.expires_ms > now ? "available" : "unavailable",
        h: fc.h == null ? null : fc.h,
        nft: isTime(fc.nft) ? fc.nft : isTime(funding.nft) ? funding.nft : null,
        settled_rate: typeof fc.settled_rate === "string" && D.parse(fc.settled_rate) ? fc.settled_rate : null,
        settlement_ms: isTime(fc.settlement_ms) ? fc.settlement_ms : null,
        verified_at_ms: isTime(fc.verified_at_ms) ? fc.verified_at_ms : null,
      },
      signal_since:
        since && since.status === "available" && isTime(since.expires_ms) && since.expires_ms > now
          ? { state: since.state, price: D.toNumber(D.parse(since.price)) }
          : null,
      context: {
        interval: u.funding_hours,
        rsi: num(r.context && r.context.rsi_14, now),
        quantity: num(oi.quantity, now),
        topRatio: num(ls.top_ratio, now),
        topShare: num(ls.top_long_share, now),
        longShare: num(ls.global_long_share, now),
        shareGapPp: num(ls.divergence, now),
        momentum: num(r.context && r.context.volatility_momentum, now),
        velocity: r.context && r.context.volatility_velocity && r.context.volatility_velocity.status === "available" ? r.context.volatility_velocity.value : "",
      },
    };
  }

  function adapt(wire, now_ms) {
    if (!isTime(now_ms)) fail("invalid_now");
    validateEnvelope(wire);
    const rows = wire.rows.map((r) => adaptRow(wire, r, now_ms));
    return {
      registry_version: model.VERSION,
      snapshot_id: wire.generation,
      mapping_version: wire.mapping_version,
      horizon: wire.horizon,
      generated_at: wire.generated_ms,
      expires_at: wire.expires_ms,
      sample: false,
      rows,
      // expiry beats cache: nothing from this snapshot is shown after envelope expiry
      envelope_expired: now_ms >= wire.expires_ms,
    };
  }

  /* Merge the 5 s live funding document. Unknown mapping fails closed (live cannot establish one).
     Returns a new snapshot-like object; rows are not mutated. */
  function applyLiveFunding(snapshot, wire, liveDoc, now_ms) {
    if (!isTime(now_ms)) fail("invalid_now");
    const result = { applied: 0, unavailable: 0, reason: null };
    if (!liveDoc || liveDoc.schema !== LIVE_SCHEMA || liveDoc.mapping_version !== snapshot.mapping_version || !Array.isArray(liveDoc.rows)) {
      result.reason = "live_mapping_or_schema_mismatch";
      return { snapshot, result };
    }
    if (isTime(liveDoc.expires_ms) && now_ms >= liveDoc.expires_ms) {
      result.reason = "live_document_expired";
      return { snapshot, result };
    }
    const byId = new Map();
    for (const row of liveDoc.rows) if (row && Number.isInteger(row.id)) byId.set(row.id, row);
    const rows = snapshot.rows.map((row) => {
      const l = byId.get(row.wire_id);
      const fc = wire.rows[row.wire_id] && wire.rows[row.wire_id].funding_change;
      if (!l) return row;
      const delta = live.currentDelta(liveDoc, l, fc, now_ms);
      const efr = l.status === "available" ? D.parse(l.efr) : null;
      // the row's own expires_ms, the source-derived freshness and the funding due time all bound the estimate
      const rowExpiry = efr && isTime(l.source_ms) && isTime(l.nft)
        ? Math.min(l.source_ms + live.FRESH_MS, l.nft, isTime(l.expires_ms) ? l.expires_ms : Infinity)
        : 0;
      const fresh = Boolean(efr) && now_ms < rowExpiry && l.source_ms <= now_ms + 5000;
      const metrics = { ...row.metrics, funding: {}, funding_change: {} };
      for (const w of model.WINDOWS) {
        const base = row.metrics.funding[w];
        metrics.funding[w] = fresh && w === "1H" // the live estimate is the Current window only
          ? {
              ...base,
              value: D.toNumber(efr),
              text: D.toString(efr),
              status: "available",
              reason: null,
              asof: l.source_ms,
              expires_at: rowExpiry,
              direction: direction("funding", D.toString(efr)),
              // A changed live rate cannot inherit the old rate's band. Current Delta remains unclassified.
              level: Number.isInteger(l.level) && l.level >= 1 && l.level <= 4 && typeof l.classification_version === "string" && l.classification_version.startsWith("funding.v1.l2.")
                ? l.level
                : base.status === "available" && base.expires_at > now_ms && base.text != null &&
                row.funding_meta.h === l.h && row.funding_meta.nft === l.nft && D.compare(D.parse(base.text), efr) === 0 ? base.level : null,
              live: true,
            }
          : base;
        metrics.funding_change[w] = { ...row.metrics.funding_change[w], current_delta: delta };
      }
      result.applied += fresh ? 1 : 0;
      result.unavailable += fresh ? 0 : 1;
      return { ...row, metrics };
    });
    return { snapshot: { ...snapshot, rows }, result };
  }

  /* ADR-0158: verified live prices stand independently; missing references clear only returns.
     Unknown mapping, invalid/expired price or document still cannot replace a valid frame. */
  function applyLivePrice(snapshot, wire, doc, now_ms) {
    if (!isTime(now_ms)) fail("invalid_now");
    const result = { applied: 0, unavailable: 0, reason: null, expires_at: null };
    const refuse = (reason) => ({ snapshot, result: { ...result, reason } });
    if (!doc || doc.schema !== PRICE_SCHEMA || doc.mapping_version !== snapshot.mapping_version || !Array.isArray(doc.rows))
      return refuse("live_mapping_or_schema_mismatch");
    if (isTime(doc.expires_ms) && now_ms >= doc.expires_ms) return refuse("live_document_expired");
    const byId = new Map();
    for (const r of doc.rows) if (r && Number.isInteger(r.id)) byId.set(r.id, r);
    let earliest = Infinity;
    let pending = 0;
    const rows = snapshot.rows.map((row) => {
      const l = byId.get(row.wire_id);
      const refs = wire.rows[row.wire_id] && wire.rows[row.wire_id].references;
      if (!l) return row;
      if (!refs) {
        pending += 1;
      }
      const tick = l.status === "available" && typeof l.price === "string" ? { price: l.price, source_ms: l.source_ms, expires_ms: l.expires_ms } : null;
      let price;
      try {
        if(!tick || !isTime(tick.source_ms) || tick.source_ms>now_ms+5000 || !isTime(tick.expires_ms) ||
            tick.expires_ms<=tick.source_ms || tick.expires_ms>tick.source_ms+60000 || now_ms>=tick.expires_ms || tick.price.length>64) throw Error();
        const decimal=D.parse(tick.price);price=D.toNumber(decimal);
        if(D.compare(decimal,D.parse("0"))<=0 || !Number.isFinite(price)) throw Error();
      } catch { result.unavailable+=1;return row; }
      const ctx = { mapping_version: snapshot.mapping_version, definition_version: PRICE_DEFINITION, generation_id: wire.generation };
      const changes = Object.fromEntries(model.WINDOWS.map(w=>[w,null]));
      for (const w of model.WINDOWS) {
        const r = tick ? ladder.priceChange(refs, tick, w, ctx, now_ms) : null;
        if (r && r.status === "available") {
          changes[w] = r.number;
        }
      }
      // ADR-0158: valid numerator stands alone; never retain an old percentage with it.
      earliest = Math.min(earliest, l.expires_ms);
      result.applied += 1;
      return { ...row, price, changes, price_live: { source_ms: l.source_ms, expires_ms: l.expires_ms } };
    });
    if (pending) result.reason = "references_pending";
    result.expires_at = Number.isFinite(earliest) ? earliest : null;
    return { snapshot: { ...snapshot, rows, price_expires_at: result.expires_at }, result };
  }

  // ADR-0118: volume never waits for a price or reference ladder.
  function retainVolume(snapshot, previous, now) {
    const old = new Map(previous?.mapping_version === snapshot.mapping_version
      ? previous.rows.map(r => [r.id, r]) : []);
    const rows = snapshot.rows.map(row => {
      const v = old.get(row.id);
      const current = isTime(row.volume_expires_ms) && now >= row.volume_expires_ms ? null : row.volume;
      if (v && v.wire_id === row.wire_id && Number.isFinite(v.volume) && v.volume >= 0 &&
          isTime(v.volume_source_ms) && v.volume_source_ms <= now + 5000 &&
          v.volume_expires_ms === v.volume_source_ms + ({"volume.rt.v1":60000,"volume.rt.v2":300000}[v.volume_definition_version || "volume.rt.v1"] ?? NaN) && now < v.volume_expires_ms &&
          (!Number.isFinite(current) || v.volume_source_ms > (row.volume_source_ms ?? -1)))
        return { ...row, volume: v.volume, volume_source_ms: v.volume_source_ms, volume_expires_ms: v.volume_expires_ms,
          volume_definition_version:v.volume_definition_version || "volume.rt.v1" };
      return current === row.volume ? row : { ...row, volume: current };
    });
    return { ...snapshot, rows };
  }

  function applyLiveVolume(snapshot, doc, now) {
    if (!doc || doc.schema !== PRICE_SCHEMA || doc.mapping_version !== snapshot.mapping_version || !Array.isArray(doc.rows)) return snapshot;
    const ids = new Set();
    for (const r of doc.rows) { if (!r || !Number.isInteger(r.id) || ids.has(r.id)) return snapshot; ids.add(r.id); }
    const byId = new Map(doc.rows.map(r => [r.id, r.volume]));
    const envelope = isTime(doc.generated_ms) && doc.generated_ms <= now + 5000 && isTime(doc.expires_ms) && now < doc.expires_ms;
    const rows = snapshot.rows.map(row => {
      const v = byId.get(row.wire_id);
      let volume = isTime(row.volume_expires_ms) && now >= row.volume_expires_ms ? null : row.volume;
      if (envelope && v?.status === "available" && v.unit === "USDT" && v.reason === null && typeof v.value === "string"
          && isTime(v.source_ms) && v.source_ms <= now + 5000 && isTime(v.expires_ms) && now < v.expires_ms
          && v.expires_ms === v.source_ms + ({"volume.rt.v1":60000,"volume.rt.v2":300000}[v.definition_version || "volume.rt.v1"] ?? NaN) && v.source_ms >= (row.volume_source_ms ?? 0)) {
        try {
          const number = D.toNumber(D.parse(v.value));
          if (Number.isFinite(number) && number >= 0) return { ...row, volume: number, volume_source_ms: v.source_ms, volume_expires_ms: v.expires_ms,
            volume_definition_version:v.definition_version || "volume.rt.v1" };
        } catch (_) { /* Invalid decimal leaves the independently valid snapshot value. */ }
      }
      return volume === row.volume ? row : { ...row, volume };
    });
    return { ...snapshot, rows };
  }

  return { SCHEMA, LIVE_SCHEMA, PRICE_SCHEMA, adapt, applyLiveFunding, applyLivePrice, applyLiveVolume, retainVolume, validateEnvelope };
});
