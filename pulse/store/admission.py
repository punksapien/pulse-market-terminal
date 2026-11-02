"""ADR-0054 exact local admission budgets and persisted circuit/backoff model."""
from collections import deque
from dataclasses import dataclass
import math

from .recovery import clock, integer, name


@dataclass(frozen=True)
class BudgetLimit:
    cap: int
    window_ms: int | None = None
    billing_start_ms: int | None = None
    billing_end_ms: int | None = None
    max_entries: int = 8192

    def validate(self):
        if not integer(self.cap,1) or not integer(self.max_entries,1) or self.max_entries>65536:
            raise ValueError("invalid_budget_limit")
        if self.window_ms is not None:
            if not integer(self.window_ms,1) or self.billing_start_ms is not None or self.billing_end_ms is not None:
                raise ValueError("invalid_rolling_window")
        elif not integer(self.billing_start_ms) or not integer(self.billing_end_ms) or self.billing_end_ms<=self.billing_start_ms:
            raise ValueError("invalid_billing_window")


class BudgetTracker:
    def __init__(self,store,namespace,limits,token):
        if not name(namespace) or type(limits) is not dict or not 1<=len(limits)<=8 or not all(name(key) for key in limits):
            raise ValueError("invalid_budget_dimensions")
        for limit in limits.values():
            if not isinstance(limit,BudgetLimit):
                raise ValueError("invalid_budget_limit")
            limit.validate()
        self.store,self.namespace=store,namespace
        def init(state):
            data=state.setdefault(namespace,{"limits":dict(limits),"events":{key:deque() for key in limits},"totals":{key:0 for key in limits},"clock":-1,"reconciled":False})
            if data["limits"]!=limits:
                raise ValueError("budget_identity_mismatch")
        store.atomic_control(token,init)

    def reconcile(self,usage,*,now_ms,evidence_id,token):
        """Explicit bootstrap with verified usage, NOT an automatic zero reset.

        Aggregate rolling usage is conservatively charged for one whole window.
        After storage loss, wait the window or obtain provider/approved-slice evidence;
        monthly usage needs independent billing evidence. C0 tests use synthetic usage.
        """
        clock(now_ms)
        if type(usage) is not dict or not name(evidence_id):
            raise ValueError("invalid_budget_evidence")
        def reconcile(state):
            data=state[self.namespace]
            if data["reconciled"]:
                raise ValueError("cannot_reset_live_budget")
            if usage.keys()!=data["limits"].keys() or any(not integer(used) or used>data["limits"][key].cap for key,used in usage.items()):
                raise ValueError("invalid_budget_usage")
            if any(limit.window_ms is None and not limit.billing_start_ms<=now_ms<limit.billing_end_ms for limit in data["limits"].values()):
                raise ValueError("billing_window_unverified")
            for key,used in usage.items():
                data["totals"][key]=used
                if used and data["limits"][key].window_ms is not None:
                    data["events"][key].append((now_ms,used))
            data.update(reconciled=True,clock=now_ms,evidence=evidence_id)
        self.store.atomic_control(token,reconcile)

    def reserve(self,costs,*,now_ms,token):
        clock(now_ms)
        if type(costs) is not dict or any(not integer(value) for value in costs.values()) or not any(costs.values()):
            raise ValueError("invalid_reservation")
        def reserve(state):
            data=state.get(self.namespace)
            if data is None or not data["reconciled"]:
                return "budget_reconciliation_required"
            if costs.keys()!=data["limits"].keys():
                raise ValueError("missing_budget_dimension")
            if now_ms<data["clock"]:
                return "clock_regression"
            data["clock"]=now_ms
            for key,limit in data["limits"].items():
                events=data["events"][key]
                if limit.window_ms is None:
                    if not limit.billing_start_ms<=now_ms<limit.billing_end_ms:
                        return "billing_window_unverified"
                else:
                    while events and events[0][0]<=now_ms-limit.window_ms:
                        _,cost=events.popleft()
                        data["totals"][key]-=cost
                if data["totals"][key]+costs[key]>limit.cap:
                    return "budget_exhausted:"+key
                if limit.window_ms is not None and costs[key] and len(events)>=limit.max_entries:
                    return "budget_ledger_full:"+key
            # No reservation occurs unless EVERY dimension passes.
            for key,limit in data["limits"].items():
                data["totals"][key]+=costs[key]
                if costs[key] and limit.window_ms is not None:
                    data["events"][key].append((now_ms,costs[key]))
            return "reserved"
        return self.store.atomic_control(token,reserve)


