/* ADR-0209: fixture-only device routing; never launches an external application. */
"use strict";
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {server}=require('../frontend/mock_pulse_server.cjs');
const profiles=[
  {name:'desktop',ua:'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',touch:0,width:1280,mobile:false},
  {name:'narrow_desktop',ua:'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',touch:0,width:390,mobile:false},
  {name:'android',ua:'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36',touch:5,width:390,mobile:true},
  {name:'iphone',ua:'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148',touch:5,width:390,mobile:true},
  {name:'ipados',ua:'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15',touch:5,width:820,mobile:true},
];
(async()=>{
 const input=process.argv.includes('--hosted')?JSON.parse(fs.readFileSync(0,'utf8')):{};
 if(input.origin){const u=new URL(input.origin);assert(u.protocol==='https:'&&u.hostname.endsWith('.vercel.app')&&u.pathname==='/'&&!u.search&&!u.hash&&!u.username&&!u.password&&!u.port);}
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});let checks=0;
 try{for(const profile of profiles){
  const context=await browser.newContext({userAgent:profile.ua,viewport:{width:profile.width,height:1000}});
  await context.addInitScript(({touch})=>{
   Object.defineProperty(navigator,'maxTouchPoints',{get:()=>touch});
   window.__outgoing=[];window.__navigationWired=false;
   // Replace only the platform transports, after the real helper is published.
   let api;Object.defineProperty(window,'PulseNavigation',{get:()=>api,set:value=>{
    api={...value,create:options=>{
     window.__navigationWired=typeof options.navigate==='function'&&options.navigator===navigator;
     return value.create({...options,open:(...args)=>window.__outgoing.push({type:'web',args}),
      navigate:url=>window.__outgoing.push({type:'app',args:[url]})});
    }};
   }});
   window.WebSocket=class{close(){}};
  },{touch:profile.touch});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.name));
  const local='http://127.0.0.1:'+server.address().port;
  await page.route('https://**/*',async route=>{
   const u=new URL(route.request().url());
   if(!input.origin||u.origin!==input.origin)return route.abort();
   if(u.pathname.startsWith('/api/')){
    const response=await fetch(local+u.pathname+u.search);
    return route.fulfill({status:response.status,headers:Object.fromEntries(response.headers),body:Buffer.from(await response.arrayBuffer())});
   }
   return route.continue({headers:{...route.request().headers(),...(input.token?{'x-vercel-protection-bypass':input.token}:{})}});
  });
  await page.goto((input.origin?input.origin+'/pulse/':local+'/')+'?source=live&utm_source=partner&utm_medium=mail&utm_campaign=app%26web&utm_term=BTC&utm_content=trade',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.pulse-row .row-trade:not([disabled])');
  const pair=await page.locator('.row-trade').first().getAttribute('data-trade');
  await page.locator('.row-trade').first().click();assert.equal(await page.locator('#pulse-sheet[open]').count(),0);checks++;
  await page.locator('.row-hit').first().click();await page.locator('#pulse-trade').click();
  const out=await page.evaluate(()=>({links:window.__outgoing,wired:window.__navigationWired}));
  assert(out.wired);assert.equal(out.links.length,2);checks++;
  for(const [i,link]of out.links.entries()){
   assert.equal(link.type,profile.mobile?'app':'web');const url=new URL(link.args[0]);
   assert.equal(url.protocol,profile.mobile?'dcxgo:':'https:');assert.equal(url.hostname,profile.mobile?'go.coindcx.com':'coindcx.com');
   assert.equal(url.pathname,profile.mobile?'/futures':'/futures/'+pair);
   assert.equal(url.searchParams.get('instrument'),profile.mobile?pair:null);
   for(const [key,value]of Object.entries({utm_source:'partner',utm_medium:'mail',utm_campaign:'app&web',utm_term:'BTC',utm_content:'trade',pulse_surface:i?'detail_cta':'market_list'}))assert.equal(url.searchParams.get(key),value);
   if(!profile.mobile)assert.deepEqual(link.args.slice(1),['_blank','noopener']);checks++;
  }
  assert.deepEqual(errors,[]);checks++;await context.close();
 }}finally{await browser.close();server.close();}
 console.log(JSON.stringify({test:'mobile_trade_routing',checks,passed:true,profiles:profiles.map(p=>p.name),fixture_only:true,hosted_ui:!!input.origin,external_apps_launched:0,production_api_calls:0}));
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
