/* Local mock of the Preview reader for browser verification only (never deployed).
   Serves pulse-preview.html and the /api/pulse manifest, addressed compact parts and live funding from the
   committed golden wire fixtures, with every timestamp shifted to "now" and digests recomputed. */
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const W = require("../../frontend/src/pulse/pulse_wire.js");
const D = require("../../frontend/src/pulse/pulse_decimal.js");
const root = path.join(__dirname, "../..");
const dir = path.join(root, "docs/contracts/fixtures/wire");
const golden = Object.fromEntries(W.PARTS.map((p) => [p, JSON.parse(fs.readFileSync(path.join(dir, p + ".json"), "utf8"))]));
const control = { fail: 0, calls: [] };
let built = null;

function shift(value, delta) {
  if (Array.isArray(value)) return value.map((v) => shift(v, delta));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, shift(v, delta)]));
  return Number.isInteger(value) && value >= 1.5e12 ? value + delta : value;
}
async function build(horizon) {
  const now = Date.now();
  // Exact-minute ladder witnesses must remain minute-aligned after shifting fixtures.
  const delta = Math.floor(now/60000)*60000 - golden.core.generated_ms;
  const generation = "sha256:" + Buffer.from(String(now)).toString("hex").padEnd(64, "0").slice(0, 64);
  const parts = {};
  for (const [name, doc] of Object.entries(golden)) {
    const shifted = JSON.parse(JSON.stringify(shift(doc, delta)).split(golden.core.generation).join(generation));
    shifted.generation = generation;
    shifted.horizon = horizon;
    if (shifted.header) { shifted.header.generation = generation; shifted.header.horizon = horizon; }
    parts[name] = shifted;
  }
  const manifest = { schema: "pulse.manifest.v1", mapping_version: parts.core.mapping_version, generation, horizon,
    generated_ms: parts.core.generated_ms, expires_ms: parts.core.expires_ms, parts: {} };
  const bodies = new Map();
  for (const [name, doc] of Object.entries(parts)) {
    const h = await W.digest(doc, webcrypto.subtle);
    bodies.set(h, doc);
    manifest.parts[name] = { schema: "pulse.artifact.v1", logical_sha256: h, raw_bytes: JSON.stringify(doc).length, mapping_version: doc.mapping_version, generation, variants: {} };
  }
  const logical = W.assemble(parts).snapshot;
  return { builtAt: now, horizon, manifest, bodies, logical };
}
async function current(horizon) {
  if (!built || built.horizon !== horizon || Date.now() - built.builtAt > 25000) built = await build(horizon);
  return built;
}
function live(b) {
  const now = Date.now();
  return {
    schema: "pulse.live_funding.v1", mapping_version: b.manifest.mapping_version, sequence: now, generated_ms: now, expires_ms: now + 60000, inputs: {},
    rows: b.logical.rows.map((r) => {
      const fc = r.funding_change;
      const efr = D.toString(D.sub(D.parse(fc.settled_rate), D.parse("-0.00003")));
      return { id: r.id, status: "available", reason: null, h: fc.h, nft: fc.nft, fr: fc.settled_rate, efr,
        source_ms: now - 500, expires_ms: fc.nft, input_refs: [] };
    }),
  };
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  if (url.pathname === "/control") {
    Object.assign(control, JSON.parse(url.searchParams.get("set") || "{}"));
    res.end(JSON.stringify(control));
    return;
  }
  if (url.pathname === "/") {
    res.setHeader("content-type", "text/html; charset=utf-8");
    res.end(fs.readFileSync(path.join(root, "pulse-preview.html")));
    return;
  }
  if (url.pathname === "/api/pulse") {
    control.calls.push(url.search);
    if (control.fail) { res.statusCode = control.fail; res.setHeader("retry-after", "1"); res.end("{}"); return; }
    const send = (body, etag, cache) => {
      res.setHeader("content-type", "application/json"); res.setHeader("cache-control", cache); if (etag) res.setHeader("etag", etag);
      res.end(JSON.stringify(body));
    };
    if (url.searchParams.get("horizon")) {
      const b = await current(url.searchParams.get("horizon"));
      const tag = '"' + b.manifest.generation.slice(7, 23) + '"';
      if (req.headers["if-none-match"] === tag) { res.statusCode = 304; res.end(); return; }
      return send(b.manifest, tag, "public, max-age=10");
    }
    if (url.searchParams.get("artifact")) {
      const h = url.searchParams.get("artifact");
      const b = built;
      const body = b && b.bodies.get(h);
      if (!body) { res.statusCode = 404; res.end("{}"); return; }
      return send(body, null, "public, max-age=31536000, immutable");
    }
    if (url.searchParams.get("live") === "1") return send(live(await current(built ? built.horizon : "1H")), null, "public, max-age=3");
    res.statusCode = 400; res.end("{}"); return;
  }
  res.statusCode = 404; res.end();
});
module.exports = { server, control };
if (require.main === module) server.listen(Number(process.env.PORT || 8765), "127.0.0.1", () => console.log("mock reader on", server.address().port));
