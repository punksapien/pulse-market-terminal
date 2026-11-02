/* ADR-0193/0194/0195: synthetic fault/clock browser check; never a production freshness claim. */
"use strict";
const assert=require("node:assert/strict");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
const {server}=require("../frontend/mock_pulse_server.cjs");
const core=require("../../docs/contracts/fixtures/wire/core.json");
(async()=>{
  await new Promise(r=>server.listen(0,"127.0.0.1",r));
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage({viewport:{width:390,height:1000}}),errors=[];
    page.on("pageerror",e=>errors.push(e.message));
    await page.clock.install();
    await page.addInitScript(()=>{
      window.__trade=[];window.__analytics=[];
      window.open=(...args)=>window.__trade.push(args);
      window.gtag=(...args)=>window.__analytics.push(args);
      window.WebSocket=class{
        constructor(){window.__socket=this;}close(){this.onclose?.();}
      };
    });
    let acceptedAt, fail=false;
    await page.route("**/api/pulse?live=price",async route=>{
      if(fail)return route.fulfill({status:503,body:"{}"});
      acceptedAt=await page.evaluate(()=>Date.now());
      await route.fulfill({contentType:"application/json",body:JSON.stringify({schema:"pulse.live_price.v1",
        mapping_version:core.mapping_version,sequence:acceptedAt,generated_ms:acceptedAt,expires_ms:acceptedAt+15000,
        rows:[0,1,2].map(id=>({id,status:"unavailable",price:null,volume:{status:"available",reason:null,definition_version:"volume.rt.v2",value:"123000000",unit:"USDT",source_ms:acceptedAt,expires_ms:acceptedAt+300000}}))})});
    });
    await page.route("https://**/*",r=>r.abort());
    await page.goto("http://127.0.0.1:"+server.address().port+"/?source=live&utm_source=partner&utm_medium=email&utm_campaign=launch&utm_content=hero",{waitUntil:"domcontentloaded"});
    await page.waitForFunction(()=>document.querySelector(".pulse-row .volume")?.textContent==="Vol 123M");
    await page.waitForFunction(()=>!!window.__socket);
    const send=async prices=>page.evaluate(({prices,mapping})=>{
      const now=Date.now();window.__socket.onmessage({data:JSON.stringify({schema:"pulse.ticks.v1",mapping_version:mapping,sent_ms:now,
        rows:Object.entries(prices).map(([p,v])=>[p,now,v])})});
    },{prices,mapping:core.mapping_version});
    await send({"B-BTC_USDT":"101","B-ETH_USDT":"120"});
    await page.locator('[data-sort="change"]').click();await page.clock.runFor(350);
    const first=()=>page.locator(".pulse-row [data-pair]").first().getAttribute("data-pair");
    assert.equal(await first(),"B-ETH_USDT");
    await send({"B-BTC_USDT":"140"});await page.clock.runFor(350);
    assert.equal(await first(),"B-BTC_USDT","sort follows offscreen/live values without another click");
    await page.locator(".row-trade").first().click();
    assert.equal(await page.locator("#pulse-sheet[open]").count(),0,"row trade must not open the detail sheet");
    await page.locator(".row-hit").first().click();await page.locator("#pulse-trade").click();
    const links=await page.evaluate(()=>window.__trade);
    assert.equal(links.length,2);
    for(const [i,args]of links.entries()){
      const url=new URL(args[0]);assert.equal(url.origin,"https://coindcx.com");
      assert.equal(url.pathname,"/futures/B-BTC_USDT");assert.equal(url.searchParams.get("utm_source"),"partner");
      assert.equal(url.searchParams.get("utm_medium"),"email");assert.equal(url.searchParams.get("utm_content"),"hero");
      assert.equal(url.searchParams.get("pulse_surface"),i?"detail_cta":"market_list");
    }
    await page.locator("#close-sheet").click();
    fail=true;
    await page.clock.fastForward(16000);
    assert.equal(await page.locator(".pulse-row .volume").first().textContent(),"Vol 123M","expired transport envelope cannot revoke volume");
    // No price update for more than60s: volume remains, price is unavailable, not extended to5min.
    await page.clock.fastForward(65000);
    assert.equal(await page.locator(".pulse-row .volume").first().textContent(),"Vol 123M");
    assert.equal(await page.locator(".pulse-row .row-price").first().textContent(),"Updating");
    assert.equal(await page.locator(".pulse-row .row-price").first().getAttribute("data-availability"),"updating");
    assert.equal(await page.locator(".pulse-row .row-price").first().getAttribute("aria-busy"),"false");
    assert(await page.locator(".pulse-row .change-pill.unavailable").count()>0,"missing return must not look like a gain");
    await send({"B-BTC_USDT":"150"});await page.clock.runFor(350);
    const btc=page.locator('.pulse-row').filter({has:page.locator('[data-pair="B-BTC_USDT"]')});
    assert.equal(await btc.locator('.row-price').getAttribute('data-availability'),'available');
    assert.equal(await btc.locator('.row-price').textContent(),'150','a verified tick clears recovery presentation');
    await page.clock.fastForward(230000);
    await page.waitForFunction(()=>document.querySelector(".pulse-row .volume")?.textContent==="Vol Updating");
    const events=await page.evaluate(()=>window.__analytics.map(a=>a[1]));
    assert(events.includes("pulse_trade_click")&&events.includes("pulse_sort_change"));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:"pass",test:"synthetic_clock_fault_browser",volume_validity_ms:300000,
      price_validity_ms:60000,live_sort:"tick_reorders",trade_surfaces:2,utm_medium:"preserved",page_errors:errors}));
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
