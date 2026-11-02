import unittest
from pulse.collectors.worker_loop import BoundedLoop,Job


class WorkerLoopTests(unittest.TestCase):
    def rig(self,jobs,*,admit=lambda _:True,lease=lambda:True):
        self.t=0
        def wait(ms): self.t+=ms
        return BoundedLoop(jobs,clock=lambda:self.t,wait=wait,lease_alive=lease,admit=admit)

    def test_five_second_cadence_and_execution_ceiling(self):
        seen=[]
        loop=self.rig([Job("live",5000,lambda **kw:seen.append((self.t,kw["deadline_ms"])))])
        result=loop.run()
        self.assertEqual([t for t,_ in seen],list(range(0,55000,5000)))
        self.assertTrue(all(d==55000 for _,d in seen))
        loop=self.rig([Job("live",1,lambda **kw:None)])
        self.assertEqual(loop.run(max_executions=3)["reason"],"execution_limit")

    def test_denial_and_failure_are_not_retried_same_invocation(self):
        called=[];admissions=[]
        def admit(n): admissions.append(n);return n!="denied"
        def broken(**kw): called.append("failed");raise TimeoutError()
        loop=self.rig([Job("denied",1,lambda **kw:called.append("bad")),Job("failed",1,broken),
                       Job("healthy",5000,lambda **kw:called.append("healthy"))],admit=admit)
        events=loop.run(duration_ms=12000)["events"]
        self.assertEqual(admissions.count("denied"),1)
        self.assertEqual(called.count("failed"),1)
        self.assertEqual(called.count("healthy"),3)
        self.assertNotIn("bad",called)
        self.assertEqual([e["status"] for e in events[:2]],["denied","failed"])

    def test_overrun_no_catchup_and_lease_loss(self):
        def slow(**kw): self.t+=60000
        loop=self.rig([Job("a",5000,slow)])
        self.assertEqual(loop.run()["reason"],"callback_overrun")
        self.assertEqual(loop.pending[0][0],65000)
        def lose(**kw): alive[0]=False
        alive=[True]
        loop=self.rig([Job("a",5000,lose),Job("b",1,lambda **kw:self.fail("ran unfenced"))],lease=lambda:alive[0])
        result=loop.run();self.assertEqual(result["reason"],"fenced_out");self.assertEqual(len(result["events"]),1)

    def test_invalid_budgets_job_names_and_clock(self):
        for jobs in ([],[Job([],1,lambda **kw:None)],[Job("x",0,lambda **kw:None)],
                     [Job("x",1,lambda **kw:None)]*2):
            with self.assertRaises(ValueError): self.rig(jobs)
        loop=self.rig([Job("x",1,lambda **kw:None)])
        for args in ({"duration_ms":55001},{"duration_ms":0},{"max_executions":0}):
            with self.assertRaises(ValueError): loop.run(**args)
        loop.last_clock=10
        with self.assertRaises(ValueError): loop.run()


if __name__=="__main__": unittest.main()
