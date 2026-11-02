"""ADR-0096 conservative shared CoinDCX quota; never resets live usage."""
from email.utils import parsedate_to_datetime
from pulse.store.shared_admission import SharedAdmission,RollingLimit

COOLDOWN="""if string.sub(KEYS[1],1,10)~='pulse:stg:' then return 0 end
local until_ms=tonumber(ARGV[1]); local previous=tonumber(redis.call('GET',KEYS[1]) or '0')
if until_ms>previous then redis.call('SET',KEYS[1],ARGV[1],'PX',until_ms-tonumber(ARGV[2])) end
return 1"""


class CoinDCXAdmission:
    def __init__(self,client,scope,*,preview_policy=False):
        if type(preview_policy) is not bool:raise ValueError("coindcx_policy_flag")
        self.client=client
        self.preview_policy=preview_policy;self.policy_ready=False
        limits=({"requests":RollingLimit(12,1000,32),"minute":RollingLimit(720,60000,1024)}
            if preview_policy else {"requests":RollingLimit(8,1000,32)})
        self.ledger=SharedAdmission(client,scope,limits,approved_plan_fallback=True)
        self.cooldown=self.ledger.prefix+"cooldown"
        self.last_feedback=None

    def clock(self):
        s,us=self.client.time();return s*1000+us//1000

    def initialise(self):
        if self.preview_policy:
            if not self.prepare():raise RuntimeError("coindcx_policy_wait")
            return False
        # Charge the whole first second; unknown usage never becomes an empty ledger.
        result=self.ledger.reconcile({"requests":8},evidence_id="conservative_initial_full_window")
        if result[0] not in (b"reconciled",b"cannot_reset_live_budget"):
            raise RuntimeError("coindcx_quota_initialisation_refused")
        return result[0]==b"reconciled"

    def prepare(self):
        if self.preview_policy and not self.policy_ready:
            from .coindcx_policy import ensure_ready
            self.policy_ready,_=ensure_ready(self.client,self.ledger,self.cooldown)
        return self.policy_ready

    def reserve(self,costs):
        if self.preview_policy and not self.policy_ready:return [b"coindcx_policy_not_ready"]
        until=self.client.get(self.cooldown)
        if until is not None and self.clock()<int(until): return [b"coindcx_circuit_open"]
        return self.ledger.reserve({name:1 for name in self.ledger.limits})

    def feedback(self,status,headers):
        now=self.clock()
        if status in (0,403,418,429,451) or status>=500:
            retry=300000 if status in (403,418,429,451) else 30000
            raw=headers.get("Retry-After",headers.get("retry-after"))
            if isinstance(raw,str):
                if raw.isdigit() and len(raw)<=8: retry=max(retry,int(raw)*1000)
                else:
                    try: retry=max(retry,int(parsedate_to_datetime(raw).timestamp()*1000)-now+5000)
                    except (ValueError,TypeError,OverflowError): pass
            retry=min(retry,86400000)
            self.client.eval(COOLDOWN,1,self.cooldown,now+retry,now)
        provider=None
        try:
            raw=headers.get("Date",headers.get("date"))
            stamp=parsedate_to_datetime(raw)
            if stamp.tzinfo is not None: provider=int(stamp.timestamp()*1000)
        except (ValueError,TypeError,OverflowError): pass
        self.last_feedback={"status":status,"provider_date_ms":provider}
        return self.last_feedback
