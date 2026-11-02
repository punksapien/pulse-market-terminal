"""ADR-0062 synthetic quota cases on newly named staging ledgers; no source calls."""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pulse_staging_gate import connect_staging
from pulse.store.shared_admission import SharedAdmission, RollingLimit


def run(cli,*,approved_plaintext=False,approved_plan_fallback=False):
    client,identity=connect_staging(cli,approved_plaintext=approved_plaintext)
    keys=[]
    try:
        scope="synthetic-"+uuid.uuid4().hex
        limits={"history":RollingLimit(3,300000),"weight":RollingLimit(5,60000)}
        first=SharedAdmission(client,scope,limits,approved_plan_fallback=approved_plan_fallback)
        second=SharedAdmission(client,scope,limits,approved_plan_fallback=approved_plan_fallback)
        keys=first.keys
        checks=[]
        def check(name,actual,expected):
            if actual[0]!=expected:
                raise RuntimeError(name+":"+actual[0].decode("ascii"))
            checks.append(name)
        costs={"history":1,"weight":2}
        check("missing_state_refused",first.reserve(costs),b"budget_reconciliation_required")
        check("explicit_usage_bootstrap",first.reconcile({"history":1,"weight":1},evidence_id="synthetic-test-not-provider-usage"),b"reconciled")
        check("second_writer_charged",second.reserve(costs),b"reserved")
        check("first_writer_charged",first.reserve(costs),b"reserved")
        check("all_dimensions_denied",second.reserve(costs),b"budget_exhausted:1")
        check("cannot_reset",second.reconcile({"history":0,"weight":0},evidence_id="synthetic"),b"cannot_reset_live_budget")
        totals=client.hgetall(first.keys[0])
        if totals[b"total:1"]!=b"3" or totals[b"total:2"]!=b"5":
            raise RuntimeError("shared_budget_totals_wrong")
        # Mark only our new fixture Q to prove ambiguous state cannot dispatch.
        first.preflight()
        client.hset(first.keys[0],"state","Q")
        check("partial_state_fails_closed",first.reserve(costs),b"budget_quarantined")
        # A separate fresh synthetic fixture exercises bounded expiry maintenance.
        prune=SharedAdmission(client,"synthetic-"+uuid.uuid4().hex,{"weight":RollingLimit(1000,60000,128)},
                              approved_plan_fallback=approved_plan_fallback)
        keys=keys+prune.keys
        check("prune_fixture_bootstrap",prune.reconcile({"weight":0},evidence_id="synthetic-expiry-fixture"),b"reconciled")
        prune.preflight()
        seed="""local clock=redis.call('TIME')
        local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
        if redis.call('HGET',KEYS[1],'total:1')~='0' or redis.call('ZCARD',KEYS[2])~=0 then return 0 end
        for i=1,65 do redis.call('ZADD',KEYS[2],now-60001,'1:'..string.format('%032x',i)) end
        redis.call('HSET',KEYS[1],'total:1',65,'count:1',65)
        return 1"""
        if client.eval(seed,2,*prune.keys)!=1:
            raise RuntimeError("prune_fixture_refused")
        check("bounded_prune_does_not_admit",prune.reserve({"weight":1}),b"budget_pruned_retry")
        check("after_prune_single_charge",prune.reserve({"weight":1}),b"reserved")
        if client.hget(prune.keys[0],"total:1")!=b"1" or client.zcard(prune.keys[1])!=1:
            raise RuntimeError("prune_totals_wrong")
        return {"status":"synthetic_shared_admission_passed","keys":keys,"checks":checks,
                "exchange_calls":0,"deletions":0,"transport":identity["transport"],
                "memory":first.last_memory,"live_egress_usage_reconciled":False,"staging_complete":False}
    except Exception as error:
        return {"status":"failed","keys":keys,"reason":str(error) if isinstance(error,RuntimeError) else type(error).__name__}
    finally:
        client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    parser.add_argument("--approved-staging-plaintext",action="store_true")
    parser.add_argument("--approved-plan-capacity",action="store_true")
    args=parser.parse_args()
    result=run(args.vercel_cli,approved_plaintext=args.approved_staging_plaintext,
        approved_plan_fallback=args.approved_plan_capacity)
    print(json.dumps(result,indent=2))
    if result["status"]=="failed":
        raise SystemExit(1)
