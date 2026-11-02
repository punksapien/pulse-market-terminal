"""ADR-0054 offline bounded control-state model. No Redis/network imports."""
import hashlib
import heapq
import math
from dataclasses import dataclass

from pulse.observation import MAX_FUTURE_SKEW_MS, MAX_SOURCE_TIME_MS


def clock(now):
    if type(now) is not int or not 0 <= now <= MAX_SOURCE_TIME_MS:
        raise ValueError("invalid_clock")


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_SOURCE_TIME_MS


def name(value):
    return isinstance(value,str) and 0 < len(value) <= 128


@dataclass(frozen=True)
class DerivedRecord:
    payload: bytes
    source_ms: int
    expires_ms: int
    versions: tuple

    def validate(self, now):
        clock(now)
        if (type(self.payload) is not bytes or len(self.payload)>256
            or not integer(self.source_ms) or self.source_ms>now+MAX_FUTURE_SKEW_MS
            or not integer(self.expires_ms) or self.expires_ms<=now or self.expires_ms<=self.source_ms
            or type(self.versions) is not tuple or not 1 <= len(self.versions) <= 8
            or any(type(v) is not tuple or len(v)!=2 or not name(v[0]) or not integer(v[1]) for v in self.versions)
            or len({v[0] for v in self.versions})!=len(self.versions)):
            raise ValueError("invalid_derived_record")


class IncrementalBuilder:
    def __init__(self, store, namespace, rows, token):
        if not name(namespace) or not integer(rows,1) or rows>4096:
            raise ValueError("invalid_builder_dimensions")
        self.store,self.namespace,self.rows=store,namespace,rows
        def init(state):
            existing=state.setdefault(namespace,{"rows":rows,"epochs":[0]*rows,"dirty":(1<<rows)-1,"records":[None]*rows,"cursor":0,"quarantine":[None]*rows})
            if existing["rows"]!=rows:
                raise ValueError("builder_identity_mismatch")
        store.atomic_control(token,init)

    def mark(self, row, token):
        if not integer(row) or row>=self.rows:
            raise ValueError("invalid_row")
        def update(state):
            data=state[self.namespace]
            if data["epochs"][row]>=MAX_SOURCE_TIME_MS:
                raise OverflowError("epoch_exhausted")
            data["epochs"][row]+=1
            data["dirty"] |= 1<<row
            data["quarantine"][row]=None
        self.store.atomic_control(token,update)

    def build(self, derive, token, *, now_ms, batch=100):
        clock(now_ms)
        if not integer(batch,1) or batch>4096:
            raise ValueError("invalid_batch")
        def capture(state):
            data=state[self.namespace]
            start=data["cursor"]
            captured=[]
            for bits,offset in ((data["dirty"]>>start,start),(data["dirty"] & ((1<<start)-1),0)):
                while bits and len(captured)<batch:
                    lowest=bits & -bits
                    row=lowest.bit_length()-1+offset
                    captured.append((row,data["epochs"][row]))
                    bits ^= lowest
            if captured:
                data["cursor"]=(captured[-1][0]+1)%self.rows
            return captured
        captured=self.store.atomic_control(token,capture)
        candidate,errors={},{}
        for row,_ in captured:
            try:
                value=derive(row)
                if not isinstance(value,DerivedRecord):
                    raise ValueError("invalid_derived_record")
                value.validate(now_ms)
                candidate[row]=value
            except Exception as error:
                # Never put arbitrary exception text (possibly credentials) in state.
                errors[row]="invalid_derived_record" if isinstance(error,ValueError) and str(error)=="invalid_derived_record" else "derive_failed"
        def commit(state):
            data=state[self.namespace]
            complete=True
            for row,epoch in captured:
                if data["epochs"][row]!=epoch:
                    complete=False
                    continue
                reason=errors.get(row)
                old,new=data["records"][row],candidate.get(row)
                if reason is None and old:
                    prior,current=dict(old.versions),dict(new.versions)
                    if prior.keys()!=current.keys() or any(current[key]<value for key,value in prior.items()) or new.source_ms<old.source_ms:
                        reason="input_version_regression"
                    elif prior==current and old!=new:
                        reason="same_version_conflict"
                if reason:
                    data["quarantine"][row]=(epoch,reason)
                    complete=False
                else:
                    data["records"][row]=new
                    data["quarantine"][row]=None
                data["dirty"] &= ~(1<<row)
            return complete
        return self.store.atomic_control(token,commit)

    def read(self,row,*,now_ms):
        clock(now_ms)
        if not integer(row) or row>=self.rows:
            raise ValueError("invalid_row")
        def read(state):
            data=state.get(self.namespace)
            return data["records"][row] if data and data["quarantine"][row] is None else None
        record=self.store.read_control(read)
        return record if record is not None and now_ms<record.expires_ms else None

    def failure(self,row):
        if not integer(row) or row>=self.rows:
            raise ValueError("invalid_row")
        return self.store.read_control(lambda state:state[self.namespace]["quarantine"][row])

    @property
    def dirty_count(self):
        return self.store.read_control(lambda state:state[self.namespace]["dirty"].bit_count())


