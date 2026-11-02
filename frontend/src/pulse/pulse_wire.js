/* Compact columnar wire v1 (ADR-0081) decoder. Mirrors the producer's reference decoder
   (pulse/wire/compact.py) so that parts reconstruct the logical pulse.snapshot.v1 rows exactly.
   Pure: no network, no clock. Caps are enforced before allocation; every malformed input throws a
   short code. Missing parts are simply absent: nothing is zero-filled or taken from another generation. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PulseWire = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const PARTS = ["core", "volatility", "ls", "funding", "funding_change", "oi", "references"];
  const METRIC_PARTS = ["volatility", "ls", "funding", "funding_change", "oi"];
  const TIMES = ["asof_ms", "expires_ms", "input_refs"];
  const SPECIAL = new Set(["value", "status", "reason", ...TIMES]);
  const FORBIDDEN = new Set(["__proto__", "constructor", "prototype"]);
  const IDENTITY = ["mapping_version", "generation", "horizon", "count", "generated_ms", "expires_ms"];
  const LIMITS = { count: 8192, columns: 1024, depth: 32, tables: 100000 };
  const fail = (code) => {
    throw new Error(code);
  };
  const isInt = (v) => Number.isInteger(v);
  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const clone = (v) => (v !== null && typeof v === "object" ? structuredClone(v) : v);

  function expand(encoded, n) {
    if (!Array.isArray(encoded) || encoded.length === 0) fail("invalid_vector");
    if (encoded[0] === "c" && encoded.length === 2) return Array.from({ length: n }, () => clone(encoded[1]));
    if (encoded[0] === "d" && encoded.length === 2 && Array.isArray(encoded[1]) && encoded[1].length === n) return encoded[1].map(clone);
    if (encoded[0] === "s" && encoded.length === 3 && Array.isArray(encoded[2])) {
      const out = Array.from({ length: n }, () => clone(encoded[1]));
      let previous = -1;
      for (const item of encoded[2]) {
        if (!Array.isArray(item) || item.length !== 2) fail("invalid_sparse_entry");
        const [i, value] = item;
        if (!isInt(i) || !(previous < i && i < n)) fail("invalid_sparse_index");
        out[i] = clone(value);
        previous = i;
      }
      return out;
    }
    return fail("invalid_vector");
  }

  const samePrefix = (a, b) => {
    const m = Math.min(a.length, b.length);
    for (let i = 0; i < m; i++) if (a[i] !== b[i]) return false;
    return true;
  };

  /* One part to an array of logical row fragments (one object per row id). */
  function decodeRows(document) {
    if (!isObject(document) || document.schema !== "pulse.columns.v1") fail("unknown_schema");
    const n = document.count;
    if (!isInt(n) || n < 1 || n > LIMITS.count) fail("invalid_count");
    const { metadata, groups, columns } = document;
    if (!Array.isArray(metadata) || !Array.isArray(groups) || !Array.isArray(columns)) fail("invalid_document");
    if (columns.length > LIMITS.columns || metadata.length > LIMITS.tables || groups.length > LIMITS.tables) fail("limits_exceeded");
    const rows = Array.from({ length: n }, () => ({}));
    const seen = [];
    const seenKeys = new Set();
    for (const column of columns) {
      if (!Array.isArray(column) || (column.length !== 4 && column.length !== 7)) fail("invalid_column");
      const [path, presence, kind] = column;
      if (!Array.isArray(path) || path.length === 0 || path.length > LIMITS.depth || path.some((k) => typeof k !== "string" || !k || FORBIDDEN.has(k)))
        fail("unsafe_path");
      const indices = presence === null ? Array.from({ length: n }, (_, i) => i) : presence;
      if (!Array.isArray(indices) || indices.length === 0 || indices.some((i, k) => !isInt(i) || i < 0 || i >= n || (k > 0 && i <= indices[k - 1])))
        fail("invalid_presence");
      const key = JSON.stringify(path);
      const present = new Set(indices);
      if (seenKeys.has(key)) fail("overlapping_path");
      for (const [other, used] of seen) {
        if (!samePrefix(path, other)) continue;
        for (const i of present) if (used.has(i)) fail("overlapping_path");
      }
      seenKeys.add(key);
      seen.push([path, present]);
      const m = indices.length;
      const values = expand(column[3], m);
      if (kind === "f" && column.length === 7) {
        const [mids, tids, states] = [4, 5, 6].map((k) => expand(column[k], m));
        for (let i = 0; i < m; i++) {
          const mid = mids[i], tid = tids[i];
          if (!isInt(mid) || mid < 0 || mid >= metadata.length || !isInt(tid) || tid < 0 || tid >= groups.length) fail("invalid_table_index");
          const meta = metadata[mid], group = groups[tid], state = states[i];
          if (!isObject(meta) || Object.keys(meta).some((k) => SPECIAL.has(k)) || !isObject(group) || Object.keys(group).some((k) => !TIMES.includes(k))
              || !isObject(state) || Object.keys(state).some((k) => k !== "status" && k !== "reason")) fail("invalid_field_tables");
          values[i] = { ...clone(meta), ...clone(group), ...clone(state), value: values[i] };
        }
      } else if (kind !== "j" || column.length !== 4) fail("invalid_column_kind");
      for (let i = 0; i < m; i++) {
        let node = rows[indices[i]];
        for (let s = 0; s < path.length - 1; s++) {
          if (!Object.prototype.hasOwnProperty.call(node, path[s])) node[path[s]] = {};
          node = node[path[s]];
          if (!isObject(node)) fail("overlapping_path");
        }
        node[path[path.length - 1]] = values[i];
      }
    }
    return rows;
  }

  function merge(target, source) {
    for (const [k, value] of Object.entries(source)) {
      if (!Object.prototype.hasOwnProperty.call(target, k)) target[k] = value;
      else if (isObject(target[k]) && isObject(value)) merge(target[k], value);
      else fail("duplicate_logical_path");
    }
  }

  function sameJson(a, b) {
    if (a === b) return true;
    if (typeof a !== "object" || typeof b !== "object" || a === null || b === null || Array.isArray(a) !== Array.isArray(b)) return false;
    const ka = Object.keys(a), kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && sameJson(a[k], b[k]));
  }

  /* parts: { core, ...optionally any of the other six }. Returns the logical snapshot built from the
     parts that are present, plus the list of parts used. Mixed generations are refused. */
  function assemble(parts) {
    if (!isObject(parts) || !parts.core) fail("core_required");
    for (const name of Object.keys(parts)) if (!PARTS.includes(name)) fail("unknown_part");
    const core = parts.core;
    const header = core.header;
    if (!isObject(header)) fail("header_missing");
    if (["mapping_version", "generation", "horizon", "generated_ms", "expires_ms"].some((k) => header[k] !== core[k])
        || !Array.isArray(header.universe) || header.universe.length !== core.count) fail("header_identity_mismatch");
    const rows = decodeRows(core);
    const inputs = {};
    const used = [];
    for (const name of PARTS) {
      const doc = parts[name];
      if (!doc) continue;
      if (!isObject(doc) || doc.schema !== "pulse.columns.v1" || doc.part !== name || IDENTITY.some((k) => doc[k] !== core[k])) fail("mixed_generation");
      if (name !== "core") {
        const fragments = decodeRows(doc);
        for (let i = 0; i < rows.length; i++) merge(rows[i], fragments[i]);
      }
      if (!isObject(doc.inputs)) fail("invalid_inputs");
      for (const [key, value] of Object.entries(doc.inputs)) {
        if (Object.prototype.hasOwnProperty.call(inputs, key) && !sameJson(inputs[key], value)) fail("input_version_mismatch");
        inputs[key] = clone(value);
      }
      used.push(name);
    }
    return { snapshot: { ...clone(header), inputs, rows }, parts: used };
  }

  /* Canonical JSON (sorted keys, no whitespace) identical to the producer's `raw()`; hashing is async. */
  function canonical(v) {
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    if (v !== null && typeof v === "object")
      return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
    return JSON.stringify(v);
  }
  async function digest(value, subtle) {
    const bytes = new TextEncoder().encode(canonical(value));
    const hash = new Uint8Array(await subtle.digest("SHA-256", bytes));
    return "sha256:" + Array.from(hash, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  return { PARTS, METRIC_PARTS, decodeRows, assemble, expand, canonical, digest };
});
