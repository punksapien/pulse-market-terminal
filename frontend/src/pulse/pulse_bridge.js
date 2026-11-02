/* ADR-0100: one passive same-origin socket, no viewer-triggered source subscriptions. */
(function(root,factory){const api=factory();if(typeof module==="object"&&module.exports)module.exports=api;else root.PulseBridge=api;})(typeof globalThis!=="undefined"?globalThis:this,function(){
  "use strict";
  const PAIR=/^B-[A-Z0-9]{1,24}_USDT$/, PRICE=/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
  function create(o){
    for(const k of ["WebSocketImpl","now","setTimer","clearTimer","onTick","mapping"])if(typeof o[k]!=="function")throw TypeError("missing_"+k);
    if(typeof o.url!=="string"||!o.url.startsWith("wss://"))throw TypeError("insecure_socket_url");
    let ws=null,retry=null,watchdog=null,attempts=0,stopped=false,wanted=new Set();
    const state={status:"idle",ticks:0,connections:0};
    const status=s=>{state.status=s;if(o.onStatus)o.onStatus(s);};
    function close(){if(watchdog!==null)o.clearTimer(watchdog);watchdog=null;const old=ws;ws=null;if(old)old.close();state.connections=0;}
    function arm(){if(watchdog!==null)o.clearTimer(watchdog);watchdog=o.setTimer(()=>{if(ws)ws.close();},10000);}
    function connect(){
      if(stopped||!wanted.size||ws)return;
      status("connecting");
      let socket;try{socket=new o.WebSocketImpl(o.url);}catch{schedule();return;}
      ws=socket;state.connections=1;arm();
      socket.onmessage=e=>{
        if(ws!==socket||typeof e.data!=="string"||e.data.length>16384)return;
        let d;try{d=JSON.parse(e.data);}catch{return;}
        const now=o.now();
        if(d.schema!=="pulse.ticks.v1"||d.mapping_version!==o.mapping()||!Number.isSafeInteger(d.sent_ms)||d.sent_ms>now+1000||d.sent_ms<=now-2000||!Array.isArray(d.rows)||d.rows.length>50)return;
        if(!d.rows.every(r=>Array.isArray(r)&&r.length===3&&PAIR.test(r[0])&&Number.isSafeInteger(r[1])&&r[1]<=now+1000&&r[1]>=now-60000&&typeof r[2]==="string"&&r[2].length<=40&&PRICE.test(r[2])&&Number.isFinite(+r[2])&&+r[2]>0))return;
        arm();attempts=0;status("live");
        for(const [pair,t,p] of d.rows)if(wanted.has(pair)){state.ticks++;o.onTick(pair,{source_ms:t,price:p});}
      };
      socket.onerror=()=>{};
      socket.onclose=()=>{if(ws!==socket)return;close();if(!stopped&&wanted.size)schedule();};
    }
    function schedule(){status("delayed");if(retry!==null)return;const delay=Math.min(10000,500*2**Math.min(attempts++,5))*(0.5+(o.random||Math.random)());retry=o.setTimer(()=>{retry=null;connect();},delay);}
    return {state,setPairs(pairs){wanted=new Set(pairs.filter(p=>typeof p==="string"&&PAIR.test(p)).slice(0,350));if(!wanted.size){if(retry!==null)o.clearTimer(retry);retry=null;close();status("idle");}else connect();},stop(){stopped=true;this.setPairs([]);}};
  }
  return {create};
});
