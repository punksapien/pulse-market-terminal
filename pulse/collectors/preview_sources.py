"""ADR-0097 bounded trusted-source dispatcher for the fixed Preview cohort."""
import time
import threading
from contextlib import nullcontext
from .binance_transport import fetch,HOST,AdmissionDenied
from .coindcx_admission import CoinDCXAdmission
from pulse.store.binance_admission import BinanceAdmission
from pulse.store.shared_admission import RollingLimit


class DispatchPacer:
    """ADR-0109: shared local pacing; Redis admission remains authoritative across instances."""
    def __init__(self,*,binance_gap=.15,coindcx_gap=.15):
        if type(binance_gap) not in (int,float) or not .05<=binance_gap<=.15:
            raise ValueError("dispatch_pacer_bound")
        if type(coindcx_gap) not in (int,float) or not .084<=coindcx_gap<=.15:
            raise ValueError("dispatch_pacer_bound")
        self.binance_gap=binance_gap
        self.coindcx_gap=coindcx_gap
        # ADR-0169: a sleeping provider must not lock the other provider's permits.
        self.locks={HOST:threading.RLock(),"coindcx":threading.RLock()}
        self.last={}
        self.header_lock=threading.RLock()
        self.header_at=None
        self.header_minute=None

    def wait(self,host,deadline):
        host=HOST if host==HOST else "coindcx"
        with self.locks[host]:
            delay=max(0,self.last.get(host,0)+(self.binance_gap if host==HOST else self.coindcx_gap)-time.monotonic())
            if time.monotonic()+delay+2>=deadline:
                raise RuntimeError("worker_source_budget_exhausted")
            if delay: time.sleep(delay)
            self.last[host]=time.monotonic()

    def reserve(self,host,deadline,callback,costs):
        """ADR-0140: serialize the permit, never the HTTP response."""
        # ADR-0178: one lock order even when a callback needs a header probe.
        with self.header_lock if host==HOST else nullcontext():
            with self.locks[HOST if host==HOST else "coindcx"]:
                self.wait(host,deadline)
                try:return callback(costs)
                finally:self.last[HOST if host==HOST else "coindcx"]=time.monotonic()