class CircuitBreaker:
    def __init__(self,store,namespace,token,*,threshold=3,base_ms=1000,cap_ms=60000,ttl_ms=300000):
        if not name(namespace) or not integer(threshold,1) or not integer(base_ms,1) or not integer(cap_ms,base_ms) or not integer(ttl_ms,cap_ms):
            raise ValueError("invalid_circuit_config")
        self.store,self.namespace=store,namespace
        self.config=(threshold,base_ms,cap_ms,ttl_ms)
        def init(state):
            data=state.setdefault(namespace,{"config":self.config,"valid_until":-1,"state":"unknown","failures":0,"attempt":0,"retry":0,"probe_until":0,"probe":0,"clock":-1})
            if data["config"]!=self.config:
                raise ValueError("circuit_identity_mismatch")
        store.atomic_control(token,init)

    def reconcile(self,*,now_ms,token):
        """Explicit operator/bootstrap action, never automatic on a public read."""
        clock(now_ms)
        def reconcile(state):
            data=state[self.namespace]
            if now_ms<data["clock"]:
                raise ValueError("clock_regression")
            if data["state"]!="unknown" and now_ms<data["valid_until"]:
                raise ValueError("cannot_reset_live_circuit")
            # A lost state permits ONE probe, not a reset to closed.
            data.update(state="open",valid_until=now_ms+self.config[3],retry=now_ms,clock=now_ms,probe_until=0)
        self.store.atomic_control(token,reconcile)

    def allow(self,*,now_ms,token):
        clock(now_ms)
        def allow(state):
            data=state.get(self.namespace)
            if data is None:
                return (False,"reconciliation_required")
            if now_ms<data["clock"]:
                return (False,"clock_regression")
            data["clock"]=now_ms
            if now_ms>=data["valid_until"]:
                return (False,"reconciliation_required")
            if data["state"]=="closed":
                return (True,None)
            if now_ms<data["retry"] or now_ms<data["probe_until"]:
                return (False,"circuit_open")
            data["probe"]+=1
            data.update(state="half_open",probe_until=now_ms+10000)
            return (True,data["probe"])
        return self.store.atomic_control(token,allow)

    def outcome(self,*,now_ms,token,status,draw,probe=None,retry_after_ms=0):
        clock(now_ms)
        if type(status) is not int or not 0<=status<=599 or type(draw) not in (float,int) or not math.isfinite(draw) or not 0<=draw<=1 or not integer(retry_after_ms):
            raise ValueError("invalid_circuit_outcome")
        def update(state):
            data=state[self.namespace]
            if now_ms<data["clock"]:
                raise ValueError("expired_circuit_outcome")
            # ADR-0055: a late real hard-block response is still negative evidence.
            if status in (403,418,429,451):
                if (probe is not None and (not integer(probe,1) or probe>data["probe"])) or data["state"]=="unknown":
                    raise ValueError("no_admitted_probe")
                attempt=min(data["attempt"]+1,63)
                ceiling=min(self.config[2],self.config[1]*2**(attempt-1))
                delay=max(self.config[1],retry_after_ms,int(draw*ceiling),data["retry"]-now_ms)
                if now_ms+max(self.config[3],delay+10000)>9007199254740991:
                    raise ValueError("circuit_deadline_overflow")
                data.update(state="open",retry=now_ms+delay,probe_until=0,
                            attempt=attempt,failures=data["failures"]+1,clock=now_ms,
                            valid_until=now_ms+max(self.config[3],delay+10000))
                return delay
            if now_ms>=data["valid_until"]:
                raise ValueError("expired_circuit_outcome")
            if data["state"]=="half_open" and (probe!=data["probe"] or now_ms>=data["probe_until"]):
                raise ValueError("stale_probe")
            if data["state"] in ("unknown","open"):
                raise ValueError("no_admitted_probe")
            data["clock"]=now_ms
            success=200<=status<400
            if success:
                data.update(state="closed",failures=0,attempt=0,probe_until=0,valid_until=now_ms+self.config[3])
                return 0
            data["failures"]+=1
            if data["failures"]>=self.config[0] or data["state"]=="half_open":
                data["attempt"]=min(data["attempt"]+1,63)
                ceiling=min(self.config[2],self.config[1]*2**(data["attempt"]-1))
                delay=max(retry_after_ms,int(draw*ceiling))
                data.update(state="open",retry=now_ms+delay,probe_until=0,valid_until=max(now_ms+self.config[3],now_ms+delay+10000))
                return delay
            return 0
        return self.store.atomic_control(token,update)
