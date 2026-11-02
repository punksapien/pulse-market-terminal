from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from pulse.collectors.all_pair_collect import run_due,maintenance_allowed

NOW=1800000000000


class AllPairCollectTests(TestCase):
    def test_pressure_keeps_bounded_existing_maintenance_not_bootstrap(self):
        for kind,record,allowed in (
            ('bootstrap',{'opens':[[1,'1']]},False),('references',{},False),
            ('references',{'opens':[[1,'1']]},True),('detail',{},False),
            ('detail',{'top_source_ms':True},False),('detail',{'top_source_ms':1},True),
            ('oi',{},True),('instrument',{},True)):
            with self.subTest(kind=kind,record=record):self.assertEqual(maintenance_allowed(kind,record),allowed)

    def test_one_blocked_provider_cannot_occupy_every_worker(self):
        from threading import Event,Lock
        from unittest.mock import Mock
        from pulse.collectors import all_pair_collect as module
        release=Event();lock=Lock();active={'coindcx':0,'binance':0};peaks=dict(active)
        members=[str(i) for i in range(33)]+['BINANCE']
        universe=[dict(symbol=s,pair_id='B-'+s+'_USDT') for s in members]
        def due(u,records,*,attempted=frozenset(),**kw):
            return [(i,s,'oi' if s=='BINANCE' else 'instrument') for i,s in enumerate(members)
                    if (s,'oi' if s=='BINANCE' else 'instrument') not in attempted]
        def execute(job,meta,source,prior,**kw):
            lane='binance' if job.kind=='oi' else 'coindcx'
            with lock:active[lane]+=1;peaks[lane]=max(peaks[lane],active[lane])
            try:
                if lane=='binance':release.set()
                else:self.assertTrue(release.wait(2),'Binance must start while DCX waits')
                source.calls+=1;return {'done':True}
            finally:
                with lock:active[lane]-=1
        state=Mock();state.read.side_effect=lambda n:{'index':0} if n=='SNAPSHOT_CURSOR' else None
        source=lambda deadline:SimpleNamespace(clock=lambda:NOW,calls=0)
        with patch.object(module,'due_jobs',side_effect=due),patch.object(module,'execute',side_effect=execute):
            result,stats=run_due(SimpleNamespace(info=lambda s:{'used_memory':10000000}),
                'a'*32,universe,{},state,Mock(),source(100),source,{}, {},{'data':{}},'m',
                now=lambda:0,deadline=100,functions={})
        self.assertEqual(len(result),34);self.assertLessEqual(peaks['coindcx'],20)
        self.assertLessEqual(peaks['binance'],16);self.assertEqual(stats['schedule_attempted'],34)

    def test_slow_dcx_uses_twenty_slots_while_twelve_binance_jobs_progress(self):
        from threading import Event,Lock
        from unittest.mock import Mock
        from pulse.collectors import all_pair_collect as module
        release=Event();lock=Lock();active={'coindcx':0,'binance':0};peaks=dict(active)
        members=[('D'+str(i),'instrument') for i in range(20)]+[('B'+str(i),'oi') for i in range(12)]
        universe=[dict(symbol=s,pair_id='B-'+s+'_USDT') for s,_ in members]
        def due(u,records,*,attempted=frozenset(),**kw):
            return [(i,s,k) for i,(s,k) in enumerate(members) if (s,k) not in attempted]
        def execute(job,meta,source,prior,**kw):
            lane='coindcx' if job.kind=='instrument' else 'binance'
            with lock:
                active[lane]+=1;peaks[lane]=max(peaks[lane],active[lane])
                if sum(active.values())==32:release.set()
            try:
                self.assertTrue(release.wait(2),'unused provider slots must not prevent full bounded overlap')
                source.calls+=1;return {'done':True}
            finally:
                with lock:active[lane]-=1
        state=Mock();state.read.side_effect=lambda n:{'index':0} if n=='SNAPSHOT_CURSOR' else None
        source=lambda deadline:SimpleNamespace(clock=lambda:NOW,calls=0)
        with patch.object(module,'due_jobs',side_effect=due),patch.object(module,'execute',side_effect=execute):
            result,stats=run_due(SimpleNamespace(info=lambda s:{'used_memory':10000000}),
                'a'*32,universe,{},state,Mock(),source(100),source,{}, {},{'data':{}},'m',
                now=lambda:0,deadline=100,functions={})
        self.assertEqual(len(result),32);self.assertEqual(peaks,{'coindcx':20,'binance':12})
        self.assertEqual(stats['schedule_attempted'],32)

    def test_next_pair_starts_before_slow_first_pair_finishes(self):
        from threading import Event,Lock
        released=Event();lock=Lock();active=[0,0];created=[0]
        def collect(c,r,m,s,b,i,p,prior,**kwargs):
            with lock:active[0]+=1;active[1]=max(active)
            try:
                if m["symbol"]=="0":self.assertTrue(released.wait(2),"a completed worker must be refilled without the slow job")
                if m["symbol"]=="9":released.set()
                return {"last_collected_ms":NOW,"last_hot_ms":NOW,"instrument_ms":NOW,
                    "global_tip_refreshed_ms":NOW,"detail_refreshed_ms":NOW,"legacy_source_ms":NOW,
                    "references_prefetch_until_ms":NOW+1000000,"funding_report":"verified","funding_history":[1],
                    "nft":NOW+1000000,"ls_periods":{h:{"source_ms":NOW} for h in ("1H","4H","24H")}}
            finally:
                with lock:active[0]-=1
        _,stats,_=self.run_wave(collect)
        self.assertEqual(stats["bootstrap_completed"],17)
        self.assertLessEqual(active[1],6,"bootstrap shares the heavy-job cap")

    def test_one_active_job_per_pair_and_dispatcher_reuse(self):
        from threading import Event,Lock
        from pulse.collectors import all_pair_collect as module
        from unittest.mock import Mock
        release=Event();lock=Lock();active=set();seen=[];created=[]
        universe=[dict(symbol=s,pair_id="B-"+s+"_USDT") for s in ("A","B")]
        def due(u,records,*,attempted=frozenset(),**kw):
            return [(i,s,k) for i,(s,k) in enumerate((("A","oi"),("A","instrument"),("B","oi"))) if (s,k) not in attempted]
        def execute(job,meta,source,prior,**kw):
            with lock:
                self.assertNotIn(job.symbol,active);active.add(job.symbol)
            try:
                if (job.symbol,job.kind)==("A","oi"):self.assertTrue(release.wait(2))
                if job.symbol=="B":release.set()
                if job.kind=="instrument":self.assertEqual(prior,{"version":1})
                source.calls+=1;seen.append((job.symbol,job.kind))
                return {"version":(prior or {}).get("version",0)+1}
            finally:
                with lock:active.remove(job.symbol)
        def source(deadline):
            s=SimpleNamespace(clock=lambda:NOW,calls=0);created.append(s);return s
        state=Mock();state.read.side_effect=lambda name:{"index":0} if name=="SNAPSHOT_CURSOR" else None
        with patch.object(module,"due_jobs",side_effect=due),patch.object(module,"execute",side_effect=execute):
            result,stats=module.run_due(SimpleNamespace(info=lambda s:{"used_memory":10000000}),"a"*32,
                universe,{"A":None,"B":None},state,Mock(),source(100),source,{}, {},{"data":{}},"m",
                now=lambda:0,deadline=100,functions={})
        self.assertEqual(result["A"],{"version":2});self.assertEqual(len(seen),3)
        self.assertLessEqual(len(created)-1,3)
        self.assertEqual(stats["metric_source_dispatches"],3)

    def run_wave(self,collect,*,initial=None,used_memory=10000000,derived_error=None,history_paused=False):
        records=dict(initial or {});writes=[]
        class State:
            def read(self,symbol):return records.get(symbol)
            def write(self,symbol,value):records[symbol]=value;writes.append((symbol,value))
        state=State()
        universe=[dict(symbol=str(i),pair_id="B-P"+str(i)+"_USDT",id=i,funding_hours=8) for i in range(17)]
        source=lambda:SimpleNamespace(clock=lambda:NOW,calls=1)
        with patch("pulse.collectors.all_pair_collect.retain_derived",side_effect=derived_error or (lambda c,r,m,v,**kw:
                {**v,"derived":{"current":{"source_ms":NOW},"changes":{}}})):
            result,stats=run_due(SimpleNamespace(info=lambda section:{"used_memory":used_memory,"maxmemory_policy":"noeviction"}),"a"*32,universe,{m["symbol"]:records.get(m["symbol"]) for m in universe},
                state,SimpleNamespace(renew=lambda:None),source(),lambda deadline:source(),
                {},{}, {"data":{}},"sha256:"+"b"*64,now=lambda:0,deadline=100,
                functions={"collect":collect},history_paused=history_paused)
        return result,stats,writes

    def test_history_quota_pause_does_not_retry_history_or_forget_queue(self):
        from pulse.collectors.binance_transport import AdmissionDenied
        attempts=[]
        def denied(*args,**kwargs):
            attempts.append(1)
            error=AdmissionDenied("budget_exhausted:1");error.denied_source="binance";raise error
        _,stats,_=self.run_wave(denied)
        self.assertTrue(stats["history_budget_paused"])
        self.assertIsNone(stats["stopped"])
        self.assertEqual(stats["schedule_due"],17)
        self.assertLessEqual(len(attempts),32)
        _,stats,_=self.run_wave(lambda *a,**kw:self.fail("history retried"),history_paused=True)
        self.assertEqual(stats["schedule_attempted"],0)

    def test_header_refresh_denial_pauses_binance_without_same_cycle_retry(self):
        from pulse.collectors.binance_transport import AdmissionDenied
        attempts=[]
        def denied(*args,**kwargs):
            attempts.append(1)
            error=AdmissionDenied("header_refresh_required");error.denied_source="binance";raise error
        _,stats,writes=self.run_wave(denied)
        self.assertTrue(stats["binance_header_paused"])
        self.assertIsNone(stats["stopped"])
        self.assertEqual(writes,[])
        self.assertEqual(stats["schedule_due"],17)
        self.assertLessEqual(len(attempts),32)

    def test_derived_failure_is_durable_but_never_marks_bootstrap_complete(self):
        result,stats,_=self.run_wave(lambda *args,**kw:{"last_collected_ms":NOW},
            derived_error=ValueError("oi_baseline_integrity_refused"))
        self.assertEqual(stats["bootstrap_completed"],0)
        self.assertEqual(result["0"]["collection_failure"]["reason"],"oi_baseline_integrity_refused")
        self.assertEqual(result["0"]["schedule_retry_ms"]["bootstrap"],NOW+60000)

    def test_successful_bootstrap_is_durable_and_each_job_runs_once(self):
        def collect(c,r,m,s,b,i,p,prior,**kwargs):
            return {"last_collected_ms":NOW,"last_hot_ms":NOW,"instrument_ms":NOW,
                "global_tip_refreshed_ms":NOW,"detail_refreshed_ms":NOW,"funding_refreshed_ms":NOW,
                "legacy_source_ms":NOW,"references_prefetch_until_ms":NOW+1000000,
                "funding_report":"verified","funding_history":[1],"nft":NOW+1000000,
                "ls_periods":{h:{"source_ms":NOW} for h in ("1H","4H","24H")}}
        result,stats,writes=self.run_wave(collect)
        self.assertEqual(stats["bootstrap_completed"],17)
        self.assertEqual(stats["scheduled_jobs"],{"bootstrap":17})
        self.assertEqual(stats["job_time_ms"],{"bootstrap":{"attempts":17,"sum_ms":0,"max_ms":0}})
        self.assertEqual(len(writes),17)
        self.assertEqual(stats["schedule_due"],0)

    def test_bad_rows_preserve_previous_record_and_persist_retry(self):
        def fail(*args,**kwargs):raise ValueError("invalid")
        result,stats,writes=self.run_wave(fail)
        self.assertEqual(stats["bootstrap_completed"],0)
        self.assertEqual(len(stats["failed"]),17)
        self.assertEqual(len(writes),17)
        self.assertEqual(stats["schedule_due"],0)
        self.assertEqual(result["0"]["schedule_retry_ms"]["bootstrap"],NOW+60000)
        self.assertEqual(result["0"]["collection_failure"]["reason"],"invalid")
        self.assertEqual(result["0"]["schedule_first_seen_ms"],NOW)

    def test_failure_metadata_never_copies_arbitrary_provider_text(self):
        def fail(*args,**kwargs):raise ValueError("redis://secret@example.invalid")
        result,_,_=self.run_wave(fail)
        self.assertEqual(result['0']['collection_failure']['reason'],'ValueError')

    def test_admission_denial_stops_without_retry(self):
        from pulse.collectors.binance_transport import AdmissionDenied
        def fail(*args,**kwargs):raise AdmissionDenied("circuit_open")
        result,stats,writes=self.run_wave(fail)
        self.assertEqual(stats["stopped"],"admission:circuit_open")
        self.assertLessEqual(stats["schedule_attempted"],32)
        self.assertEqual(writes,[])

    def test_transport_diagnostics_survive_all_pair_aggregation(self):
        def fail(*args,**kwargs):
            error=TimeoutError()
            error.source_endpoint="candlesticks"
            error.source_timeout_ms=8000
            error.source_elapsed_ms=8001
            raise error
        _,stats,_=self.run_wave(fail)
        self.assertEqual(stats["stopped"],"source_transport_failed")
        self.assertEqual(stats["transport_error_type"],"TimeoutError")
        self.assertEqual(stats["transport_endpoint"],"candlesticks")
        self.assertEqual(stats["source_elapsed_ms"],8001)

    def test_failed_bootstrap_keeps_completed_substeps_but_never_marks_pair_ready(self):
        def partial(c,r,m,s,b,i,p,prior,*,all_mode,checkpoint):
            checkpoint({"cold_refreshed_ms":NOW,"cold_source_ms":NOW-300000})
            raise ValueError("unfinished")
        result,stats,_=self.run_wave(partial)
        self.assertEqual(stats["bootstrap_completed"],0)
        for record in result.values():
            self.assertEqual(record["cold_source_ms"],NOW-300000)
            self.assertEqual(record["schedule_retry_ms"]["bootstrap"],NOW+60000)
            self.assertNotIn("last_collected_ms",record)

    def test_memory_pressure_pauses_bootstrap_and_persists_hysteresis(self):
        def fail(*args,**kwargs):self.fail("bootstrap ran under memory pressure")
        for used,prior,expected in ((110000000,None,"bootstrap_repair_paused"),
                (107000000,{"SCHEDULE_PRESSURE":{"state":"bootstrap_repair_paused"}},"bootstrap_repair_paused"),
                (120000000,None,"hard_stop")):
            with self.subTest(used=used):
                _,stats,writes=self.run_wave(fail,initial=prior,used_memory=used)
                self.assertEqual(stats["memory_pressure"],expected)
                self.assertEqual(stats["bootstrap_completed"],0)
                self.assertTrue(all(name=="SCHEDULE_PRESSURE" for name,_ in writes))
