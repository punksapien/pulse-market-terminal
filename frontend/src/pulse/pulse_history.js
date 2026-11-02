/* ADR-0126: lazy, bounded history buckets; historical source times never become live fields. */
(function(root,factory){
  if(typeof module==="object"&&module.exports)module.exports=factory();
  else root.PulseHistory=factory();
})(typeof globalThis!=="undefined"?globalThis:this,function(){
  "use strict";
  const HASH=/^sha256:[a-f0-9]{64}$/;
  const metrics=["open_interest","long_short","funding","funding_change","volatility"];
  function validate(doc,mapping,bucket,now){
    if(!doc||doc.schema!=="pulse.history.v1"||doc.mapping_version!==mapping||!HASH.test(mapping)||
        !HASH.test(doc.generation)||doc.bucket!==bucket||!Number.isSafeInteger(doc.generated_ms)||
        !Number.isSafeInteger(doc.expires_ms)||doc.generated_ms>now+5000||doc.expires_ms<=now||
        ![180000,720000].includes(doc.expires_ms-doc.generated_ms)||!Array.isArray(doc.rows)||doc.rows.length<1||doc.rows.length>10)
      throw Error("invalid_history_document");
    function check(s,unit){
      if(!s||s.unit!==unit||typeof s.label!=="string"||s.label.length>100||!Array.isArray(s.points)||s.points.length>512||
          !["available","unavailable"].includes(s.status)||(s.status==="available")!==(s.points.length>=2))throw Error("invalid_history_series");
      let previous=-1;
      for(const p of s.points){
        if(!Array.isArray(p)||p.length!==2||!Number.isSafeInteger(p[0])||p[0]<0||p[0]<=previous||p[0]>doc.generated_ms+5000||
            typeof p[1]!=="string"||p[1].length>128||! /^-?\d+(?:\.\d+)?(?:e[+-]?\d+)?$/i.test(p[1])||!Number.isFinite(Number(p[1]))||
            (unit==="quantity"&&Number(p[1])<0)||(unit==="ratio"&&Number(p[1])<=0))throw Error("invalid_history_point");
        previous=p[0];
      }
    }
    const ids=new Set();
    for(const row of doc.rows){
      if(!Number.isSafeInteger(row.id)||Math.floor(row.id/10)!==bucket||ids.has(row.id)||
          typeof row.pair_id!=="string"||!/^B-[A-Z0-9]{1,24}_USDT$/.test(row.pair_id)||!row.series)throw Error("invalid_history_row");
      ids.add(row.id);
      for(const metric of metrics){
        if(metric==="volatility")for(const h of ["1H","4H","24H"])check(row.series.volatility&&row.series.volatility[h],"fraction");
        else check(row.series[metric],metric==="open_interest"?"quantity":metric==="long_short"?"ratio":"fraction");
      }
    }
    return doc;
  }
  function create(o){
    const cache=new Map(),pending=new Map();
    function peek(id,mapping){
      const doc=cache.get(mapping+":"+Math.floor(id/10));
      return doc&&o.now()<doc.expires_ms?doc.rows.find(r=>r.id===id):null;
    }
    async function load(id,mapping){
      if(!Number.isSafeInteger(id)||id<0||id>349||!HASH.test(mapping))throw Error("invalid_history_identity");
      if(peek(id,mapping))return peek(id,mapping);
      const bucket=Math.floor(id/10),key=mapping+":"+bucket;
      if(pending.has(key))return pending.get(key).then(()=>peek(id,mapping));
      const request=(async()=>{
        const response=await o.fetch(o.url(id),{credentials:"same-origin",headers:{Accept:"application/json"},cache:"no-cache"});
        if(response.status!==200)throw Error("history_http_"+response.status);
        const text=await response.text();
        if(text.length>1048576)throw Error("history_size");
        const doc=validate(JSON.parse(text),mapping,bucket,o.now());
        if(o.digest){const original={...doc};delete original.generation;
          if(await o.digest(original)!==doc.generation)throw Error("history_digest");}
        cache.set(key,doc);
        for(const k of cache.keys())if(!k.startsWith(mapping+":"))cache.delete(k);
        return doc;
      })();
      pending.set(key,request);
      try{await request;return peek(id,mapping);}finally{pending.delete(key);}
    }
    return {peek,load};
  }
  return {create,validate};
});
