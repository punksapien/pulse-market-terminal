"""ADR-0089 bounded orchestration; no exchange/store calls on import."""
from dataclasses import dataclass
import heapq


@dataclass(frozen=True)
class Job:
    name: str
    period_ms: int
    run: object


class BoundedLoop:
    def __init__(self,jobs,*,clock,wait,lease_alive,admit):
        if not all(callable(f) for f in (clock,wait,lease_alive,admit)):
            raise ValueError("worker_dependencies_required")
        if not 1<=len(jobs)<=4096 or any(not isinstance(j,Job) for j in jobs): raise ValueError("worker_job_scope")
        for j in jobs:
            if not isinstance(j.name,str) or not 1<=len(j.name)<=128 or type(j.period_ms) is not int or j.period_ms<1 or not callable(j.run):
                raise ValueError("invalid_worker_job")
        if len({j.name for j in jobs})!=len(jobs): raise ValueError("worker_job_scope")
        self.clock,self.wait,self.lease_alive,self.admit=clock,wait,lease_alive,admit
        self.last_clock=-1
        now=self.now();self.jobs={j.name:j for j in jobs}
        self.pending=[(now,j.name) for j in jobs];heapq.heapify(self.pending)

    def now(self):
        value=self.clock()
        if type(value) is not int or value<self.last_clock or value<0:
            raise ValueError("worker_clock_not_monotonic_ms")
        self.last_clock=value
        return value

    def run(self,*,duration_ms=55000,max_executions=256):
        if type(duration_ms) is not int or not 1<=duration_ms<=55000 or type(max_executions) is not int or not 1<=max_executions<=4096:
            raise ValueError("invalid_worker_budget")
        end=self.now()+duration_ms;events=[];attempted=set()
        while self.now()<end and len(events)<max_executions:
            if not self.lease_alive(): return {"reason":"fenced_out","events":events}
            due,name=self.pending[0];now=self.now()
            if due>=end: break
            if due>now:
                self.wait(min(due-now,end-now,1000));continue
            heapq.heappop(self.pending)
            job=self.jobs[name];status="denied"
            try:
                if name not in attempted and self.admit(name):
                    status="completed";job.run(deadline_ms=end)
            except Exception:
                status="failed"
            after=self.now()
            next_due=due+(max(0,(after-due)//job.period_ms)+1)*job.period_ms
            if status in ("denied","failed"):
                attempted.add(name);next_due=max(next_due,end)
            heapq.heappush(self.pending,(next_due,name))
            events.append({"name":name,"status":status,"start_ms":now,"end_ms":after})
            if not self.lease_alive(): return {"reason":"fenced_out","events":events}
            if after>end: return {"reason":"callback_overrun","events":events}
        return {"reason":"execution_limit" if len(events)>=max_executions else "budget_complete","events":events}
