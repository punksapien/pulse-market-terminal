/* ADR-0195/0209: device-appropriate navigation only, never order execution. */
(function(root,factory){
  const api=factory();
  if(typeof module==="object"&&module.exports)module.exports=api;else root.PulseNavigation=api;
})(typeof globalThis!=="undefined"?globalThis:this,function(){
  "use strict";
  const KEYS=["utm_source","utm_medium","utm_campaign","utm_term","utm_content"];
  const EVENTS=new Set(["view","metric_change","level_change","window_change","sort_change","filter_open","filter_apply","filter_reset","filter_remove","detail_open","detail_close","guide_open","retry","trade_click"]);
  const clean=v=>typeof v==="string"?v.replace(/[\u0000-\u001f\u007f]/g,"").trim().slice(0,128):"";
  function create(o={}){
    const now=o.now||Date.now, utms={};
    // ADR-0209: legacy device rule, including iPadOS's desktop-style user agent.
    const agent=o.navigator||{},ua=agent.userAgent||"";
    const mobile=/android|iphone|ipad|ipod|mobile/i.test(ua)||(agent.maxTouchPoints>1&&/Macintosh/.test(ua));
    try{const saved=JSON.parse(o.storage?.getItem("pulse_attribution_v1")||"{}");for(const k of KEYS)if(clean(saved[k]))utms[k]=clean(saved[k]);}catch(_){}
    try{const p=new URL(o.href).searchParams;for(const k of KEYS)if(clean(p.get(k)))utms[k]=clean(p.get(k));}catch(_){}
    try{o.storage?.setItem("pulse_attribution_v1",JSON.stringify(utms));}catch(_){}
    const attribution={utm_source:"dcxpulse",utm_medium:"screener",utm_campaign:"pulse_futures",...utms};
    let started=now(), count=0, last="", lastAt=-Infinity;
    function context(c={}){
      const out={};
      // Funding's internal 1H/4H/24H slots label Current/1D/7D, not time horizons.
      if(["funding","funding_change"].includes(c.metric))c={...c,window:({"1H":c.metric==="funding"?"Current":"Last","4H":"1D","24H":"7D"})[c.window]||c.window};
      const values={metric:["volatility","long_short","funding","funding_change","open_interest"],window:["1H","4H","24H","1D","7D","Current","Last"],sort:["design","symbol","volume","metric","price","change"],surface:["market_list","detail_cta"]};
      for(const [k,allowed]of Object.entries(values))if(allowed.includes(c[k]))out["pulse_"+k]=c[k];
      if(Number.isInteger(c.level)&&c.level>=0&&c.level<=4)out.pulse_level=c.level;
      if(typeof c.descending==="boolean")out.pulse_order=c.descending?"desc":"asc";
      if(typeof c.pair==="string"&&/^B-[A-Z0-9]{1,24}_USDT$/.test(c.pair))out.pulse_pair=c.pair;
      return out;
    }
    function track(event,c={}){
      if(!EVENTS.has(event))return false;
      const time=now(), payload={...attribution,...context(c)}, signature=event+JSON.stringify(payload);
      if(time-started>=60000||time<started){started=time;count=0;}
      if(count>=60||(signature===last&&time-lastAt<200))return false;
      count++;last=signature;lastAt=time;
      try{o.gtag?.("event","pulse_"+event,payload);}catch(_){}
      return true;
    }
    function destination(pair,surface,c={}){
      if(!/^B-[A-Z0-9]{1,24}_USDT$/.test(pair)||!["market_list","detail_cta"].includes(surface))return null;
      const url=new URL(mobile?"dcxgo://go.coindcx.com/futures":"https://coindcx.com/futures/"+encodeURIComponent(pair));
      if(mobile)url.searchParams.set("instrument",pair);
      for(const [k,v]of Object.entries({...attribution,...context({...c,surface})}))url.searchParams.set(k,v);
      return url.href;
    }
    function trade(pair,surface,c={}){
      const url=destination(pair,surface,c);if(!url)return false;
      track("trade_click",{...c,surface,pair});
      // Preserve the user's click gesture even if analytics is blocked or throws.
      if(mobile)o.navigate?.(url);else o.open?.(url,"_blank","noopener");return true;
    }
    return {track,destination,trade};
  }
  return {create};
});
