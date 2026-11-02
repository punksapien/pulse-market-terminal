/* Event-time price change from the snapshot's reference ladder (ADR-0005/0019). Mirrors the Python
   reference pulse.metrics.price_change.price_change; shared vectors prove status, reason and value.
   Pure: caller passes now_ms. Decimal exact; only the final fraction is projected to Number. */
(function (root, factory) {
  const isNode = typeof module === "object" && module.exports;
  const api = factory(isNode ? require("./pulse_decimal.js") : root.PulseDecimal);
  if (isNode) module.exports = api;
  else root.PulseLadder = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (D) {
  "use strict";
  const MINUTE = 60000;
  const WINDOW = { "1H": 3600000, "4H": 14400000, "24H": 86400000 };
  const ORDER = ["1H", "4H", "24H"];
  const SKEW = 5000;
  const DELAYED_MS = 120000;
  const MAX_TIME = Number.MAX_SAFE_INTEGER;
  const isTime = (v) => Number.isSafeInteger(v) && v >= 0 && v <= MAX_TIME;
  const positive = (text) => {
    const v = D.parse(text);
    return v && v.c > 0n ? v : null;
  };
  const no = (reason, extra = {}) => ({ status: "unavailable", reason, number: null, fraction: null, delayed: null, source: null, ...extra });

  // (current - reference) / reference, truncated at 40 decimals, then explicit Number projection
  function fraction(current, reference) {
    const diff = D.sub(current, reference);
    const numerator = diff.c * 10n ** BigInt(reference.s) * 10n ** 40n;
    const denominator = reference.c * 10n ** BigInt(diff.s);
    const scaled = numerator / denominator;
    const text = D.toString({ c: scaled, s: 40 });
    return { text, number: Number(text) };
  }

  function evaluate(price, ref, horizon, expectedMinute) {
    if (!ref || ref.status !== "available" || ref.reference_minute_ms !== expectedMinute) return no("reference_missing_or_invalid");
    const open = positive(ref.open_price);
    if (!open) return no("reference_missing_or_invalid");
    const f = fraction(price, open);
    if (!Number.isFinite(f.number)) return no("arithmetic_unavailable");
    return { status: "available", reason: null, number: f.number, fraction: f.text, delayed: null, source: null };
  }

  /* refs: row.references (wire). ctx: { mapping_version, definition_version, generation_id } pinned by the snapshot.
     tick: { price: decimal string, source_ms, expires_ms }. */
  function priceChange(refs, tick, horizon, ctx, now_ms) {
    if (!isTime(now_ms)) throw new RangeError("invalid_now");
    if (!(horizon in WINDOW)) throw new RangeError("invalid_horizon");
    const price = tick && positive(tick.price);
    if (!price || !isTime(tick.source_ms) || tick.source_ms > now_ms + SKEW) return no("invalid_tick");
    if (!isTime(tick.expires_ms) || tick.expires_ms <= tick.source_ms) return no("invalid_numerator_expiry");
    if (!refs || refs.status !== "available" || !refs.ladder) return no("pair_or_ladder_mismatch");
    if (refs.mapping_version !== ctx.mapping_version || refs.definition_version !== ctx.definition_version || refs.generation_id !== ctx.generation_id)
      return no("version_mismatch");
    const ladder = refs.ladder;
    const slots = ladder.slots;
    if (!isTime(ladder.built_at_ms) || ladder.built_at_ms > now_ms + SKEW || ladder.start_ms !== Math.floor(ladder.built_at_ms / MINUTE) * MINUTE
        || !Array.isArray(slots) || slots.length < 1 || slots.length > 8 || slots.some((r) => !Array.isArray(r) || r.length !== 3)
        || ladder.valid_until_ms !== ladder.start_ms + slots.length * MINUTE || ladder.valid_until_ms > MAX_TIME)
      return no("malformed_ladder");
    if (now_ms >= tick.expires_ms) return no("numerator_expired");
    if (now_ms >= ladder.valid_until_ms) return no("ladder_expired");
    if (tick.source_ms < ladder.start_ms || tick.source_ms >= ladder.valid_until_ms) return no("outside_ladder");
    const row = Math.floor((tick.source_ms - ladder.start_ms) / MINUTE);
    const expected = Math.floor((tick.source_ms - WINDOW[horizon]) / MINUTE) * MINUTE;
    const result = evaluate(price, slots[row][ORDER.indexOf(horizon)], horizon, expected);
    return result.status === "available" ? { ...result, source: "live" } : result;
  }

  /* Snapshot price with its own source time and exact references (no ladder dependency). */
  function fallbackChange(refs, horizon, ctx, now_ms) {
    if (!isTime(now_ms)) throw new RangeError("invalid_now");
    if (!(horizon in WINDOW)) throw new RangeError("invalid_horizon");
    const f = refs && refs.fallback;
    if (!refs || refs.status !== "available" || !f) return no("invalid_fallback");
    if (refs.mapping_version !== ctx.mapping_version || refs.definition_version !== ctx.definition_version || refs.generation_id !== ctx.generation_id)
      return no("version_mismatch");
    const price = positive(f.price);
    if (!price || !isTime(f.source_ms) || f.source_ms > now_ms + SKEW || !isTime(f.expires_ms) || f.expires_ms <= f.source_ms) return no("invalid_fallback");
    const delayed = now_ms - f.source_ms > DELAYED_MS;
    if (now_ms >= f.expires_ms) return no("fallback_expired", { delayed });
    const expected = Math.floor((f.source_ms - WINDOW[horizon]) / MINUTE) * MINUTE;
    const result = evaluate(price, f.references && f.references[horizon], horizon, expected);
    return { ...result, delayed, source: result.status === "available" ? "fallback" : null };
  }

  /* Prefer the live tick; fall back to the snapshot price only when no valid live tick covers `now`. */
  function best(refs, tick, horizon, ctx, now_ms) {
    if (tick) {
      const live = priceChange(refs, tick, horizon, ctx, now_ms);
      if (live.status === "available") return live;
    }
    return fallbackChange(refs, horizon, ctx, now_ms);
  }

  return { priceChange, fallbackChange, best, fraction };
});
