/* Live-mode wording: removes sample/illustrative labelling from the design's copy when real data is
   shown. Only the design's own fixed phrases are rewritten; values are never touched here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PulseCopy = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const exact = new Map([
    ["Sample data · not live. Statistical estimates, not financial advice.", "Statistical estimates, not financial advice."],
    ["Sample delayed-data state · original observation age is retained.", "Data is delayed · the original observation age is retained."],
    ["Sample source-unavailable state · missing values are never zero.", "Source unavailable · missing values are never zero."],
    ["Delayed sample", "Delayed data"],
    ["Loading sample markets", "Loading markets"],
    ["Trading is disabled for sample data", "Trading is disabled in this preview"],
    ["Trade unavailable in design preview", "Trade unavailable in this preview"],
    ["Sample source unavailable", "Source unavailable"],
    ["Sample only", "Not applicable"],
    ["Illustrative context — not a trading recommendation.", "Not a trading recommendation."],
    ["Illustrative sample series · not exchange history", "History retained by the backend"],
    ["Illustrative sample history", "History"],
    ["Next settlement · sample", "Next settlement"],
    ["Last settled · sample", "Last settled"],
    ["Sample totals are illustrative; live totals require verified settlement coverage.", "Totals use verified regular settlements and are unavailable when coverage is incomplete."],
    ["Live integration requires matching source buckets and independent expiry.", "Top and global use the same source bucket; each series has its own expiry."],
    [" sample markets. ", " markets. "],
    ["Design preview information", "Preview information"],
  ]);
  const upper = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  function liveCopy(value) {
    if (typeof value !== "string") return value;
    if (exact.has(value)) return exact.get(value);
    let m = /^Sample data · not live\. Intended source: (.+)$/.exec(value);
    if (m) return "Source: " + m[1];
    m = /^Fixture created (.+) · not an exchange observation\.$/.exec(value);
    if (m) return "Snapshot generated " + m[1] + " · each value carries its own source time.";
    m = /^Sample RSI-14: (.+)\. Not a live observation\.$/.exec(value);
    if (m) return "RSI-14: " + m[1] + ".";
    let out = value
      .replace(/ · illustrative level$/, " · level")
      .replace(/ · sample$/, "")
      .replace(/ sample markets\./, " markets.")
      .replace(/ Sample rates, ratios, prices, volume, context and level assignments are fictional\. No forecasts, trading advice or verified settlement coverage are implied\./,
        " Levels outside volatility are pilot bands, not yet calibrated. No forecasts or trading advice are implied.")
      .replace(/^Sample · /, "");
    m = /^Sample (.+)$/.exec(out);
    if (m) out = upper(m[1]);
    return out;
  }
  return { liveCopy };
});
