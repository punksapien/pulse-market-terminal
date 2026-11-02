/* Read-only finite Preview observation; protection token exists only on stdin. */
"use strict";
const fs=require("node:fs");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
(async()=>{
  const {origin,token}=JSON.parse(fs.readFileSync(0,"utf8"));
  const url=new URL(origin);
  if(url.protocol!=="https:"||!url.hostname.endsWith(".vercel.app")||url.username||url.password||url.pathname!=="/")throw new Error("scope");
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage({viewport:{width:390,height:1100}});const errors=[];
    page.on("pageerror",error=>errors.push(error.name));
    await page.route("**/*",route=>new URL(route.request().url()).origin===origin
      ?route.continue({headers:{...route.request().headers(),"x-vercel-protection-bypass":token}}):route.abort());
    await page.goto(origin+"/pulse/?source=live",{waitUntil:"domcontentloaded"});
    await page.waitForFunction(()=>window.PulseDesign?.diagnostics().phase==="ready",null,{timeout:30000});
    await page.locator('[data-metric="long_short"]').click();
    await page.waitForTimeout(15000);
    const samples=[];
    for(let i=0;i<25;i++){
      samples.push(await page.evaluate(()=>{
        const d=window.PulseDesign.diagnostics();
        return {at:Date.now(),generation:d.snapshot,phase:d.phase,
          rows:[...document.querySelectorAll(".pulse-row")].filter(r=>{const b=r.getBoundingClientRect();return !r.hidden&&b.bottom>0&&b.top<innerHeight&&r.querySelector("[data-pair]");}).map(r=>({
            pair:r.querySelector("[data-pair]").dataset.pair,
            price:r.querySelector(".row-price").textContent,
            change:r.querySelector(".change-pill").textContent})),ticks:d.ticks};
      }));
      if(i<24)await page.waitForTimeout(5000);
    }
    const gaps=samples.flatMap(s=>s.rows.filter(r=>!r.price||r.price==="—"||!r.change||r.change==="—").map(r=>({at:s.at,...r})));
    console.log(JSON.stringify({status:errors.length?"page_errors":"observed",samples:samples.length,
      generations:new Set(samples.map(s=>s.generation)).size,rowObservations:samples.reduce((n,s)=>n+s.rows.length,0),
      gaps,pageErrors:errors,lastTickLatency:samples.at(-1).ticks}));
  }finally{await browser.close();}
})().catch(error=>{console.error(JSON.stringify({status:"failed",error_type:error.name}));process.exitCode=1;});
