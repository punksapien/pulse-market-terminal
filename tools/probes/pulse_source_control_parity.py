"""ADR-0064 bounded synthetic header/circuit parity, zero exchange requests."""
import argparse
from datetime import datetime,timezone
from email.utils import format_datetime
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.shared_admission import RollingLimit


def run(cli):
    client,identity=connect_staging(cli,approved_plaintext=True)
    result={"status":"running","keys":[],"checks":[],"exchange_calls":0}
    def clock():
        seconds,micros=client.time()
        return seconds*1000+micros//1000
    def date():
        return format_datetime(datetime.fromtimestamp(clock()/1000,timezone.utc),usegmt=True)
    def store(cap=20):
        item=BinanceAdmission(client,"synthetic-"+uuid.uuid4().hex,
            {"history":RollingLimit(20,300000),"weight":RollingLimit(cap,60000)},
            approved_plan_fallback=True,approved_bootstrap=True)
        result["keys"].extend(item.keys)
        return item
    def expect(name,actual,expected):
        if actual[0]!=expected:
            raise RuntimeError(name+":"+actual[0].decode("ascii"))
        result["checks"].append(name)
    try:
        a=store()
        b=BinanceAdmission(client,a.scope,a.limits,approved_plan_fallback=True,approved_bootstrap=True)
        expect("history_cannot_bootstrap",a.reserve({"history":1,"weight":0}),b"bootstrap_probe_required")
        expect("single_fixed_bootstrap",a.reserve({"history":0,"weight":1}),b"reserved")
        expect("second_writer_cannot_bootstrap",b.reserve({"history":0,"weight":1}),b"bootstrap_already_charged")
        a.feedback(200,{"X-MBX-USED-WEIGHT-1M":"5","Date":date()},draw=0)
        if client.hget(a.keys[0],"total:2")!=b"5":
            raise RuntimeError("header_bootstrap_not_recorded")
        result["checks"].append("positive_header_seeds_usage")
        expect("first_regular",a.reserve({"history":0,"weight":1}),b"reserved")
        expect("second_regular",b.reserve({"history":0,"weight":1}),b"reserved")
        b.feedback(429,{"Retry-After":"60","Date":date()},draw=0)
        a.feedback(200,{"X-MBX-USED-WEIGHT-1M":"2","Date":date()},draw=0)
        expect("late_success_cannot_close_circuit",b.reserve({"history":0,"weight":1}),b"circuit_open")
        if int(client.hget(a.keys[-1],"provider_floor"))<7:
            raise RuntimeError("smaller_header_rewound_usage")
        result["checks"].append("smaller_header_cannot_refund")
        item=store(5)
        expect("floor_fixture_bootstrap",item.reserve({"history":0,"weight":1}),b"reserved")
        item.feedback(200,{"X-MBX-USED-WEIGHT-1M":"5","Date":date()},draw=0)
        # Our fresh synthetic ledger only: expire local charge, keep provider floor.
        item.preflight()
        script="""local rows=redis.call('ZRANGE',KEYS[1],0,8)
        local c=redis.call('TIME');local now=tonumber(c[1])*1000+math.floor(tonumber(c[2])/1000)
        for _,member in ipairs(rows) do redis.call('ZADD',KEYS[1],now-60001,member) end
        return #rows"""
        client.eval(script,1,item.keys[2])
        expect("local_expiry_cannot_forget_external_usage",item.reserve({"history":0,"weight":1}),b"provider_budget_exhausted")
        for status in (403,418,429,451):
            item=store()
            expect("hard_bootstrap_"+str(status),item.reserve({"history":0,"weight":1}),b"reserved")
            try:
                item.feedback(status,{"Retry-After":"60","Date":date()},draw=0)
            except RuntimeError as error:
                if "bootstrap_header_unverified" not in str(error):
                    raise
            expect("hard_status_opens_"+str(status),item.reserve({"history":0,"weight":1}),b"circuit_open")
        item=store()
        expect("missing_header_fixture",item.reserve({"history":0,"weight":1}),b"reserved")
        try:
            item.feedback(200,{"Date":date()},draw=0)
        except RuntimeError:
            pass
        expect("missing_header_refuses",item.reserve({"history":0,"weight":1}),b"circuit_open")
        item=store()
        expect("clock_fixture_bootstrap",item.reserve({"history":0,"weight":1}),b"reserved")
        item.feedback(200,{"X-MBX-USED-WEIGHT-1M":"1","Date":date()},draw=0)
        expect("clock_fixture_reservation",item.reserve({"history":0,"weight":1}),b"reserved")
        item.preflight()
        client.hset(item.keys[0],"clock",clock()+5000)
        try:
            item.feedback(200,{"X-MBX-USED-WEIGHT-1M":"2","Date":date()},draw=0)
            raise RuntimeError("regressed_feedback_was_accepted")
        except RuntimeError as error:
            if "clock_regression" not in str(error):
                raise
        result["checks"].append("regressed_feedback_refused")
        expect("reservation_cannot_bypass_feedback_clock",item.reserve({"history":0,"weight":1}),b"clock_regression")
        result.update(status="source_control_synthetic_passed",memory=client.info("memory"),
                      transport=identity["transport"],staging_complete=False)
        return result
    except Exception as error:
        result.update(status="failed",reason=str(error) if isinstance(error,RuntimeError) else type(error).__name__)
        return result
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    args=parser.parse_args()
    result=run(args.vercel_cli)
    print(json.dumps(result,indent=2))
    if result["status"]=="failed":
        raise SystemExit(1)
