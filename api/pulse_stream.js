/* ADR-0100. WebSocket reads do not start the collector. Git-triggered Preview only. */
const http = require("node:http");
const { WebSocketServer } = require("ws");
const { createBridge, authorised } = require("../pulse/stream/bridge.cjs");
const bridge = createBridge();
const server = http.createServer(async (req,res) => {
  res.setHeader("Cache-Control","no-store"); res.setHeader("Content-Type","application/json");
  try {
    bridge.configuration();
    if (req.method !== "POST") { res.writeHead(426); return res.end('{"reason":"websocket_required"}'); }
    if (!authorised(req.headers.authorization,process.env.PULSE_WORKER_SECRET)) { res.writeHead(401); return res.end('{"reason":"unauthorized"}'); }
    if ((req.headers["content-length"] && req.headers["content-length"] !== "0") || req.headers["transfer-encoding"] || req.url.includes("?")) {
      res.writeHead(400); return res.end('{"reason":"payload_refused"}');
    }
    const result = await bridge.collect();
    res.writeHead(result.status === "lease_busy" ? 409 : result.status === "unavailable" ? 503 : 200);
    res.end(JSON.stringify(result));
  } catch (error) {
    res.writeHead(process.env.VERCEL_ENV === "preview" ? 503 : 404);
    const safe=new Set(["stream_disabled","stream_scope","stream_redis_scope","stream_redis_unavailable","registry_missing","registry_expired_or_corrupt","registry_mapping","stream_memory_stop"]);
    res.end(JSON.stringify({reason:authorised(req.headers.authorization,process.env.PULSE_WORKER_SECRET)&&safe.has(error.message)?error.message:"stream_unavailable"}));
  }
});
const sockets = new WebSocketServer({noServer:true,maxPayload:128,perMessageDeflate:false});
server.on("upgrade", (req,socket,head) => {
  try {
    bridge.configuration();
    if (req.url.includes("?") || req.headers.origin !== "https://" + req.headers.host) throw Error("origin");
    sockets.handleUpgrade(req,socket,head,ws => { bridge.attach(ws).catch(() => ws.close(1013,"unavailable")); });
  } catch { socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); }
});
module.exports = server;