class Sources:
    def __init__(self,client,*,scope,deadline,ca_file=None,preview_dcx12=False):
        if scope not in ("operator-preview-c1","vercel-bom1-c1","vercel-bom1-all-v1"):
            raise ValueError("preview_egress_scope_refused")
        if type(preview_dcx12) is not bool or preview_dcx12 and scope!="vercel-bom1-all-v1":
            raise ValueError("preview_dcx_policy_scope")
        self.client,self.deadline,self.ca_file=client,deadline,ca_file
        self.all_policy=scope=="vercel-bom1-all-v1"
        self.policy_ready=False
        self.dcx=CoinDCXAdmission(client,scope+("-dcx12" if preview_dcx12 else "-dcx"),preview_policy=preview_dcx12)
        self.binance=BinanceAdmission(client,scope+"-binance",{
            "history":RollingLimit(850 if self.all_policy else 700,300000),"weight":RollingLimit(1680,60000)},
            approved_plan_fallback=True,approved_bootstrap=True)
        self.last=0;self.calls=0;self.last_source_ms=None;self.header_at=None;self.header_minute=None;self.live=False;self.live_deadline=None
        self.check_lease=lambda:None
        # ADR-0164: all-pair throughput inside unchanged shared admission ceilings.
        self.pacer=DispatchPacer(binance_gap=.084 if self.all_policy else .15,
                                coindcx_gap=.084 if preview_dcx12 else .15)

    def clock(self):
        s,us=self.client.time();return s*1000+us//1000

    def initialise(self):
        self._ensure_policy()
        if self.dcx.initialise(): time.sleep(1.01)
        # Weighted success establishes a provider header floor before unweighted calls.
        self._ensure_header()

    def _ensure_policy(self):
        if getattr(self,"all_policy",False) and not self.policy_ready:
            from .all_pair_policy import ensure_ready
            self.binance.preflight()
            self.policy_ready,_=ensure_ready(self.client)
            if not self.policy_ready:raise AdmissionDenied("quota_transition_wait")
        if getattr(self.dcx,"preview_policy",False) and not self.dcx.prepare():
            raise AdmissionDenied("coindcx_quota_transition_wait")

    def _dispatch(self,path,params,*,host,weight,family):
        return self._dispatch_inner(path,params,host=host,weight=weight,family=family)

    def _dispatch_inner(self,path,params,*,host,weight,family,_probe=False):
        self._ensure_policy()
        limit=self.live_deadline if getattr(self,"live",False) and self.live_deadline is not None else self.deadline
        pacer=getattr(self,"pacer",None)
        if pacer is None:
            delay=.15-(time.monotonic()-self.last)
            if delay>0: time.sleep(delay)
        self.check_lease()
        if host==HOST and not _probe:
            self._ensure_header()
            self.check_lease()
        self.last=time.monotonic()
        admission=self.binance if host==HOST else self.dcx
        def checked_reserve(costs):
            self.check_lease()
            if host==HOST and not _probe and pacer is not None:
                before=self.calls
                self._ensure_header()  # ADR-0178: queue waiting must not stale the precheck.
                if self.calls!=before:pacer.wait(host,limit)
                self.check_lease()
            if (host==HOST and path!="/fapi/v1/fundingInfo" and costs.get("history",0)>0
                    and getattr(self,"all_policy",False)):
                return admission.reserve(costs,history_headroom=32)
            return admission.reserve(costs)
        maintenance_started=time.monotonic()
        for attempt in range(129):
            remaining=limit-time.monotonic()-2
            # ADR-0112: don't charge/dispatch a request with a millisecond-scale timeout.
            if remaining<2:raise RuntimeError("worker_source_budget_exhausted")
            try:
                self.calls+=1
                value=fetch(path,params,admission,{"history":2 if family else int(weight==0),"weight":weight},
                    self.ca_file,host=host,max_response=4*1024*1024,decimal_numbers=True,timeout=min(8,remaining),
                    reserve=(lambda costs:pacer.reserve(host,limit,checked_reserve,costs)) if pacer is not None else None,
                    deadline=limit)
                break
            except AdmissionDenied as exc:
                self.calls-=1  # reserve refused before HTTP; never count maintenance as exchange traffic.
                exc.denied_source="binance" if host==HOST else "coindcx"
                # budget_pruned_retry is the ledger's maintenance step: it pruned expired entries and charged nothing,
                # so the caller asks again; every other denial stops the pass.
                if getattr(exc,"admission_reason",None)!="budget_pruned_retry":raise
                self.prune_steps=getattr(self,"prune_steps",0)+1
                self.check_lease()
                if attempt==128 or time.monotonic()-maintenance_started>=4:raise
            except RuntimeError as exc:
                if str(exc)=="worker_source_budget_exhausted":self.calls-=1
                raise
        self.check_lease()
        feedback=admission.last_feedback
        self.last_source_ms=feedback.get("provider_date_ms") if feedback else None
        if host==HOST and weight>0:
            # a weighted success refreshes the provider usage header the ledger relies on
            self.header_at=self.last_source_ms
            self.header_minute=self.header_at//60000 if self.header_at is not None else None
            if pacer is not None:
                with pacer.header_lock:
                    if self.header_at is not None and (pacer.header_at is None or self.header_at>=pacer.header_at):
                        pacer.header_at=self.header_at
                        pacer.header_minute=self.header_minute
        return value

    def _ensure_header(self):
        """ADR-0109: all ordinary calls share a single-flight current-minute header probe.
        Lua stays authoritative; this only avoids a predictable denial and never retries a refused call."""
        pacer=getattr(self,"pacer",None)
        if pacer is not None:
            with pacer.header_lock:
                self.header_at,self.header_minute=pacer.header_at,pacer.header_minute
                return self._ensure_header_inner()
        return self._ensure_header_inner()

    def _ensure_header_inner(self):
        now=self.clock()
        if (self.header_minute==now//60000 and self.header_at is not None and now-self.header_at<55000
                and now%60000<59000):
            return
        if now%60000>=59000:
            # do not straddle a provider minute between the heartbeat and the call it enables
            time.sleep((60000-now%60000)/1000+.1)
        # ADR-0178: functions share response-aware Redis feedback, not a new empty quota.
        if self._shared_header():return
        self._dispatch_inner("/fapi/v1/openInterest",{"symbol":"BTCUSDT"},host=HOST,weight=1,family=False,_probe=True)

    def _shared_header(self):
        admission=self.binance
        if not callable(getattr(admission,"header_state",None)) or not hasattr(admission,"mapping"):return False
        started=time.monotonic()
        for attempt in range(21):
            self.check_lease();now=self.clock()
            values=admission.header_state()
            if not isinstance(values,(tuple,list)) or len(values)!=7:return False
            mapping,state,initialised,*numbers=values
            if (mapping!=admission.mapping.encode() or state!=b"R" or initialised!=b"1"):
                return False
            try:
                if any(v is None or not isinstance(v,bytes) or len(v)>20 for v in numbers[:3]):return False
                if numbers[3] is not None and (not isinstance(numbers[3],bytes) or len(numbers[3])>20):return False
                at,minute,floor=(int(v) for v in numbers[:3]);until=int(numbers[3] or b"0")
            except (ValueError,TypeError):return False
            if floor>=0 and 0<=now-at<55000 and minute==now//60000 and now%60000<59000:
                self.header_at,self.header_minute=at,minute
                pacer=getattr(self,"pacer",None)
                if pacer is not None:pacer.header_at,pacer.header_minute=at,minute
                return True
            if (until<=now or attempt==20 or time.monotonic()-started>=1
                    or time.monotonic()+2.05>=self.deadline):return False
            time.sleep(.05)
        return False

    def request(self,path,params,*,host=HOST,weight=0,family=False):
        self.check_lease()
        # the fast live path (price ticks) has its own, later deadline and a smaller safety margin
        live=getattr(self,"live",False);ld=getattr(self,"live_deadline",None)
        limit,margin=(ld,2) if live and ld is not None else (self.deadline,2)
        if time.monotonic()+margin>=limit: raise RuntimeError("worker_source_budget_exhausted")
        self.check_lease()
        value=self._dispatch(path,params,host=host,weight=weight,family=family)
        return value