class DeadlineArtifacts:
    def __init__(self,store,namespace,token,*,max_bytes,max_items=64):
        if not name(namespace) or not integer(max_bytes,1) or max_bytes>store.max_bytes or not integer(max_items,1) or max_items>8192:
            raise ValueError("invalid_artifact_capacity")
        self.store,self.namespace=store,namespace
        def init(state):
            data=state.setdefault(namespace,{"blobs":{},"deadlines":[],"scheduled":{},"retained":{},"bytes":0,"limits":(max_bytes,max_items)})
            if data["limits"]!=(max_bytes,max_items):
                raise ValueError("artifact_identity_mismatch")
        store.atomic_control(token,init)

    @staticmethod
    def _schedule(data,digest,deadline):
        data["scheduled"][digest]=deadline
        heapq.heappush(data["deadlines"],(deadline,digest))
        if len(data["deadlines"])>2*max(1,len(data["blobs"])):
            data["deadlines"]=[(due,key) for key,due in data["scheduled"].items()]
            heapq.heapify(data["deadlines"])

    def stage(self,payload,*,expires_ms,deadline_ms,now_ms,token):
        clock(now_ms)
        if type(payload) is not bytes or not integer(expires_ms) or not integer(deadline_ms) or not now_ms<expires_ms<=deadline_ms:
            raise ValueError("invalid_artifact")
        digest=hashlib.sha256(payload).hexdigest()
        def put(state):
            data=state[self.namespace]
            existing=data["blobs"].get(digest)
            if existing:
                if existing[0]!=payload:
                    raise ValueError("immutable_artifact_identity_conflict")
                expiry,deadline=max(existing[1],expires_ms),max(existing[2],deadline_ms)
                data["blobs"][digest]=(payload,expiry,deadline)
                if deadline>data["scheduled"][digest]:
                    self._schedule(data,digest,deadline)
                return digest
            bytecap,itemcap=data["limits"]
            if data["bytes"]+len(payload)>bytecap or len(data["blobs"])>=itemcap:
                raise MemoryError("artifact_admission_denied")
            data["blobs"][digest]=(payload,expires_ms,deadline_ms)
            data["bytes"]+=len(payload)
            self._schedule(data,digest,deadline_ms)
            return digest
        return self.store.atomic_control(token,put)

    def retain(self,role,digests,token):
        if role not in ("active","previous","candidate") or type(digests) is not tuple:
            raise ValueError("invalid_manifest")
        def put(state):
            data=state[self.namespace]
            if len(digests)>data["limits"][1] or any(d not in data["blobs"] for d in digests):
                raise ValueError("missing_manifest_blob")
            data["retained"][role]=digests
        self.store.atomic_control(token,put)

    def read(self,digest,*,now_ms):
        clock(now_ms)
        value=self.store.read_control(lambda state:state[self.namespace]["blobs"].get(digest))
        return value[0] if value and now_ms<value[1] else None

    def reap(self,*,now_ms,token,batch=100):
        clock(now_ms)
        if not integer(batch,1) or batch>100:
            raise ValueError("invalid_reap_batch")
        def delete(state):
            data=state[self.namespace]
            protected=set(d for manifest in data["retained"].values() for d in manifest)
            removed=[]
            for _ in range(batch):
                if not data["deadlines"] or data["deadlines"][0][0]>now_ms:
                    break
                due,digest=heapq.heappop(data["deadlines"])
                if data["scheduled"].get(digest)!=due:
                    continue
                if digest in protected:
                    self._schedule(data,digest,now_ms+1000)
                    continue
                payload,_,_=data["blobs"].pop(digest)
                data["bytes"]-=len(payload)
                del data["scheduled"][digest]
                removed.append(digest)
            if len(data["deadlines"])>2*max(1,len(data["blobs"])):
                data["deadlines"]=[(due,key) for key,due in data["scheduled"].items()]
                heapq.heapify(data["deadlines"])
            return tuple(removed)
        return self.store.atomic_control(token,delete)


