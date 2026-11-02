/* Finite hosted reader/navigation observation. Token stdin only; no writes or source calls. */
"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs");
function assess(samples,status){
  const observed=Array.isArray(samples)&&samples.length>0;
  const fields=observed&&samples.every(s=>s.rows===350&&s.missingPrice===0&&s.missingVolume===0);
  const phases=observed&&samples.every(s=>s.phase==='ready');
  const sorting=observed&&samples.every(s=>Number.isInteger(s.numericChanges)&&s.numericChanges>=2&&s.sortedDescending===true);
  const transport=status&&typeof status==='object'&&status['price:200']>0&&Object.entries(status).every(([key,count])=>{
    const match=/^(?:snapshot\/part|1|price):(\d{3})$/.exec(key);
    return !!match&&Number.isInteger(count)&&count>=0&&Number(match[1])<400;
  });
  return {availability_pass:!!(fields&&phases&&sorting&&transport),field_availability_pass:!!fields,
    phase_pass:!!phases,numeric_sort_pass:!!sorting,transport_pass:!!transport};
}
module.exports={assess};
if(require.main===module)(async()=>{
  const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
  const {origin,token,seconds=60}=JSON.parse(fs.readFileSync(0,"utf8"));
  const url=new URL(origin);
  if(url.protocol!=="https:"||!url.hostname.endsWith(".vercel.app")||url.pathname!=="/"||!Number.isInteger(seconds)||seconds<1||seconds>600)throw Error("scope");
  if(!token&&url.hostname!=="dcxpulse.vercel.app")throw Error("protection_required");
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage({viewport:{width:390,height:1000}}),errors=[],status={},volumes=new Set();
    page.on("pageerror",e=>errors.push(e.name));
    await page.route("**/*",route=>new URL(route.request().url()).origin===origin?
      route.continue({headers:{...route.request().headers(),...(token?{"x-vercel-protection-bypass":token}:{})}}):route.abort());
    page.on("response",async response=>{
      if(!response.url().includes("/api/pulse?"))return;
      const kind=new URL(response.url()).searchParams.get("live")||"snapshot/part",key=kind+":"+response.status();status[key]=(status[key]||0)+1;
      if(kind==="price"&&response.status()===200)try{
        const doc=await response.json();for(const r of doc.rows||[])if(r.volume?.status==="available"){
          volumes.add(r.volume.definition_version||"volume.rt.v1");
          assert.equal(r.volume.expires_ms-r.volume.source_ms,(r.volume.definition_version||"volume.rt.v1")==="volume.rt.v2"?300000:60000);
          if(r.status==="available")assert.equal(r.expires_ms-r.source_ms,60000);
        }
      }catch(e){errors.push("live_validation:"+e.name);}
    });
    const start=Date.now();
    await page.goto(origin+"/pulse/?source=live&utm_source=release_check&utm_medium=verification&utm_campaign=continuity",{waitUntil:"domcontentloaded",timeout:30000});
    await page.waitForFunction(()=>window.PulseDesign?.diagnostics().phase==="ready",null,{timeout:45000});
    const ready=Date.now()-start;
    assert.equal(await page.evaluate(()=>window.PulseDesign.diagnostics().totalRows),350);
    await page.evaluate(()=>{window.__links=[];window.__events=[];window.open=(...a)=>window.__links.push(a);window.gtag=(...a)=>window.__events.push(a);});
    const pair=await page.locator(".row-hit").first().getAttribute("data-pair");
    await page.locator(".row-trade").first().click();
    await page.locator(".row-hit").first().click();await page.locator("#pulse-trade").click();
    const links=await page.evaluate(()=>window.__links);
    assert.equal(links.length,2);
    for(const [i,args]of links.entries()){
      const u=new URL(args[0]);assert.equal(u.origin,"https://coindcx.com");assert.equal(u.pathname,"/futures/"+pair);
      assert.equal(u.searchParams.get("utm_source"),"release_check");assert.equal(u.searchParams.get("utm_medium"),"verification");
      assert.equal(u.searchParams.get("pulse_surface"),i?"detail_cta":"market_list");
    }
    await page.locator("#close-sheet").click();
    const tabs=[];
    for(const metric of ["long_short","funding","funding_change","open_interest","volatility"]){
      await page.locator('[data-metric="'+metric+'"]').click();
      await page.waitForFunction(()=>window.PulseDesign.diagnostics().phase==="ready",null,{timeout:25000});tabs.push(metric);
    }
    await page.locator('[data-sort="change"]').click();
    const samples=[];
    for(let i=0;i<Math.ceil(seconds/5);i++){
      samples.push(await page.evaluate(()=>{
        const d=window.PulseDesign.diagnostics(),rows=[...document.querySelectorAll(".pulse-row")].filter(r=>!r.hidden&&r.querySelector("[data-pair]"));
        const values=rows.map(r=>Number(r.querySelector(".change-pill").textContent.replace("%",""))).filter(Number.isFinite);
        return {at:Date.now(),generation:d.snapshot,phase:d.phase,rows:d.totalRows,
          missingVolume:rows.filter(r=>r.querySelector(".volume").dataset.availability!=="available").length,
          missingPrice:rows.filter(r=>r.querySelector(".row-price").dataset.availability!=="available").length,
          numericChanges:values.length,sortedDescending:values.every((v,j)=>!j||values[j-1]>=v),socket:d.socket?.status};
      }));
      await page.waitForTimeout(5000);
    }
    assert(samples.every(s=>s.sortedDescending),"displayed return order must remain numeric");
    assert.deepEqual(errors,[]);
    const assessment=assess(samples,status);
    if(!assessment.availability_pass)process.exitCode=1;
    console.log(JSON.stringify({status:assessment.availability_pass?"pass":"availability_failed",functional_checks_passed:true,
      ...assessment,origin,first_ready_ms:ready,rows:350,tabs,trade_surfaces:2,
      volume_versions:[...volumes],api_status_counts:status,generations:new Set(samples.map(s=>s.generation)).size,samples,page_errors:errors}));
  }finally{await browser.close();}
})().catch(e=>{console.error(JSON.stringify({status:"browser_check_failed",error_type:e.name,message:e.message.slice(0,160)}));process.exitCode=1;});
