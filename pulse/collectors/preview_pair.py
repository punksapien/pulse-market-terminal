"""ADR-0097 resumable real pair collection; all HTTP uses the admitted dispatcher."""
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import hashlib
import json
import time
import os

from .binance_oi import parse_current
from .oi_history import history_spec,parse_history
from .binance_ls import history_spec as ls_spec,parse_history as parse_ls
from .funding_inputs import inputs,history
from .price_references import requests_for,parse_opens
from pulse.metrics.funding import settled_report
from pulse.metrics.price_change import PriceTick
from pulse.metrics.price_precision import prepare_price
from pulse.store.metric_capsule import MetricCapsule
from pulse.store.capsule_model import CapsuleSpec
from pulse.store.funding_cache import encode
from pulse.wire.legacy_rows import evaluate


def exact_json(value):
    if isinstance(value,Decimal): return str(value)
    if hasattr(value,"item"): return exact_json(value.item())
    if isinstance(value,dict): return {k:exact_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [exact_json(v) for v in value]
    return value


def stores(client,run_id,meta):
    pair,symbol=meta["pair_id"],meta["symbol"]
    from pulse.store.production_scope import release_enabled
    # ADR-0174: conditional approval activated only after a fenced staging migration.
    compact=(release_enabled(os.environ)
             and os.environ.get("PULSE_OWN_OI_300_APPROVED")=="1")
    specs=(CapsuleSpec(pair,symbol,"oi","quantity",60000,300 if compact else 1536,900000,900000,True),
           history_spec(pair,symbol),ls_spec(pair,symbol,"global"),ls_spec(pair,symbol,"top"))
    return tuple(MetricCapsule(client,run_id,s,approved_plan_fallback=True) for s in specs)


def save_points(store,points,*,require_existing=False):
    if type(require_existing) is not bool:raise ValueError("invalid_existing_ring_policy")
    if not points: return
    store.acquire()
    try:
        if require_existing:store.initialise(require_existing=True)
        else:store.initialise()
        for i in range(0,len(points),16): store.write_many(points[i:i+16])
    finally:
        store.release()


def legacy_oi_rows(source,symbol,prior,*,reuse=False):
    """ADR-0134: exact response reuse, only within its native hourly bucket."""
    def validate(rows):
        validation_now=source.clock()  # ADR-0150: one trusted clock, never an RPC per row.
        if (not isinstance(rows,list) or len(rows)>25 or any(type(r) is not dict
                or r.get("symbol")!=symbol for r in rows)):
            raise ValueError("legacy_oi_history_identity")
        if any(type(r.get("timestamp")) is not int or r["timestamp"]<0
               or r["timestamp"]>validation_now+5000 or r["timestamp"]%3600000 for r in rows):
            raise ValueError("legacy_oi_history_time")
    cached=prior.get("legacy_oi_history") if reuse else None
    if cached is not None:validate(cached)
    bucket=source.clock()//3600000*3600000
    if cached and max(r["timestamp"] for r in cached)==bucket:return cached
    rows=source.request("/futures/data/openInterestHist",{"symbol":symbol,"period":"1h","limit":25})
    validate(rows)
    if reuse:prior["legacy_oi_history"]=exact_json(rows)
    return rows


def collect_legacy(source,pair,symbol,rt,prior,*,reuse_oi=False):
    """Hourly v1 inputs (candles and 1H OI history) for the unchanged legacy scorer and signal engine."""
    from pulse.wire.rows import WINDOWS
    import pandas as pd
    candles={};witness={};source_times=[]
    for resolution,days in (("60",90),("240",120),("1D",365)):
        at=datetime.fromtimestamp(source.clock()/1000,timezone.utc)
        start=int((at-timedelta(days=days)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        payload=source.request("/market_data/candlesticks",{"pair":pair,"from":start,
            "to":int(at.timestamp()),"resolution":resolution,"pcode":"f"},host="https://public.coindcx.com")
        source_times.append(source.last_source_ms)
        rows=payload.get("data") if type(payload) is dict else None
        validation_now=source.clock()  # ADR-0150: bound validation RPCs independently of candle count.
        if not isinstance(rows,list) or not 40<=len(rows)<=10000: raise ValueError("legacy_candle_response_bound")
        if any(type(r) is not dict or type(r.get("time")) is not int or r["time"]>validation_now+5000 for r in rows):
            raise ValueError("legacy_candle_time")
        stamps=[r["time"] for r in rows]
        if len(stamps)!=len(set(stamps)): raise ValueError("legacy_candle_duplicate")
        frame=pd.DataFrame(rows)
        frame["timestamp"]=pd.to_datetime(frame["time"],unit="ms",utc=True)
        for col in ("open","high","low","close","volume"):
            frame[col]=pd.to_numeric(frame[col].astype(str),errors="raise")
        candles[resolution]=frame.sort_values("timestamp")
        witness[resolution]=hashlib.sha256(json.dumps(exact_json(payload),sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    legacy_oi=legacy_oi_rows(source,symbol,prior,reuse=reuse_oi)
    if any(t is None for t in source_times): raise ValueError("candle_provider_time_missing")
    outputs=evaluate(pair,candles,rt,legacy_oi)
    keys=("volatility_score","regime","vol_direction","rsi_14","vol_momentum","vol_velocity_label",
          "trade_signal","vol_outlook","vol_outlook_confidence","vol_outlook_reasons")
    prior["legacy"]={h:{key:exact_json(outputs[h][key]) for key in keys} for h in WINDOWS}
    prior["legacy_source_ms"]=min(source_times)
    retained=dict(prior.get("volatility_history",{}))
    for h in WINDOWS:
        stamp=prior["legacy_source_ms"]
        points=[p for p in retained.get(h,[]) if p[0]<stamp]
        points.append([stamp,str(outputs[h]["volatility_score"])])
        retained[h]=points[-96:]
    prior["volatility_history"]=retained
    prior["candle_hashes"]=witness
    return prior


LEGACY_REFRESH_MS=720000    # ADR-0109: refresh at12min, reserving3min inside the existing15min model cadence


def refresh_legacy(source,pair,symbol,rt,prior,now,*,reuse_oi=False):
    """Scheduled v1 inputs. A pair without enough history (new listing, thin market) keeps its legacy-derived
    fields unavailable instead of blocking the universe; retried on the next refresh, never a substitute value."""
    if now-prior.get("legacy_source_ms",0)<LEGACY_REFRESH_MS: return prior
    try:
        collect_legacy(source,pair,symbol,rt,prior,reuse_oi=reuse_oi)
        prior.pop("legacy_error",None)
    except ValueError as exc:
        prior.pop("legacy",None)
        prior["legacy_error"]=str(exc)[:80]
        prior["legacy_source_ms"]=now
    return prior


def collect_hot(client,run_id,meta,source,prior):
    """ADR-0112: current OI + instrument only; independent context timestamps do not advance."""
    prior=collect_oi(client,run_id,meta,source,prior)
    return collect_instrument(meta,source,prior)


def collect_oi(client,run_id,meta,source,prior):
    """ADR-0133: OI refresh never forces an unrelated metadata request."""
    prior=dict(prior or {})
    own=stores(client,run_id,meta)[0]
    raw=source.request("/fapi/v1/openInterest",{"symbol":meta["symbol"]},weight=1)
    point=parse_current(own.spec,raw,now_ms=source.clock())
    save_points(own,[point])
    prior.update(last_hot_ms=source.clock(),oi_source_ms=point.source_ms)
    return prior


def collect_instrument(meta,source,prior):
    """ADR-0133: retain the provider clock, never restamp cached metadata."""
    from .funding_inputs import hours
    prior=dict(prior or {})
    raw=source.request("/exchange/v1/derivatives/futures/data/instrument",
        {"pair":meta["pair_id"],"margin_currency_short_name":"USDT"},host="https://api.coindcx.com")
    instrument=raw.get("instrument",raw)
    if (instrument.get("pair")!=meta["pair_id"] or instrument.get("quote_currency_short_name")!="USDT"
            or instrument.get("settle_currency_short_name")!="USDT"
            or hours(instrument.get("funding_frequency"))!=meta["funding_hours"]):
        raise ValueError("hot_instrument_identity")
    stamp=source.last_source_ms;at=source.clock()
    if type(stamp) is not int or stamp>at+5000 or at-stamp>=120000:
        raise ValueError("instrument_provider_time_missing_or_stale")
    prior.update(instrument=exact_json(instrument),instrument_ms=source.last_source_ms)
    return prior


def ls_repair_limit(prior,series,now_ms):
    """ADR-0133: refill the elapsed gap in one already-admitted bounded request."""
    stamp=prior.get(series+"_source_ms",prior.get(series+"_refreshed_ms",0))
    if type(stamp) is not int or stamp<0 or stamp>now_ms+5000:raise ValueError("ls_repair_clock")
    return 320 if not stamp else min(320,max(3,(max(0,now_ms-stamp)+299999)//300000+2))


def bootstrap_history_limit(store,prior,series,now_ms):
    """ADR-0145: reuse history only after proving the retained boundary exists."""
    stamp=prior.get(series+"_source_ms",0)
    if type(stamp) is not int or stamp<0 or stamp>now_ms+5000:
        raise ValueError("bootstrap_history_clock")
    if not stamp:return 320
    boundary=store.baseline(stamp,0)
    if boundary.get("status")!="available" or boundary.get("source_ms")!=stamp:
        return 320
    return ls_repair_limit(prior,series,now_ms)


def collect_ls_tip(client,run_id,meta,source,prior,*,periods=False,repair=False):
    """ADR-0113 current global ratio, independent from history/legacy completion."""
    prior=dict(prior or {});store=stores(client,run_id,meta)[2]
    limit=ls_repair_limit(prior,"global",source.clock()) if repair else 3
    payload=source.request("/futures/data/globalLongShortAccountRatio",{"symbol":meta["symbol"],"period":"5m","limit":limit})
    points=parse_ls(payload,store.spec,now_ms=source.clock())
    if not points:raise ValueError("ls_tip_empty")
    if source.clock()-max(point.source_ms for point in points)>=900000:raise ValueError("ls_tip_expired")
    save_points(store,points)
    prior["global_tip_refreshed_ms"]=source.clock()
    prior["global_source_ms"]=max(point.source_ms for point in points)
    if periods:
        for window in ("1H","4H","24H"):
            prior=collect_ls_period(client,run_id,meta,source,prior,window=window)
        prior["ls_windows_refreshed_ms"]=source.clock()
    return prior


def collect_ls_period(client,run_id,meta,source,prior,*,window):
    """ADR-0133: one independently due native-period observation."""
    choices={"1H":("1h",3600000),"4H":("4h",14400000),"24H":("1d",86400000)}
    if window not in choices:raise ValueError("ls_period_window")
    period,step=choices[window];prior=dict(prior or {})
    store=stores(client,run_id,meta)[2]
    payload=source.request("/futures/data/globalLongShortAccountRatio",{"symbol":meta["symbol"],"period":period,"limit":2})
    now=source.clock();points=parse_ls(payload,store.spec,now_ms=now)
    if not points:raise ValueError("ls_period_empty")
    point=max(points,key=lambda p:p.source_ms)
    if point.source_ms%step or now>=point.source_ms+step+900000:raise ValueError("ls_period_clock")
    retained=dict(prior.get("ls_periods",{}))
    retained[window]={"source_ms":point.source_ms,"expires_ms":point.source_ms+step+900000,"value":point.sources[0]}
    prior["ls_periods"]=retained
    return prior


def collect_detail(client,run_id,meta,source,prior,*,top_refresh_ms=600000,repair=False,maintenance_only=False):
    """ADR-0126: bounded detail maintenance before slow candle context."""
    prior=dict(prior or {})
    if top_refresh_ms not in (600000,900000):raise ValueError("top_refresh_policy")
    own,cold,_,top=stores(client,run_id,meta)
    now=source.clock()
    if now-prior.get("top_refreshed_ms",0)>=top_refresh_ms:
        payload=source.request("/futures/data/topLongShortAccountRatio",
            {"symbol":meta["symbol"],"period":"5m","limit":ls_repair_limit(prior,"top",now) if repair else 3})
        points=parse_ls(payload,top.spec,now_ms=source.clock())
        if not points or source.clock()-max(p.source_ms for p in points)>=2100000:
            raise ValueError("ls_top_tip_expired")
        if maintenance_only:save_points(top,points,require_existing=True)
        else:save_points(top,points)
        prior["top_refreshed_ms"]=source.clock()
        prior["top_source_ms"]=max(point.source_ms for point in points)
    # ADR-0188: approved bounded OI repair may overwrite existing slots during a soft pause.
    current=own.read()
    missing=False
    if current["status"]=="available":
        from pulse.metrics.open_interest import WINDOW_MS
        for window in WINDOW_MS.values():
            target=current["source_ms"]-window
            if target<0:continue
            a=own.baseline(target,60000)
            if a["status"]=="available":continue
            b=cold.baseline(target,300000)
            # Never replace conflicting or ambiguous data with a different baseline.
            if b["status"]=="available" or b.get("reason")!="baseline_missing":continue
            missing=True
    if missing:
        # ADR-0166: one bounded full cold window repairs all three baseline holes.
        payload=source.request("/futures/data/openInterestHist",{
            "symbol":meta["symbol"],"period":"5m","limit":320})
        points=parse_history(payload,cold.spec,now_ms=source.clock())
        if maintenance_only:save_points(cold,points,require_existing=True)
        else:save_points(cold,points)
        if points:prior["cold_source_ms"]=max(p.source_ms for p in points)
    prior["detail_refreshed_ms"]=source.clock()
    return prior


def collect_funding(client,run_id,meta,source,bulk,info,premium,prior,*,settlement_only=False):
    """ADR-0115: a verified settlement report is independent of candle progress."""
    prior=dict(prior or {});pair,symbol=meta["pair_id"],meta["symbol"]
    from pulse.metrics.funding import FRESH
    at=source.clock();stamp=prior.get("instrument_ms")
    if type(stamp) is int and at-stamp>=FRESH-10000:
        # ADR-0165: refresh stale verification dependency, not the cached funding timestamp.
        prior=collect_instrument(meta,source,prior)
    metadata,_=inputs(pair,symbol,prior["instrument"],bulk,info["data"],premium["data"][symbol],
        instrument_ms=prior["instrument_ms"],info_ms=info["source_ms"],mapping=meta["mapping_version"])
    if metadata.coindcx_hours!=meta["funding_hours"]:raise ValueError("registry_interval_changed")
    now=source.clock()
    if settlement_only and prior.get("funding_report") and prior.get("funding_history"):
        from pulse.store.funding_cache import decode
        from pulse.metrics.funding import HOUR,settled_view
        report=decode(prior["funding_report"].encode())
        identity=("pair_id","mapping_version","coindcx_hours","binance_hours","next_funding_ms","settled_rate")
        if (report.latest is not None and all(getattr(report.metadata,k)==getattr(metadata,k) for k in identity)
                and now<report.latest.time_ms+metadata.coindcx_hours*HOUR
                and settled_view(report,now_ms=now).last.status=="available"):
            return prior
    if (prior.get("funding_report") and prior.get("nft")==metadata.next_funding_ms
            and prior.get("funding_history") and now-prior.get("funding_refreshed_ms",0)<900000):return prior
    end=source.clock();start=end-8*86400000
    payload=source.request("/fapi/v1/fundingRate",{"symbol":symbol,"startTime":start,"endTime":end,"limit":512},family=True)
    events=history(payload,pair,symbol,start_ms=start,end_ms=end)
    report=settled_report(events,metadata,now_ms=source.clock())
    prior["funding_attempt_ms"]=source.clock();prior["funding_attempt_nft"]=metadata.next_funding_ms
    if report.verified_at_ms is not None and report.last.status=="available":
        prior["funding_report"]=encode(report).payload.decode()
        prior["funding_refreshed_ms"]=source.clock();prior["nft"]=metadata.next_funding_ms
        prior.pop("funding_refresh_reason",None)
        from pulse.metrics.funding import HOUR,JITTER
        regular=sorted({e.time_ms:e for e in events if e.rate_type=="Regular"}.values(),key=lambda e:e.time_ms)
        # Retain only the consecutive suffix verified by the current interval.
        suffix=[regular[-1]]
        for event in reversed(regular[:-1]):
            if abs(suffix[-1].time_ms-event.time_ms-metadata.coindcx_hours*HOUR)>JITTER:break
            suffix.append(event)
        count=(168//metadata.coindcx_hours if report.cumulative_7d.status=="available" else
               24//metadata.coindcx_hours if report.cumulative_1d.status=="available" else 2)
        prior["funding_history"]=[[e.time_ms,str(e.rate)] for e in list(reversed(suffix))[-max(2,count):]]
    else:prior["funding_refresh_reason"]=report.last.reason or "settlement_unverified"
    return prior


def collect_references(meta,source,bulk,prior,*,force_prefetch=False):
    """ADR-0122 independent bounded candle repair, without completing slow context."""
    prior=dict(prior or {});pair,symbol=meta["pair_id"],meta["symbol"]
    now=source.clock()
    if type(force_prefetch) is not bool:raise ValueError("reference_prefetch_policy")
    if not force_prefetch and prior.get("opens") and now<prior.get("references_prefetch_until_ms",0)-120000:return prior
    rt=bulk["prices"].get(pair)
    if not isinstance(rt,dict) or rt.get("mkt")!=symbol:raise ValueError("pair_ticker_identity")
    price=prepare_price(str(rt.get("ls")))
    if not price.available:raise ValueError("pair_price_precision")
    tick=PriceTick(pair,rt["btST"],price.value)
    plans=requests_for(tick,now_ms=now,prefetch_minutes=26)
    needed={stamp for plan in plans for stamp in plan["needed"]}
    opens={stamp:value for stamp,value in prior.get("opens",[]) if stamp in needed}
    for plan in plans:
        params={k:v for k,v in plan.items() if k!="needed"}
        payload=source.request("/market_data/candlesticks",params,host="https://public.coindcx.com")
        for stamp,candle in parse_opens(payload,plan).items():
            if stamp in opens and Decimal(opens[stamp])!=candle.price:raise ValueError("reference_conflicting_duplicate")
            opens[stamp]=str(candle.price)
    prior["opens"]=[[stamp,value] for stamp,value in sorted(opens.items())]
    prior["references_refreshed_ms"]=source.clock()
    complete=all(stamp in opens for stamp in needed)
    prior["references_prefetch_until_ms"]=min(2**53-1,now//60000*60000+(22 if complete else 1)*60000)
    return prior


def collect(client,run_id,meta,source,bulk,info,premium,prior,*,funding_already_checked=False,all_mode=False,checkpoint=None):
    """Each completed pair is durable; failure never marks a partial pair complete."""
    from pulse.wire.rows import WINDOWS
    pair,symbol=meta["pair_id"],meta["symbol"]
    prior=dict(prior or {});own,cold,global_,top=stores(client,run_id,meta)
    if checkpoint is not None and (not all_mode or not callable(checkpoint)):
        raise ValueError("bootstrap_checkpoint_policy")
    def progress():
        if checkpoint is not None:
            partial=dict(prior);partial.pop("last_collected_ms",None)
            checkpoint(partial)
    hot=0<=source.clock()-prior.get("last_hot_ms",0)<45000 and "instrument" in prior
    if not hot:
        raw=source.request("/fapi/v1/openInterest",{"symbol":symbol},weight=1)
        point=parse_current(own.spec,raw,now_ms=source.clock());save_points(own,[point])
        prior.update(last_hot_ms=point.source_ms,oi_source_ms=point.source_ms)
        progress()
    now=source.clock()
    for name,store in (("cold",cold),("global",global_),("top",top)):
        due=prior.get(name+"_refreshed_ms",0)+ (900000 if name=="top" else 300000)
        if due>now: continue
        first=not prior.get(name+"_refreshed_ms")
        limit=(bootstrap_history_limit(store,prior,name,now) if all_mode else
            320 if first or now-prior[name+"_refreshed_ms"]>600000 else 3)
        path=("/futures/data/openInterestHist" if name=="cold" else
              "/futures/data/"+("global" if name=="global" else "top")+"LongShortAccountRatio")
        payload=source.request(path,{"symbol":symbol,"period":"5m","limit":limit})
        points=(parse_history(payload,store.spec,now_ms=source.clock()) if name=="cold" else
                parse_ls(payload,store.spec,now_ms=source.clock()))
        save_points(store,points)
        prior[name+"_refreshed_ms"]=source.clock()
        if points:prior[name+"_source_ms"]=max(point.source_ms for point in points)
        if name=="global":prior["global_tip_refreshed_ms"]=source.clock()
        progress()
    if hot:
        instrument_ms=prior["instrument_ms"];instrument=prior["instrument"]
    else:
        raw_instrument=source.request("/exchange/v1/derivatives/futures/data/instrument",
            {"pair":pair,"margin_currency_short_name":"USDT"},host="https://api.coindcx.com")
        instrument_ms=source.last_source_ms
        instrument=raw_instrument.get("instrument",raw_instrument)
    if instrument_ms is None: raise ValueError("instrument_provider_time_missing")
    metadata,estimate=inputs(pair,symbol,instrument,bulk,info["data"],premium["data"][symbol],
        instrument_ms=instrument_ms,info_ms=info["source_ms"],mapping=meta["mapping_version"])
    if metadata.coindcx_hours!=meta["funding_hours"]: raise ValueError("registry_interval_changed")
    prior.update(instrument=exact_json(instrument),instrument_ms=instrument_ms)
    progress()
    if not funding_already_checked:prior=collect_funding(client,run_id,meta,source,bulk,info,premium,prior,settlement_only=all_mode)
    progress()
    rt=bulk["prices"].get(pair)
    if not isinstance(rt,dict) or rt.get("mkt")!=symbol: raise ValueError("pair_ticker_identity")
    prior=collect_references(meta,source,bulk,prior)
    progress()
    refresh_legacy(source,pair,symbol,rt,prior,now,reuse_oi=all_mode)
    prior.update(instrument=exact_json(instrument),instrument_ms=instrument_ms,last_collected_ms=source.clock())
    return prior
