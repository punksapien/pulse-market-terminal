/* Finite read-only Preview sheet check; protection token is supplied only on stdin. */
"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
(async()=>{
  const {origin,token}=JSON.parse(fs.readFileSync(0,"utf8"));
  const url=new URL(origin);
  if(url.protocol!=="https:"||!url.hostname.endsWith(".vercel.app")||url.username||url.password||url.pathname!=="/")throw Error("scope");
  if(!token&&url.hostname!=='dcxpulse.vercel.app')throw Error('public_origin_scope');
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage({viewport:{width:390,height:1000}}),errors=[],history=[];
    page.on("pageerror",e=>errors.push(e.name));
    page.on("response",r=>{if(r.url().startsWith(origin+"/api/pulse?history="))history.push({status:r.status(),encoding:r.headers()["content-encoding"],bytes:Number(r.headers()["content-length"])});});
    await page.route("**/*",route=>new URL(route.request().url()).origin===origin?
      route.continue({headers:{...route.request().headers(),...(token?{"x-vercel-protection-bypass":token}:{})}}):route.abort());
    await page.goto(origin+"/pulse/?source=live",{waitUntil:"domcontentloaded"});
    await page.waitForFunction(()=>window.PulseDesign?.diagnostics().phase==="ready",null,{timeout:90000});
    assert.equal(history.length,0,"table load never downloads history");
    const checks=[];
    for(const metric of ["long_short","open_interest","funding","funding_change","volatility"]){
      await page.locator('[data-metric="'+metric+'"]').click();
      await page.waitForFunction(()=>window.PulseDesign.diagnostics().phase==="ready",null,{timeout:30000});
      await (token?page.locator('[data-pair="B-BTC_USDT"]'):page.locator('[data-pair]').first()).click();
      await page.waitForFunction(()=>window.PulseDesign.diagnostics().pendingParts.length===0,null,{timeout:20000});
      await page.waitForFunction(()=>!document.querySelector("#sheet-content").textContent.includes("Loading retained history"),null,{timeout:20000});
      const text=await page.locator("#sheet-content").innerText();
      const charts=await page.locator("#sheet-content .history-chart svg").count();
      if(metric==="funding"||metric==="funding_change")assert.ok(!text.includes("Awaiting verification"),metric+" verified settlement is visible");
      assert.match(await page.locator("#detail-risk-disclaimer").innerText(),/Pulse signals are statistical estimates, not a financial advice/);
      checks.push({metric,charts,insufficient:text.includes("Not enough verified history"),awaitingVerification:text.includes("Awaiting verification"),
        historyHeading:await page.locator("#sheet-content h3").allTextContents()});
      if(metric==="long_short"||metric==="open_interest")assert.equal(charts,1,metric+" retained chart");
      await page.locator("#close-sheet").click();
    }
    assert.deepEqual(errors,[]);
    assert.ok(history.every(r=>r.status===200&&r.encoding==="br"));
    console.log(JSON.stringify({status:"passed",checks,historyRequests:history,pageErrors:errors.length}));
  }finally{await browser.close();}
})().catch(e=>{console.error(JSON.stringify({status:"failed",error_type:e.name,message:String(e.message).slice(0,160)}));process.exitCode=1;});
