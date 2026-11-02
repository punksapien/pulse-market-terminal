/* Read-only hosted check. JSON stdin supplies origin and an in-memory protection token. */
"use strict";
const assert=require("node:assert/strict");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
const fs=require("node:fs");
(async()=>{
  const {origin,token}=JSON.parse(fs.readFileSync(0,"utf8"));
  const url=new URL(origin);
  if(url.protocol!=="https:"||!url.hostname.endsWith(".vercel.app")||url.username||url.password)throw new Error("scope");
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:390,height:800}});const errors=[];
    page.on("pageerror",e=>errors.push(e.name));
    await page.route("**/*",route=>{
      if(new URL(route.request().url()).origin===origin)
        return route.continue({headers:{...route.request().headers(),"x-vercel-protection-bypass":token}});
      return route.abort();
    });
    await page.goto(origin+"/pulse/?source=live",{waitUntil:"domcontentloaded"});
    await page.waitForFunction(()=>window.PulseDesign?.diagnostics().phase==="ready",null,{timeout:30000});
    const click=async name=>page.locator("button,[role=tab]").filter({hasText:new RegExp("^"+name+"$")}).first().click();
    const results=[];
    for(const metric of ["Funding","Funding Chg"]){
      await click(metric);
      for(const window of ["1D","7D"]){
        await click(window);
        await page.waitForFunction(()=>window.PulseDesign.diagnostics().phase==="ready",null,{timeout:20000});
        await page.waitForFunction(()=>[...document.querySelectorAll(".pulse-row .metric-icon")].some(e=>e.children.length>0),null,{timeout:10000});
        results.push({metric,window,visibleMeters:await page.locator(".pulse-row .metric-icon svg").count()});
      }
    }
    await click("L/S Ratio");
    for(const window of ["1H","4H","24H"]){
      await click(window);
      await page.waitForFunction(()=>window.PulseDesign.diagnostics().phase==="ready",null,{timeout:20000});
      const row=page.locator(".pulse-row").first();
      const value=await row.locator(".metric-number").innerText();
      assert.notEqual(value,"—");results.push({metric:"L/S",window,firstRatio:value});
    }
    assert.deepEqual(errors,[]);console.log(JSON.stringify({status:"passed",checks:results,pageErrors:errors.length}));
  } finally {await browser.close();}
})().catch(e=>{console.error(JSON.stringify({status:"failed",error_type:e.name}));process.exitCode=1;});
