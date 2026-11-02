/* Finite read-only Preview stream-to-DOM witness. Protection token is stdin-only. */
"use strict";
const fs=require("node:fs");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
(async()=>{
  const {origin,token,passiveProductionTicks=false}=JSON.parse(fs.readFileSync(0,"utf8"));
  const u=new URL(origin);
  if(u.protocol!=="https:"||!u.hostname.endsWith(".vercel.app")||u.pathname!=="/"||u.username||u.password||u.search||u.hash)throw Error("scope");
  if(!token&&u.hostname!=="dcxpulse.vercel.app")throw Error('public_origin_scope');
  if(typeof passiveProductionTicks!=='boolean'||passiveProductionTicks&&u.hostname==='dcxpulse.vercel.app')throw Error('mirror_scope');
  const browser=await chromium.launch({headless:true});
  const feeds=new Set();
  try{
    const page=await browser.newPage({viewport:{width:390,height:1100}}),errors=[];
    // Test transport only: consume the existing public fanout, never an exchange socket.
    // This proves the hosted consumer with real ticks, NOT the disabled Preview WS route.
    if(passiveProductionTicks){
      const WS=require('ws');
      await page.routeWebSocket('**/api/pulse_stream',route=>{
        const feed=new WS('wss://dcxpulse.vercel.app/api/pulse_stream',{origin:'https://dcxpulse.vercel.app',
          handshakeTimeout:10000,maxPayload:16384,perMessageDeflate:false});
        feeds.add(feed);
        feed.on('message',data=>{try{route.send(data.toString());}catch{feed.close();}});
        feed.on('error',()=>{});
        feed.on('close',()=>{feeds.delete(feed);try{route.close();}catch{}});
        route.onClose(()=>feed.close());
      });
    }
    // WebSocket upgrades do not pass through page.route; use the scoped bypass cookie.
    if(token){const seed=await fetch(origin+"/pulse/",{headers:{"x-vercel-protection-bypass":token,
      "x-vercel-set-bypass-cookie":"true"},redirect:"manual",signal:AbortSignal.timeout(15000)});
    if(![200,307].includes(seed.status))throw Error("protection");
    const cookies=seed.headers.getSetCookie().map(value=>{
      const first=value.split(";")[0],at=first.indexOf("=");
      return {name:first.slice(0,at),value:first.slice(at+1),domain:u.hostname,path:"/",secure:true,httpOnly:true};
    });
    if(!cookies.length)throw Error("protection_cookie");
    await page.context().addCookies(cookies);}
    page.on("pageerror",e=>errors.push(e.name));
    await page.route("**/*",r=>new URL(r.request().url()).origin===origin
      ?r.continue({headers:{...r.request().headers(),...(token?{"x-vercel-protection-bypass":token}:{})}}):r.abort());
    await page.addInitScript(()=>{
      const Native=window.WebSocket;
      window.__streamPaint={frames:0,samples:[],misses:0,returnsPresent:0,returnReasons:{},pairs:{}};
      let ladder;
      Object.defineProperty(window,"PulseLadder",{configurable:true,get:()=>ladder,set:value=>{
        ladder={...value,priceChange(...args){
          const result=value.priceChange(...args),key=result.reason||result.status;
          const reasons=window.__streamPaint.returnReasons;
          reasons[key]=(reasons[key]||0)+1;
          return result;
        }};
      }});
      window.WebSocket=class extends Native{
        constructor(...args){super(...args);this.addEventListener("message",e=>{
          let b;try{b=JSON.parse(e.data);}catch{return;}
          if(b.schema!=="pulse.ticks.v1"||!Array.isArray(b.rows))return;
          const stats=window.__streamPaint;stats.frames++;
          const candidates=[];
          for(const [pair,source,price] of b.rows){
            stats.pairs[pair]=true;
            const hit=document.querySelector('[data-pair="'+CSS.escape(pair)+'"]');
            const row=hit?.closest(".pulse-row"),rect=row?.getBoundingClientRect();
            if(row&&!row.hidden&&rect.bottom>0&&rect.top<innerHeight)candidates.push({row,source,price});
          }
          requestAnimationFrame(()=>requestAnimationFrame(()=>{
            for(const {row,source,price} of candidates){
              const actual=row.querySelector(".row-price")?.textContent;
              if(actual!==window.PulseModel?.price(Number(price))){stats.misses++;continue;}
              stats.samples.push(Date.now()-source);
              const change=row.querySelector(".change-pill")?.textContent;
              if(change?.endsWith('%')&&Number.isFinite(Number(change.slice(0,-1))))stats.returnsPresent++;
            }
          }));
        });}
      };
    });
    await page.goto(origin+"/pulse/?source=live",{waitUntil:"domcontentloaded",timeout:20000});
    await page.waitForFunction(()=>window.PulseDesign?.diagnostics().phase==="ready",null,{timeout:40000});
    await page.waitForTimeout(30000);
    const result=await page.evaluate(()=>{
      const s=window.__streamPaint,sorted=s.samples.slice().sort((a,b)=>a-b);
      const q=p=>sorted[Math.min(sorted.length-1,Math.floor(sorted.length*p))]??null;
      return {phase:PulseDesign.diagnostics().phase,socket:PulseDesign.diagnostics().socket,
        priceTransport:PulseDesign.diagnostics().price,pendingParts:PulseDesign.diagnostics().pendingParts,
        frames:s.frames,distinctPairs:Object.keys(s.pairs).length,matchedPricePaints:sorted.length,priceMisses:s.misses,
        returnPaintsPresent:s.returnsPresent,returnReasons:s.returnReasons,sourceToDomMs:{p50:q(.5),p95:q(.95),max:q(1)},
        visible:[...document.querySelectorAll(".pulse-row")].filter(r=>{const b=r.getBoundingClientRect();return !r.hidden&&b.bottom>0&&b.top<innerHeight;})
          .map(r=>({pair:r.querySelector("[data-pair]")?.dataset.pair,price:r.querySelector(".row-price")?.textContent,change:r.querySelector(".change-pill")?.textContent}))};
    });
    console.log(JSON.stringify({...result,at_ms:Date.now(),transport:passiveProductionTicks?'test-only passive public tick mirror':'native deployed socket',pageErrors:errors}));
  }finally{for(const feed of feeds)feed.terminate();await browser.close();}
})().catch(e=>{console.log(JSON.stringify({status:"failed",error_type:e.name}));process.exitCode=1;});
