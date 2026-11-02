/* ADR-0070 (frontend wiring): exact decimal strings via scaled BigInt. No Number arithmetic
   on funding values; Number is only the final, explicit projection for rendering. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PulseDecimal = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const GRAMMAR = /^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/;
  const MAX_LENGTH = 64;
  const MAX_EXPONENT = 40;
  const MAX_SCALE = 60;

  // value = coefficient * 10^-scale
  function parse(text) {
    if (typeof text !== "string" || text.length > MAX_LENGTH) return null;
    const m = GRAMMAR.exec(text);
    if (!m) return null;
    const fraction = m[3] || "";
    let scale = fraction.length - (m[4] ? Number(m[4]) : 0);
    if (m[4] && Math.abs(Number(m[4])) > MAX_EXPONENT) return null;
    let coefficient = BigInt(m[2] + fraction);
    if (scale < 0) {
      coefficient *= 10n ** BigInt(-scale);
      scale = 0;
    }
    if (scale > MAX_SCALE) return null;
    return { c: m[1] === "-" ? -coefficient : coefficient, s: scale };
  }

  function align(a, b) {
    const s = Math.max(a.s, b.s);
    return [a.c * 10n ** BigInt(s - a.s), b.c * 10n ** BigInt(s - b.s), s];
  }

  function sub(a, b) {
    const [x, y, s] = align(a, b);
    return { c: x - y, s };
  }

  function mulPow10(a, power) {
    // exact multiplication by 10^power (power may be negative); no rounding
    return power >= 0
      ? { c: a.c * 10n ** BigInt(power), s: a.s }
      : { c: a.c, s: a.s - power };
  }

  function compare(a, b) {
    const [x, y] = align(a, b);
    return x < y ? -1 : x > y ? 1 : 0;
  }

  function isZero(a) {
    return a.c === 0n;
  }

  // canonical plain decimal string, trailing zeros removed, no "-0"
  function toString(a) {
    let c = a.c;
    let s = a.s;
    while (s > 0 && c % 10n === 0n) {
      c /= 10n;
      s -= 1;
    }
    const negative = c < 0n;
    let digits = (negative ? -c : c).toString();
    if (s > 0) {
      digits = digits.padStart(s + 1, "0");
      digits = digits.slice(0, digits.length - s) + "." + digits.slice(digits.length - s);
    }
    return (negative ? "-" : "") + digits;
  }

  // Explicit, lossy final projection (correctly rounded by the engine's string parser).
  function toNumber(a) {
    return Number(toString(a));
  }

  return { parse, sub, mulPow10, compare, isZero, toString, toNumber };
});
