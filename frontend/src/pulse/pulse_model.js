/* ADR-0025: pure registry/snapshot/view model. No DOM, network or thresholds. */
(function (root, factory) {
  const model = factory();
  if (typeof module === "object" && module.exports) module.exports = model;
  else root.PulseModel = model;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const VERSION = "pulse-design.v2";
  const WINDOWS = ["1H", "4H", "24H"];
  const COLORS = { 1: "#4965D2", 2: "#37B271", 3: "#F7931A", 4: "#F13B4D" };
  const REGISTRY = [
    {
      id: "volatility",
      label: "Volatility",
      column: "Volatility",
      unit: "fraction",
      headline: "Volatility score",
      levels: ["Calm", "Active", "Hot", "Extreme"],
      directions: ["Surging", "Rising", "Steady", "Falling", "Contracting"],
      filter: "Volatility direction",
      icon: "gauge",
      description:
        "Estimated price dispersion over the selected horizon. Magnitude is not a price-direction forecast.",
      source: "CoinDCX futures candles",
      breakdown: ["1H Volatility", "4H Volatility", "24H Volatility"],
    },
    {
      id: "long_short",
      label: "L/S Ratio",
      column: "L/S Ratio",
      unit: "ratio",
      headline: "Global accounts L/S ratio",
      levels: ["Balanced", "Tilted", "Crowded", "Extreme"],
      directions: ["Long dominant", "Short dominant", "Balanced"],
      filter: "Accounts skew",
      icon: "accounts",
      description:
        "Net-long accounts divided by net-short accounts. Account counts do not measure position size or exposure.",
      source: "Binance USDⓈ-M · global accounts",
      breakdown: ["1H share change", "4H share change", "24H share change"],
    },
    {
      id: "funding",
      label: "Funding",
      column: "Funding",
      unit: "rate_fraction",
      headline: "Estimated funding rate",
      levels: ["Neutral", "Mild", "Elevated", "Extreme"],
      directions: ["Longs pay", "Shorts pay", "Zero"],
      filter: "Funding direction",
      icon: "funding",
      description:
        "Estimate per native funding interval. Positive means longs pay; negative means shorts pay. Annualisation is not a yield forecast.",
      source: "CoinDCX · estimated funding",
      breakdown: ["Native interval", "1D cumulative", "7D cumulative"],
    },
    {
      id: "funding_change",
      label: "Funding Chg",
      column: "Funding Δ",
      unit: "rate_fraction_delta",
      headline: "Last-settlement funding change",
      levels: ["Stable", "Drifting", "Swinging", "Spiking"],
      directions: ["Rising", "Falling", "Unchanged"],
      filter: "Settled-rate direction",
      icon: "delta",
      description:
        "Latest minus previous regular settlement, only when the interval and coverage are verified. Expressed in absolute basis points, not relative percent.",
      source: "Binance USDⓈ-M · regular settlements",
      breakdown: ["Last settlement Δ", "1D settled Δ", "7D settled Δ"],
    },
    {
      id: "open_interest",
      label: "Open Interest",
      column: "OI change",
      unit: "fraction",
      headline: "Open interest change",
      levels: ["Steady", "Active", "Strong", "Extreme"],
      directions: ["Increasing", "Decreasing", "Unchanged"],
      filter: "OI direction",
      icon: "oi",
      description:
        "Simple return in open-interest quantity over the selected window. Quantity removes the price effect; baseline timing error is disclosed.",
      source: "Binance USDⓈ-M · open interest",
      breakdown: ["1H OI change", "4H OI change", "24H OI change"],
    },
  ];
  // ADR-0026: Figma filter controls are sample UI ranges, never level cutoffs.
  const controls = {
    volatility: {
      key: "direction",
      title: "Volatility direction",
      options: [
        ["all", "All"],
        ...["Surging", "Rising", "Steady", "Falling", "Contracting"].map(
          (s) => [s, s],
        ),
      ],
    },
    long_short: {
      key: "threshold",
      title: "Minimum L/S ratio",
      options: [
        ["all", "Any"],
        ["gt:1", "> 1"],
        ["gt:1.5", "> 1.5"],
        ["gt:2", "> 2"],
        ["gt:3", "> 3"],
      ],
    },
    funding: {
      key: "interval",
      title: "Funding interval",
      options: [
        ["all", "All"],
        ["1", "1H"],
        ["4", "4H"],
        ["8", "8H"],
      ],
    },
    funding_change: {
      key: "threshold",
      title: "Settled funding delta (bps)",
      options: [
        ["all", "Any"],
        ["gt:0.00015", "> +1.5 bps"],
        ["gt:0.0003", "> +3 bps"],
        ["lt:-0.00015", "< −1.5 bps"],
        ["lt:-0.0003", "< −3 bps"],
      ],
    },
    open_interest: {
      key: "threshold",
      title: "OI delta",
      options: [
        ["all", "Any"],
        ["gt:0.1", "> +10%"],
        ["gt:0.2", "> +20%"],
        ["lt:-0.1", "< −10%"],
        ["lt:-0.2", "< −20%"],
      ],
    },
  };
  const windowLabels = (id) =>
    ["funding", "funding_change"].includes(id)
      ? id === "funding"
        ? ["Current", "1D", "7D"]
        : ["Last", "1D", "7D"]
      : id === "long_short" ? ["1H", "4H", "24H"] : ["1H Chg", "4H Chg", "24H Chg"];
  const changeWindow = (id, w) =>
    ["funding", "funding_change"].includes(id) ? "24H" : w;
  REGISTRY.forEach((m) => {
    m.control = controls[m.id];
    m.control.options.forEach(Object.freeze);
    Object.freeze(m.control.options);
    Object.freeze(m.control);
    Object.freeze(m.levels);
    Object.freeze(m.directions);
    Object.freeze(m.breakdown);
    Object.freeze(m);
  });
  Object.freeze(REGISTRY);
  const metric = (id) => REGISTRY.find((m) => m.id === id);
  const numeric = (v) => typeof v === "number" && Number.isFinite(v);
  // ADR-0200: presentation only; missing values remain null for math, sort and filters.
  function missingState(reason, loading = false) {
    if (loading || reason === "part_not_loaded") return {kind:"loading",label:"Loading",description:"Loading verified data."};
    if (reason === "settlement_pending") return {kind:"pending",label:"Pending",description:"Waiting for the next settlement to be verified."};
    if (["unsupported","unsupported_contract","not_listed"].includes(reason)) return {kind:"unavailable",label:"N/A",description:"This source does not support this contract or metric."};
    if (["insufficient_history","history_missing","baseline_missing","discarded_baseline"].includes(reason)) return {kind:"pending",label:"Pending",description:"Not enough verified history for this value yet."};
    if (["expired","stale","stale_price","stale_estimate","source_expired"].includes(reason)) return {kind:"updating",label:"Updating",description:"The previous observation expired. Waiting for a verified update."};
    return {kind:"unavailable",label:"No data",description:"A verified value is currently unavailable. Other fields continue updating independently."};
  }
  function effective(cell, now, phase = "ready") {
    if (phase === "unavailable") return "unavailable";
    if (
      !cell ||
      !numeric(cell.value) ||
      !["available", "stale"].includes(cell.status)
    )
      return "unavailable";
    if (
      !numeric(cell.asof) ||
      !numeric(cell.expires_at) ||
      cell.asof > now + 5000 ||
      cell.expires_at <= now
    )
      return "unavailable";
    return phase === "stale" ? "stale" : cell.status;
  }
  const formatters = new Map();
  function formatter(min, max) {
    const key = min + ":" + max;
    if (!formatters.has(key))
      formatters.set(
        key,
        new Intl.NumberFormat("en-US", {
          minimumFractionDigits: min,
          maximumFractionDigits: max,
        }),
      );
    return formatters.get(key);
  }
  function decimals(n, places = 2) {
    return formatter(places, places).format(n);
  }
  function percentage(v, signed = false, places = 2, suffix = "%") {
    if (!numeric(v)) return "—";
    const n = v * 100;
    if (n !== 0 && Math.abs(n) < 1e-8)
      return (signed && n > 0 ? "+" : "") + n.toExponential(2) + suffix;
    const p =
      n !== 0 && Math.abs(n) < Math.pow(10, -places)
        ? Math.min(8, Math.ceil(-Math.log10(Math.abs(n))) + 1)
        : places;
    return (
      (signed && n > 0 ? "+" : "") +
      decimals(Object.is(n, -0) ? 0 : n, p) +
      suffix
    );
  }
  function format(id, value) {
    if (!numeric(value)) return "—";
    if (id === "long_short") return formatter(0, 2).format(value) + "x";
    // ADR-0125: round only the display; payload fractions and classifications stay unchanged.
    if (id === "funding" || id === "funding_change") {
      const bps = Object.is(value, -0) ? 0 : value * 10000;
      const rounded = formatter(2, 2).format(bps);
      const text = rounded === "-0.00" ? "0.00" : rounded;
      return (id === "funding_change" && bps > 0 && text !== "0.00" ? "+" : "") + text + " bps";
    }
    return percentage(value, id === "open_interest", id === "funding" ? 4 : 2);
  }
  function price(v) {
    if (!numeric(v)) return "—";
    if (v !== 0 && Math.abs(v) < 1e-15) return v.toExponential(4);
    const places =
      Math.abs(v) >= 1000
        ? 2
        : Math.abs(v) >= 1
          ? 5
          : v === 0
            ? 0
            : Math.min(16, Math.ceil(-Math.log10(Math.abs(v))) + 5);
    return formatter(0, places).format(v);
  }
  function compact(v) {
    if (!numeric(v)) return "—";
    for (const [size, suffix] of [
      [1e9, "B"],
      [1e6, "M"],
      [1e3, "K"],
    ]) {
      if (Math.abs(v) >= size)
        return (
          decimals(v / size, v / size >= 100 ? 0 : 2).replace(/\.00$/, "") +
          suffix
        );
    }
    return decimals(v, 0);
  }
  const defaults = () => ({
    signal: "all",
    direction: "all",
    volume: 0,
    availability: "all",
    search: "",
    threshold: "all",
    interval: "all",
  });
  const filterCount = (f) =>
    Number(f.signal !== "all") +
    Number(f.direction !== "all") +
    Number(f.volume > 0) +
    Number(f.availability !== "all") +
    Number(Boolean(f.search)) +
    Number(f.threshold !== "all") +
    Number(f.interval !== "all");
  function matchesThreshold(value, threshold) {
    if (threshold === "all") return true;
    const match = parseThreshold(threshold);
    if (!match) return false;
    return (
      numeric(value) &&
      (match.op === "gt" ? value > match.bound : value < match.bound)
    );
  }
  function parseThreshold(threshold) {
    if (typeof threshold !== "string") return null;
    const match = /^(gt|lt):(-?\d+(?:\.\d+)?)$/.exec(threshold);
    return match ? { op: match[1], bound: Number(match[2]) } : null;
  }
  // ADR-0027: avoid a deadline array/spread proportional to the universe.
  function nextExpiry(snapshot, after = -Infinity) {
    let next = Infinity;
    for (const r of snapshot.rows) {
      for (const deadline of [r.volume_expires_ms, r.price_expires_ms, ...Object.values(r.change_expires_ms || {})])
        if (numeric(deadline) && deadline > after && deadline < next) next = deadline;
      for (const m of REGISTRY)
        for (const w of WINDOWS) {
          const deadline = r.metrics?.[m.id]?.[w]?.expires_at;
          if (numeric(deadline) && deadline > after && deadline < next)
            next = deadline;
        }
    }
    return next;
  }
  const bit = (mask, i) => (mask[i >>> 5] & (1 << (i & 31))) !== 0;
  const mark = (mask, i) => {
    mask[i >>> 5] |= 1 << (i & 31);
  };
  function freeze(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value))
      return value;
    for (const child of Object.values(value)) freeze(child);
    return Object.freeze(value);
  }

  class Index {
    constructor(snapshot) {
      if (
        !snapshot ||
        snapshot.registry_version !== VERSION ||
        !snapshot.snapshot_id ||
        !Array.isArray(snapshot.rows) ||
        snapshot.rows.length > 50000
      )
        throw Error("snapshot_registry_mismatch");
      this.snapshot = freeze(snapshot);
      this.rows = snapshot.rows;
      this.byId = new Map();
      this.orders = new Map();
      this.columns = new Map();
      this.masks = new Map();
      this.words = Math.ceil(this.rows.length / 32);
      const Order = this.rows.length <= 65535 ? Uint16Array : Uint32Array;
      const ids = this.rows.map((r, i) => {
        if (!/^B-[A-Z0-9]+_USDT$/.test(r.id) || this.byId.has(r.id))
          throw Error("invalid_or_duplicate_pair");
        this.byId.set(r.id, i);
        return i;
      });
      const order = (key, read) => {
        this.orders.set(
          key,
          Order.from(
            ids.slice().sort((a, b) => {
              const x = read(this.rows[a]),
                y = read(this.rows[b]);
              if (typeof x === "string") return x.localeCompare(y) || a - b;
              return (
                (numeric(y) ? y : -Infinity) - (numeric(x) ? x : -Infinity) ||
                a - b
              );
            }),
          ),
        );
      };
      order("symbol", (r) => r.symbol);
      this.orders.set("design", Order.from(ids));
      order("volume", (r) => r.volume);
      order("price", (r) => r.price);
      for (const w of WINDOWS) {
        order("change:" + w, (r) => r.changes[w]);
        for (const m of REGISTRY) {
          const key = m.id + ":" + w;
          const values = new Float64Array(this.rows.length).fill(NaN);
          const levels = new Uint8Array(this.rows.length);
          const expiry = new Float64Array(this.rows.length);
          this.rows.forEach((r, i) => {
            const c = r.metrics?.[m.id]?.[w];
            if (c && c.unit !== m.unit) throw Error("metric_unit_mismatch");
            values[i] = c && numeric(c.value) ? c.value : NaN;
            levels[i] =
              c && Number.isInteger(c.level) && c.level >= 1 && c.level <= 4
                ? c.level
                : 0;
            expiry[i] = c?.expires_at || 0;
            for (const facet of [
              "level:" + levels[i],
              "signal:" + r.signal,
              "direction:" + (c?.direction || ""),
            ]) {
              const mk = key + ":" + facet;
              if (!this.masks.has(mk))
                this.masks.set(mk, new Uint32Array(this.words));
              mark(this.masks.get(mk), i);
            }
          });
          this.columns.set(key, { values, levels, expiry });
          order(key, (r) => r.metrics?.[m.id]?.[w]?.value);
        }
      }
    }
    select(
      {
        metric: id,
        window: w,
        level = 0,
        filters = defaults(),
        sort = "metric",
        descending = true,
        phase = "ready",
      },
      now,
      liveRead = null,
    ) {
      if (!metric(id) || !WINDOWS.includes(w)) throw Error("unknown_view");
      const key = id + ":" + w;
      const mask = new Uint32Array(this.words).fill(0xffffffff);
      for (const facet of [
        level ? "level:" + level : null,
        filters.signal !== "all" ? "signal:" + filters.signal : null,
        filters.direction !== "all" ? "direction:" + filters.direction : null,
      ]) {
        if (!facet) continue;
        const next = this.masks.get(key + ":" + facet);
        for (let i = 0; i < mask.length; i++) mask[i] &= next ? next[i] : 0;
      }
      const field =
        sort === "metric"
          ? key
          : sort === "change"
            ? "change:" + changeWindow(id, w)
            : sort;
      const order = this.orders.get(field);
      if (!order) throw Error("unknown_sort");
      const present = [],
        absent = [],
        counts = [0, 0, 0, 0, 0];
      const threshold =
        filters.threshold === "all" ? null : parseThreshold(filters.threshold);
      if (filters.threshold !== "all" && !threshold) return { ids: [], counts };
      const query = String(filters.search || "")
        .trim()
        .toUpperCase();
      const ascending = sort === "symbol" ? !descending : descending;
      for (let j = 0; j < order.length; j++) {
        const i = order[ascending ? j : order.length - j - 1];
        if (!bit(mask, i)) continue;
        const r = this.rows[i],
          c = r.metrics?.[id]?.[w];
        const st = effective(c, now, phase);
        if (
          filters.threshold !== "all" &&
          (st !== "available" ||
            !(threshold.op === "gt"
              ? c.value > threshold.bound
              : c.value < threshold.bound))
        )
          continue;
        if (
          filters.interval !== "all" &&
          String(r.context?.interval) !== filters.interval
        )
          continue;
        if (level && st !== "available") continue;
        if (filters.availability !== "all" && st !== filters.availability)
          continue;
        if (
          filters.volume &&
          (!numeric(r.volume) || r.volume <= filters.volume)
        )
          continue;
        if (query && !r.symbol.includes(query)) continue;
        (st === "unavailable" ? absent : present).push(i);
        if (st === "available" && c.level) counts[c.level]++;
      }
      const ids = present.concat(absent);
      if (sort === "price" || sort === "change") {
        // ADR-0193: sort the same frame paint uses, independently of metric availability.
        const values = new Float64Array(this.rows.length).fill(NaN);
        for (const i of ids) {
          const row = this.rows[i], frame = liveRead ? liveRead(row, now) : row;
          const value = sort === "price" ? frame?.price : frame?.changes?.[changeWindow(id, w)];
          if (numeric(value)) values[i] = value;
        }
        ids.sort((a,b) => {
          const x=values[a], y=values[b], ax=Number.isFinite(x), by=Number.isFinite(y);
          if (ax !== by) return ax ? -1 : 1;
          return (ax ? (descending ? y-x : x-y) : 0) || a-b;
        });
      }
      return { ids, counts };
    }
  }
  function virtualRange(
    total,
    scrollTop,
    height,
    rowHeight = 68,
    overscan = 3,
  ) {
    const start = Math.max(
      0,
      Math.floor(Math.max(0, scrollTop - 12) / rowHeight) - overscan,
    );
    const end = Math.min(
      total,
      start + Math.ceil(Math.max(0, height) / rowHeight) + overscan * 2 + 1,
    );
    return {
      start: Math.min(start, total),
      end,
      offset: Math.min(start, total) * rowHeight,
      height: total * rowHeight + 12,
    };
  }
  // ADR-0127: sheet context uses separate lazy wire parts; no duplicated metadata.
  function detailParts(id) {
    return ({volatility:["volatility","oi","funding"],long_short:["ls","oi","funding"],
      funding:["funding","funding_change","oi"],funding_change:["funding_change","funding"],
      open_interest:["oi","ls","funding"]}[id] || []).slice();
  }
  return {
    VERSION,
    detailParts,
    WINDOWS,
    COLORS,
    REGISTRY,
    metric,
    numeric,
    missingState,
    effective,
    format,
    percentage,
    price,
    compact,
    defaults,
    filterCount,
    matchesThreshold,
    nextExpiry,
    windowLabels,
    changeWindow,
    Index,
    virtualRange,
  };
});
