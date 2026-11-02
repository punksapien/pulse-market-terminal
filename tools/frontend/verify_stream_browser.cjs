/* Real reader/real Brotli, mocked only at the streaming transport boundary. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
const assert=require("node:assert/strict");
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:375,height:710}});
    const errors=[], peers=[];let mapping;
    page.on("pageerror",e=>errors.push(e.message));
    page.on("response",async r=>{if(r.url().includes("horizon=")){const d=await r.json();mapping=d.mapping_version;}});
    await page.route("https://fonts.googleapis.com/**",r=>r.abort());
    await page.route("https://fonts.gstatic.com/**",r=>r.abort());
    await page.route("https://coindcx.s3.amazonaws.com/**",r=>r.abort());
    await page.routeWebSocket("**/api/pulse_stream",ws=>peers.push(ws));
    await page.goto((process.env.VERIFY_BASE||"http://127.0.0.1:8787")+"/?source=live");
    await page.waitForFunction(()=>PulseDesign.diagnostics().phase==="ready"&&PulseDesign.diagnostics().price?.applied>0);
    assert.equal(peers.length,1,"one shared browser socket, not per-pair sockets");
    const before=await page.evaluate(()=>PulseDesign.diagnostics());
    await page.evaluate(()=>{
      window.__pricePaints=0;
      new MutationObserver(records=>{if(records.some(r=>(r.target.nodeType===1?r.target:r.target.parentElement)?.closest(".row-price,.change-pill")))window.__pricePaints++;})
        .observe(document.querySelector("#row-pool"),{subtree:true,childList:true,characterData:true});
    });
    const began=Date.now();
    peers[0].send(JSON.stringify({schema:"pulse.ticks.v1",mapping_version:mapping,sent_ms:Date.now(),rows:[["B-BTC_USDT",Date.now()-100,"107.125"]]}));
    await page.waitForFunction(()=>Array.from(document.querySelectorAll(".row-price")).some(n=>n.textContent==="107.125"));
    const result=await page.evaluate(()=>{
      const row=Array.from(document.querySelectorAll(".pulse-row")).find(n=>n.querySelector(".row-price").textContent==="107.125");
      return {change:row.querySelector(".change-pill").textContent,diagnostics:PulseDesign.diagnostics()};
    });
    assert.match(result.change,/7\.12/);
    assert(await page.evaluate(()=>window.__pricePaints>0),"price observer must count textContent childList mutations");
    assert.equal(result.diagnostics.compileMs,before.compileMs,"tick must not rebuild the index");
    assert.equal(result.diagnostics.snapshot,before.snapshot,"tick must not rebuild the snapshot");
    assert(result.diagnostics.lastTickPaintMs<50);
    // A foreign mapping cannot alter either price or return.
    peers[0].send(JSON.stringify({schema:"pulse.ticks.v1",mapping_version:"sha256:"+"f".repeat(64),sent_ms:Date.now(),rows:[["B-BTC_USDT",Date.now(),"999"]]}));
    await page.evaluate(()=>new Promise(requestAnimationFrame));
    assert.equal(await page.locator(".row-price").filter({hasText:"999"}).count(),0);
    await page.evaluate(()=>{Object.defineProperty(document,"hidden",{configurable:true,get:()=>true});document.dispatchEvent(new Event("visibilitychange"));});
    assert.equal((await page.evaluate(()=>PulseDesign.diagnostics())).socket.status,"idle");
    await page.evaluate(()=>{Object.defineProperty(document,"hidden",{configurable:true,get:()=>false});document.dispatchEvent(new Event("visibilitychange"));});
    assert.equal(peers.length,2,"resume reconnects one socket");
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:"pass",transport:"mock",reader:"real_brotli",receipt_to_assertion_ms:Date.now()-began,paint_ms:result.diagnostics.lastTickPaintMs,price:"107.125",change:result.change,hidden_pause:true,index_unchanged:true}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