class RepairQueue:
    @staticmethod
    def urgency(key,value,now_ms):
        slack=value[0]-now_ms
        return (0,slack*value[1],key) if slack<0 else (1,slack/value[1],key)

    def __init__(self,store,namespace,token,*,capacity=512):
        if not name(namespace) or not integer(capacity,1) or capacity>8192:
            raise ValueError("invalid_queue_capacity")
        self.store,self.namespace=store,namespace
        def init(state):
            data=state.setdefault(namespace,{"items":{},"epoch":0,"capacity":capacity})
            if data["capacity"]!=capacity:
                raise ValueError("queue_identity_mismatch")
        store.atomic_control(token,init)

    def enqueue(self,key,*,expires_ms,importance,now_ms,token):
        clock(now_ms)
        if type(key) is not tuple or len(key)!=3 or not all(name(part) for part in key) or not integer(expires_ms) or not integer(importance,1) or importance>1000:
            raise ValueError("invalid_repair")
        def put(state):
            data=state[self.namespace]
            old=data["items"].get(key)
            if old:
                # Do not reset the lease on a duplicate enqueue.
                updated=(min(expires_ms,old[0]),max(importance,old[1]))
                if updated!=(old[0],old[1]):
                    old[0],old[1]=updated
                    old[3]=0  # A changed task cannot be acknowledged by its former claim.
                    old[2]=now_ms
                return False
            if len(data["items"])>=data["capacity"]:
                expired=[(old_key,value) for old_key,value in data["items"].items() if value[0]<=now_ms and value[2]<=now_ms]
                if not expired:
                    raise MemoryError("repair_admission_denied")
                discard,_=max(expired,key=lambda item:self.urgency(item[0],item[1],now_ms))
                del data["items"][discard]
            data["items"][key]=[expires_ms,importance,now_ms,0]
            return True
        return self.store.atomic_control(token,put)

    def claim(self,*,now_ms,lease_ms,token,batch=1):
        clock(now_ms)
        if not integer(lease_ms,1) or lease_ms>60000 or not integer(batch,1) or batch>100:
            raise ValueError("invalid_repair_claim")
        def take(state):
            data=state[self.namespace]
            selected=heapq.nsmallest(batch,((key,value) for key,value in data["items"].items() if value[2]<=now_ms),
                                     key=lambda entry:self.urgency(entry[0],entry[1],now_ms))
            if data["epoch"]+len(selected)>MAX_SOURCE_TIME_MS or now_ms+lease_ms>MAX_SOURCE_TIME_MS:
                raise OverflowError("repair_epoch_or_lease_exhausted")
            result=[]
            for key,value in selected:
                data["epoch"]+=1
                value[2],value[3]=now_ms+lease_ms,data["epoch"]
                result.append((key,value[3]))
            return tuple(result)
        return self.store.atomic_control(token,take)

    def ack(self,key,epoch,token,*,now_ms):
        clock(now_ms)
        if not integer(epoch,1):
            return False
        def remove(state):
            data=state[self.namespace]["items"]
            if key not in data or data[key][3]!=epoch or now_ms>=data[key][2]:
                return False
            del data[key]
            return True
        return self.store.atomic_control(token,remove)


class RollingScrubber:
    """Header-only sweep. Does not validate occupancy or finite payload corruption."""
    def __init__(self,store,namespace,manifest,token):
        if not name(namespace) or type(manifest) is not tuple or len(manifest)>6000 or not all(name(key) for key in manifest) or len(set(manifest))!=len(manifest):
            raise ValueError("invalid_scrub_manifest")
        self.store,self.namespace=store,namespace
        def init(state):
            data=state.setdefault(namespace,{"manifest":manifest,"cursor":0})
            if data["manifest"]!=manifest:
                raise ValueError("scrub_manifest_mismatch")
        store.atomic_control(token,init)

    def tick(self,token):
        def run(state):
            data=state[self.namespace]
            manifest=data["manifest"]
            if not manifest:
                return ()
            count=(len(manifest)+59)//60
            keys=tuple(manifest[(data["cursor"]+i)%len(manifest)] for i in range(count))
            result=self.store.inspect_ring_headers(keys,token)
            data["cursor"]=(data["cursor"]+count)%len(manifest)
            return result
        return self.store.atomic_control(token,run)


class PressureLadder:
    """Frozen C0 prototype; runtime staging policy is MeasuredCapacity (ADR-0066)."""
    def __init__(self,store,namespace,token,hard_ceiling=21000000):
        if not integer(hard_ceiling,1) or not name(namespace):
            raise ValueError("invalid_memory_ceiling")
        self.store,self.namespace,self.ceiling=store,namespace,hard_ceiling
        def init(state):
            data=state.setdefault(namespace,{"ceiling":hard_ceiling,"level":0})
            if data["ceiling"]!=hard_ceiling:
                raise ValueError("pressure_identity_mismatch")
        store.atomic_control(token,init)

    def observe(self,used_bytes,token):
        if not integer(used_bytes):
            raise ValueError("invalid_memory_measurement")
        def update(state):
            data=state[self.namespace]
            level=data["level"]
            thresholds=(70,80,90,100)
            while level and used_bytes*100 < (thresholds[level-1]-5)*self.ceiling:
                level-=1
            target=sum(used_bytes*100>=threshold*self.ceiling for threshold in thresholds)
            level=max(level,target)
            data["level"]=level
            return {"level":level,"warn":level>=1,"pause_low_priority":level>=2,
                    "stop_publish":level>=3,"allocation_denied":level>=4}
        return self.store.atomic_control(token,update)
