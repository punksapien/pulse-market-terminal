"""ADR-0064 shared response-aware source control; never a viewer-triggered client."""
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
import random
import uuid

from .shared_admission import SharedAdmission
from pulse.observation import MAX_SOURCE_TIME_MS


def header_values(headers,*,now_ms):
    def get(name):
        value=headers.get(name)
        if value is None:
            value=headers.get(name.lower())
        return value if isinstance(value,str) and len(value)<=128 else None
    raw=get("X-MBX-USED-WEIGHT-1M")
    weight=int(raw) if raw and raw.isascii() and raw.isdigit() and len(raw)<=10 else -1
    if weight>1000000000:
        weight=-1
    date=-1
    try:
        parsed=parsedate_to_datetime(get("Date"))
        if parsed.tzinfo is not None and parsed.timestamp()>=0:
            date=int(parsed.timestamp()*1000)
    except (TypeError,ValueError,OverflowError):
        pass
    retry=0
    value=get("Retry-After")
    if value:
        if value.isascii() and value.isdigit():
            retry=int(value)*1000
        else:
            try:
                parsed=parsedate_to_datetime(value)
                if parsed.tzinfo is not None:
                    # A Redis clock ahead of provider time must not shorten its ban.
                    reference=min(now_ms,date) if date>=0 else now_ms-5000
                    retry=max(0,math.ceil(parsed.timestamp()*1000-reference))
            except (TypeError,ValueError,OverflowError):
                pass
    if not 0<=retry<=MAX_SOURCE_TIME_MS-now_ms:
        # Never shorten a valid provider restriction; unrepresentable means closed.
        retry=MAX_SOURCE_TIME_MS-now_ms
    return weight,date,retry


class BinanceAdmission(SharedAdmission):
    def __init__(self,client,scope,limits,*,approved_plan_fallback=False,approved_bootstrap=False):
        super().__init__(client,scope,limits,approved_plan_fallback=approved_plan_fallback)
        if (set(limits)!={"history","weight"} or limits["history"].window_ms!=300000
                or limits["weight"].window_ms!=60000 or type(approved_bootstrap) is not bool):
            raise ValueError("invalid_binance_budget_policy")
        self.keys.append(self.prefix+"control")
        self._expected_keys=tuple(self.keys)
        self.approved_bootstrap=approved_bootstrap
        self.pending=None
        self._source_scripts={}
        self.last_feedback=None

    def _source_script(self,name):
        if name not in self._source_scripts:
            self.preflight()
            self._source_scripts[name]=self.client.script_load((Path(__file__).with_name("lua")/name).read_bytes())
        return self._source_scripts[name]

    def reconcile(self,*args,**kwargs):
        raise RuntimeError("binance_requires_header_bootstrap")

    def header_state(self):
        """ADR-0178: bounded trusted-control read; reservation stays authoritative."""
        self.preflight()
        return self.client.hmget(self.keys[-1],["mapping","state","initialised","header_at",
            "provider_minute","provider_floor","refresh_until"])

    def reserve(self,costs,*,history_headroom=0):
        if self.pending is not None:
            return [b"previous_feedback_required"]
        if (type(costs) is not dict or costs.keys()!=self.limits.keys()
                or any(type(costs[k]) is not int or not 0<=costs[k]<=v.cap for k,v in self.limits.items())
                or not any(costs.values())):
            raise ValueError("invalid_shared_budget_cost")
        if (type(history_headroom) is not int or not 0<=history_headroom<=32
                or history_headroom and self.limits["history"].cap!=850):
            raise ValueError("invalid_history_headroom")
        self.preflight()
        nonce=uuid.uuid4().hex
        config=[]
        for name,limit in self.limits.items():
            # ADR-0184: lower optional admission only; keep the same charged ledger.
            cap=limit.cap-history_headroom if name=="history" and costs[name]>0 else limit.cap
            config.extend((cap,limit.window_ms,costs[name],limit.max_entries))
        result=self.client.evalsha(self._source_script("binance_reserve.lua"),len(self.keys),*self.keys,
            self.mapping,nonce,*config,"approved_header_bootstrap" if self.approved_bootstrap else "denied")
        if result[0]==b"reserved":
            self.pending=(nonce,costs["weight"])
        self.preflight()
        return result

    def feedback(self,status,headers,*,draw=None):
        if self.pending is None:
            raise RuntimeError("feedback_without_permit")
        self.preflight()
        seconds,micros=self.client.time()
        now=seconds*1000+micros//1000
        weight,date,retry=header_values(headers,now_ms=now)
        if type(status) is not int or not 0<=status<=599:
            raise ValueError("invalid_source_status")
        # Missing headers on a weighted success cannot freshen the provider floor.
        if status==200 and self.pending[1]>0 and weight<1:
            weight=0
        elif self.pending[1]==0 and weight==0:
            weight=-1
        draw=random.SystemRandom().random() if draw is None else draw
        if type(draw) not in (int,float) or not math.isfinite(draw) or not 0<=draw<=1:
            raise ValueError("invalid_jitter_draw")
        result=self.client.evalsha(self._source_script("binance_feedback.lua"),len(self.keys),*self.keys,
            self.mapping,self.pending[0],status,weight,date,retry,draw)
        self.last_feedback={"status":status,"used_weight_1m":weight,"provider_date_ms":date,
                            "retry_after_ms":retry,"outcome":result[0].decode("ascii")}
        self.pending=None
        self.preflight()
        if result[0]!=b"feedback_recorded":
            raise RuntimeError("source_feedback_refused:"+result[0].decode("ascii"))
        return self.last_feedback
