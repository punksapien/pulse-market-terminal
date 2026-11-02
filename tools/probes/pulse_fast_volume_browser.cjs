/* ADR-0118: local browser witness, synthetic volume and unavailable price. */
"use strict";
const assert=require("node:assert/strict");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
const {server}=require("../frontend/mock_pulse_server.cjs");
const core=require("../../docs/contracts/fixtures/wire/core.json");
(async()=>{
  await new Promise(r=>server.listen(0,"127.0.0.1",r));
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage();const errors=[];
    page.on("pageerror",e=>errors.push(e.message));
    let requests=0;
    await page.route("**/api/pulse?live=price",async route=>{
      requests++;const now=Date.now();
      await route.fulfill({contentType:"application/json",body:JSON.stringify({schema:"pulse.live_price.v1",
        mapping_version:core.mapping_version,generated_ms:now,expires_ms:now+15000,sequence:now,
        rows:[0,1,2].map(id=>({id,status:"unavailable",reason:"stale_price",price:null,source_ms:null,expires_ms:null,
          volume:{status:"available",reason:null,value:"123000000",unit:"USDT",source_ms:now,expires_ms:now+60000}}))})});
    });
    for(const host of ["https://coindcx.s3.amazonaws.com/**","https://fonts.googleapis.com/**","https://fonts.gstatic.com/**"])
      await page.route(host,r=>r.abort());
    await page.goto("http://127.0.0.1:"+server.address().port+"/?source=live",{waitUntil:"domcontentloaded"});
    await page.waitForFunction(()=>[...document.querySelectorAll(".pulse-row .volume")].some(e=>e.textContent==="Vol 123M"),null,{timeout:15000});
    assert.ok(requests>0);assert.deepEqual(errors,[]);
    console.log("Browser: live volume rendered despite unavailable price; no console errors");
  } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
