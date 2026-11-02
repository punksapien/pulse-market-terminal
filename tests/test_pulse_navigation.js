"use strict";
const assert=require("node:assert/strict");
const N=require("../frontend/src/pulse/pulse_navigation.js");
let time=1000, events=[],opened=[],saved;
const storage={getItem:()=>saved,setItem:(k,v)=>{saved=v;}};
const n=N.create({href:"https://dcxpulse.vercel.app/?utm_source=partner&utm_medium=mail&utm_content=cta&secret=no",storage,now:()=>time,
  gtag:(...a)=>events.push(a),open:(...a)=>opened.push(a)});
assert(n.trade("B-BTC_USDT","market_list",{metric:"funding",window:"1D",sort:"change",level:2}));
const u=new URL(opened[0][0]);assert.equal(u.origin,"https://coindcx.com");assert.equal(u.pathname,"/futures/B-BTC_USDT");
assert.equal(u.searchParams.get("utm_source"),"partner");assert.equal(u.searchParams.get("utm_medium"),"mail");
assert.equal(u.searchParams.get("pulse_surface"),"market_list");assert.equal(u.searchParams.get("pulse_window"),"1D");
assert(!u.searchParams.has("secret"));assert.deepEqual(opened[0].slice(1),["_blank","noopener"]);
assert.equal(events[0][2].pulse_pair,"B-BTC_USDT");
assert.equal(new URL(n.destination("B-BTC_USDT","detail_cta",{metric:"funding",window:"4H"})).searchParams.get("pulse_window"),"1D");
assert.equal(new URL(n.destination("B-BTC_USDT","detail_cta",{metric:"funding_change",window:"1H"})).searchParams.get("pulse_window"),"Last");
for(const pair of ["B-BTC_USDC","BTCUSDT","B-X_USDT?x=y","https://evil.test","B-../_USDT",null])assert.equal(n.trade(pair,"market_list"),false);
assert.equal(n.destination("B-BTC_USDT","invalid"),null);
const reload=N.create({href:"https://dcxpulse.vercel.app/",storage});assert.equal(new URL(reload.destination("B-XAU_USDT","detail_cta")).searchParams.get("utm_source"),"partner");
const broken=N.create({href:"bad",storage:{getItem(){throw Error();},setItem(){throw Error();}},gtag(){throw Error();},open:(...a)=>opened.push(a)});
assert(broken.trade("B-1000PEPE_USDT","detail_cta"));
const before=events.length;for(let i=0;i<100;i++){time+=250;n.track("sort_change",{sort:i%2?"price":"change",search:"private"});}
assert(events.length-before<=59);assert(events.every(e=>!JSON.stringify(e).includes("private")));
time+=60000;assert(n.track("view"));assert(!n.track("unknown"));assert(!n.track("view"));
const devices=[
  ["Android",1,true],["Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",5,true],
  ["iPad",5,true],["iPod",1,true],["Macintosh",5,true],["Macintosh",1,false],
  ["Macintosh",0,false],["Windows NT 10.0",0,false],["Mobile WebView",1,true],["",0,false],
];
let mobileCases=0;
for(const [userAgent,maxTouchPoints,isMobile]of devices)for(const surface of ["market_list","detail_cta"]){
  const web=[],app=[],analytics=[];
  const nav=N.create({href:"https://dcxpulse.vercel.app/?utm_source=partner&utm_medium=mail&utm_campaign=a%26b&utm_term=BTC%2FUSDT&utm_content=%E2%9C%93&secret=never_forward",
    navigator:{userAgent,maxTouchPoints},open:(...a)=>web.push(a),navigate:u=>app.push(u),gtag:(...a)=>analytics.push(a)});
  assert(nav.trade("B-1000PEPE_USDT",surface,{metric:"funding",window:"4H"}));
  const url=new URL(isMobile?app[0]:web[0][0]);
  assert.equal(web.length,isMobile?0:1);assert.equal(app.length,isMobile?1:0);
  assert.equal(url.protocol,isMobile?"dcxgo:":"https:");
  assert.equal(url.hostname,isMobile?"go.coindcx.com":"coindcx.com");
  assert.equal(url.pathname,isMobile?"/futures":"/futures/B-1000PEPE_USDT");
  assert.equal(url.searchParams.get("instrument"),isMobile?"B-1000PEPE_USDT":null);
  for(const [key,value]of Object.entries({utm_source:"partner",utm_medium:"mail",utm_campaign:"a&b",utm_term:"BTC/USDT",utm_content:"✓",pulse_surface:surface,pulse_window:"1D"}))assert.equal(url.searchParams.get(key),value);
  assert(!url.searchParams.has("secret"));assert.equal(analytics[0][1],"pulse_trade_click");
  for(const pair of ["B-BTC_USDC","BTCUSDT","B-X_USDT?instrument=evil",null])assert.equal(nav.trade(pair,surface),false);
  assert.equal(nav.trade("B-BTC_USDT","invalid"),false);assert.equal(web.length+app.length,1);mobileCases++;
}
const appDespiteAnalytics=[];
assert(N.create({href:"bad",navigator:{userAgent:"Android"},storage:{getItem(){throw Error();},setItem(){throw Error();}},
  gtag(){throw Error();},navigate:u=>appDespiteAnalytics.push(u)}).trade("B-XAU_USDT","detail_cta"));
assert.equal(new URL(appDespiteAnalytics[0]).searchParams.get("instrument"),"B-XAU_USDT");
console.log("Navigation: desktop and "+mobileCases+" device/surface cases, retained UTMs, bounded events, injection and analytics failure tests pass");
