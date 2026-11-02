/* Contract pulse_snapshot_v1 "Live funding": pair Current Delta only with a verified settlement of
   the same cycle. Pure, exact decimal, no clock reads (caller passes now_ms). */
(function (root, factory) {
  const api = factory(
    typeof module === "object" && module.exports ? require("./pulse_decimal.js") : root.PulseDecimal,
  );
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PulseLive = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (D) {
  "use strict";
  const FRESH_MS = 120000;
  const FUTURE_SKEW_MS = 5000;
  const MAX_TIME = Number.MAX_SAFE_INTEGER;
  const isTime = (v) => Number.isSafeInteger(v) && v >= 0 && v <= MAX_TIME;
  const unavailable = (reason) => ({
    status: "unavailable",
    reason,
    value: null,
    number: null,
    expires_ms: null,
    classification_status: "unclassified",
  });

  /* live: one row of pulse.live_funding.v1; envelope carries mapping_version.
     settled: funding_change record of the main snapshot row for the same id. */
  function currentDelta(envelope, live, settled, now_ms) {
    if (!isTime(now_ms)) throw new RangeError("invalid_now");
    if (!envelope || !live || !settled) return unavailable("missing_input");
    if (envelope.mapping_version == null || envelope.mapping_version !== settled.mapping_version)
      return unavailable("mapping_mismatch");
    if (live.id !== settled.id) return unavailable("identity_mismatch");
    if (live.status !== "available") return unavailable(live.reason || "live_unavailable");
    if (settled.status !== "available") return unavailable(settled.reason || "settled_unavailable");
    if (live.h !== settled.h || live.nft !== settled.nft) return unavailable("cycle_identity_mismatch");
    if (!isTime(live.nft) || !isTime(live.source_ms) || !isTime(settled.settlement_ms) || ![1, 4, 8].includes(settled.h))
      return unavailable("invalid_time");
    const efr = D.parse(live.efr);
    const fr = D.parse(live.fr);
    const rate = D.parse(settled.settled_rate);
    if (!efr || !fr || !rate) return unavailable("invalid_decimal");
    if (D.compare(fr, rate) !== 0) return unavailable("settled_rate_mismatch");
    if (live.source_ms > now_ms + FUTURE_SKEW_MS) return unavailable("future_estimate");
    // The actual funding cycle ends h hours after the retained settlement; the published nft can be a few
    // seconds later (jitter). Both bound the estimate, and the retained operand is not usable past rollover.
    const due = settled.settlement_ms + settled.h * 3600000;
    if (now_ms >= due) return unavailable("settlement_pending");
    // an estimate must belong to this cycle: not before the retained settlement, not at or after its due time
    if (live.source_ms < settled.settlement_ms || live.source_ms >= due) return unavailable("cycle_identity_mismatch");
    // expiry is source-derived, never receipt-derived (contract)
    const expires = Math.min(live.source_ms + FRESH_MS, live.nft, due, isTime(live.expires_ms) ? live.expires_ms : Infinity);
    if (now_ms >= expires) return unavailable(now_ms >= live.nft ? "settlement_pending" : "stale_estimate");
    const value = D.sub(efr, rate);
    return {
      status: "available",
      reason: null,
      value: D.toString(value),
      bps: D.toString(D.mulPow10(value, 4)),
      number: D.toNumber(value),
      expires_ms: expires,
      classification_status: "unclassified",
    };
  }

  return { currentDelta, FRESH_MS };
});
