/* Operator-only bounded hosted proof; secrets arrive on stdin and are never logged. */
const {WebSocket}=require("ws");
let input="";process.stdin.setEncoding("utf8");process.stdin.on("data",s=>input+=s);
process.stdin.on("end",async()=>{
  const cleanup=[];
  try {
    const c=JSON.parse(input);input="";
    const base=new URL(c.url);if(!base.hostname.endsWith(".vercel.app")||base.protocol!=="https:")throw Error("preview_url");
    const headers={"x-vercel-protection-bypass":c.bypass};
    const ages=[],hops=[],pairs=new Set();
    const ws=new WebSocket(base.origin.replace("https:","wss:")+"/api/pulse_stream",{headers:{...headers,Origin:base.origin},handshakeTimeout:12000,maxPayload:16384});
    cleanup.push(()=>ws.terminate());
    const opened=await new Promise(resolve=>{
      ws.on("open",()=>resolve(true));ws.on("unexpected-response",(_,r)=>{r.resume();console.log(JSON.stringify({websocket_status:r.statusCode}));resolve(false);});
      ws.on("error",e=>{console.log(JSON.stringify({websocket_error:e.code||e.name}));resolve(false);});
    });
    if(!opened){ws.terminate();return;}
    console.log(JSON.stringify({websocket_status:101}));
    ws.on("message",raw=>{
      let b;try{b=JSON.parse(raw.toString());}catch{return;}
      if(b.schema!=="pulse.ticks.v1")return;
      const t=Date.now();for(const r of b.rows){ages.push(t-r[1]);hops.push(t-b.sent_ms);pairs.add(r[0]);}
    });
    const post=async path=>{
      try {const r=await fetch(base.origin+path,{method:"POST",headers:{...headers,Authorization:"Bearer "+c.secret},signal:AbortSignal.timeout(70000)});
        const text=await r.text();let body;try{body=JSON.parse(text);}catch{body={reason:"non_json_response"};}
        return {path,status:r.status,body};
      }catch(e){return {path,error:e.name};}
    };
    let browser,page,paints=0,worker;
    if(c.browser){
      console.log(JSON.stringify(await post("/api/pulse_worker")));
      const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
      browser=await chromium.launch({headless:true});
      cleanup.push(()=>browser.close());
      const context=await browser.newContext({viewport:{width:375,height:710}});
      const seed=await fetch(base.origin+"/pulse/",{headers:{...headers,"x-vercel-set-bypass-cookie":"true"},redirect:"manual",signal:AbortSignal.timeout(15000)});
      const cookies=seed.headers.getSetCookie().map(v=>{const first=v.split(";")[0],at=first.indexOf("=");return {name:first.slice(0,at),value:first.slice(at+1),domain:base.hostname,path:"/",secure:true,httpOnly:true};});
      await context.addCookies(cookies);
      console.log(JSON.stringify({browser_protection_status:seed.status,cookies:cookies.length}));
      if(![200,307].includes(seed.status))throw Error("browser_protection_setup");
      page=await context.newPage();
      page.on("response",r=>{
        const u=new URL(r.url());
        if(u.origin===base.origin&&u.pathname==="/api/pulse")
          console.log(JSON.stringify({reader_status:r.status(),channel:u.searchParams.has("artifact")?"artifact":u.searchParams.get("horizon")||u.searchParams.get("live"),encoding:r.headers()["content-encoding"]||"identity"}));
      });
      await page.route("https://fonts.googleapis.com/**",r=>r.abort());
      await page.route("https://fonts.gstatic.com/**",r=>r.abort());
      await page.route("https://coindcx.s3.amazonaws.com/**",r=>r.abort());
      page.on("pageerror",e=>console.log(JSON.stringify({browser_error:e.name})));
      await page.goto(base.origin+"/pulse/?source=live",{waitUntil:"domcontentloaded",timeout:15000});
      try {await page.waitForFunction(()=>window.PulseDesign?.diagnostics().phase==="ready",null,{timeout:10000});}
      catch {console.log(JSON.stringify({browser_not_ready:await page.evaluate(()=>window.PulseDesign?.diagnostics()||{page_missing:true})}));throw Error("browser_not_ready");}
      await page.evaluate(()=>{
        window.__pricePaints=0;
        new MutationObserver(records=>{if(records.some(r=>(r.target.nodeType===1?r.target:r.target.parentElement)?.closest(".row-price,.change-pill")))window.__pricePaints++;})
          .observe(document.querySelector("#row-pool"),{subtree:true,childList:true,characterData:true});
      });
      worker=Promise.resolve({path:"/api/pulse_worker",status:"already_reported"});
    }else worker=post("/api/pulse_worker");
    const stream=post("/api/pulse_stream");
    await new Promise(r=>setTimeout(r,40000));ws.close();
    ages.sort((a,b)=>a-b);hops.sort((a,b)=>a-b);
    const summary=xs=>Object.fromEntries([["p50",.5],["p95",.95],["p99",.99]].map(([k,q])=>[k,xs[Math.min(xs.length-1,Math.floor(q*xs.length))]??null]));
    console.log(JSON.stringify({ticks:ages.length,pairs:pairs.size,event_to_operator_ms:summary(ages),bridge_to_operator_ms:summary(hops),clock_skew_corrected:false}));
    if(page){console.log(JSON.stringify({browser:await page.evaluate(()=>({diagnostics:PulseDesign.diagnostics(),price_paints:window.__pricePaints,rows:document.querySelectorAll(".pulse-row").length}))}));await browser.close();}
    console.log(JSON.stringify(await stream));console.log(JSON.stringify(await worker));
  } catch(e) {console.log(JSON.stringify({probe_failed:/^[a-z_]+$/.test(e.message)?e.message:e.name}));process.exitCode=1;}
  finally {for(const f of cleanup)await f();}
});
