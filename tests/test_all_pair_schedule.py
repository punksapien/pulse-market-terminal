from unittest import TestCase
from pulse.collectors.all_pair_schedule import due_jobs,next_wave,reference_due,REFERENCE_REFRESH_MS

NOW=1800000000000


class ScheduleTests(TestCase):
    def test_offline_reference_benchmark_preserves_membership_and_dispersion(self):
        from tools.probes.pulse_reference_phase_benchmark import benchmark
        result=benchmark(['PAIR'+str(i) for i in range(350)],NOW)
        self.assertEqual(sum(result['minute_counts'].values()),350)
        self.assertLess(result['phased_peak_pairs_per_minute'],40)
        self.assertEqual(result['legacy_peak_pairs_per_minute'],350)
        for symbols in ([],['A']*350):
            with self.assertRaisesRegex(ValueError,'membership'):benchmark(symbols,NOW)

    def test_reference_phases_spread_seeded_burst_without_sliding_or_late_coverage(self):
        from collections import Counter
        record={'last_collected_ms':NOW,'references_refreshed_ms':NOW,
                'references_prefetch_until_ms':NOW+22*60000,'opens':[[NOW-86400000,'1']]}
        bins=Counter()
        for i in range(350):
            symbol='PAIR'+str(i);due=reference_due(symbol,record)
            self.assertTrue(NOW<due<=NOW+REFERENCE_REFRESH_MS)
            self.assertLess(due,record['references_prefetch_until_ms']-60000)
            self.assertEqual(reference_due(symbol,dict(record)),due)
            bins[(due-NOW)//60000]+=1
            later={**record,'references_refreshed_ms':due+2000,
                   'references_prefetch_until_ms':due+22*60000}
            self.assertEqual(reference_due(symbol,later),due+REFERENCE_REFRESH_MS)
        self.assertLess(max(bins.values()),40,'350-pair burst must not become one minute of work')
        due=reference_due('A',record);r={**record,'schedule_retry_ms':{'references':due+60000}}
        self.assertNotIn('references',{x[2] for x in due_jobs([{'symbol':'A'}],{'A':r},now_ms=due)})

    def test_incomplete_reference_history_keeps_repair_due_and_validates_clocks(self):
        for record in ({},{'references_prefetch_until_ms':NOW+60000},
                       {'opens':[[1,'1']],'references_refreshed_ms':NOW,'references_prefetch_until_ms':NOW+60000}):
            self.assertEqual(reference_due('A',record),max(0,record.get('references_prefetch_until_ms',0)-120000))
        for record in ({'references_prefetch_until_ms':True},{'references_refreshed_ms':-1}):
            with self.assertRaisesRegex(ValueError,'schedule_timestamp'):reference_due('A',record)

    def test_partial_bootstrap_does_not_starve_verified_components(self):
        for fields,independent in (({},False),({'oi_source_ms':NOW-120000},False),
                ({'instrument':{}},False),({'oi_source_ms':NOW-120000,'instrument':{}},True)):
            record={'schedule_first_seen_ms':NOW-500000,**fields}
            jobs=due_jobs([{'symbol':'DODOXUSDT'}],{'DODOXUSDT':record},now_ms=NOW)
            kinds={j[2] for j in jobs}
            self.assertIn('bootstrap',kinds)
            self.assertEqual('oi' in kinds,independent)
            self.assertNotIn('last_collected_ms',record)
            if independent:
                self.assertTrue({'instrument','funding','global','detail'}<=kinds)
                record['schedule_retry_ms']={'oi':NOW+60000}
                self.assertNotIn('oi',{j[2] for j in due_jobs([{'symbol':'DODOXUSDT'}],{'DODOXUSDT':record},now_ms=NOW)})
    def test_metadata_due_at_sixty_seconds_reserves_unchanged_freshness(self):
        u=[{"symbol":"A"}];r={"A":{"last_collected_ms":NOW,"instrument_ms":NOW}}
        for age,expected in ((59999,False),(60000,True),(90000,True)):
            jobs=due_jobs(u,r,now_ms=NOW+age)
            self.assertEqual("instrument" in {j[2] for j in jobs},expected)
    def test_reserved_reference_wave_preserves_general_work_and_retry_floors(self):
        import heapq
        heap=[(NOW-10000,"A","legacy"),(NOW-100,"B","references"),
              (NOW-90,"C","references"),(NOW-9000,"D","oi")]
        heapq.heapify(heap)
        wave=next_wave(heap,limit=16,homogeneous=True,references_first=True)
        self.assertEqual([j.symbol for j in wave],["B","C"])
        self.assertEqual([(j.symbol,j.kind) for j in next_wave(heap,limit=16,homogeneous=True)],[("A","legacy")])
        self.assertEqual(heap,[(NOW-9000,"D","oi")])
        with self.assertRaises(ValueError):next_wave([],references_first=1)
    def test_missing_jobs_age_from_stable_record_anchor_and_keep_retry_floor(self):
        universe=[{"symbol":"A"}]
        record={"last_collected_ms":NOW-1000000}
        for stamp in (NOW,NOW+600000):
            jobs=due_jobs(universe,{"A":record},now_ms=stamp)
            period=next(v for v in jobs if v[2]=="period_1H")
            self.assertEqual(period[0],NOW-1000000)
        record={"schedule_first_seen_ms":NOW-1000000,"schedule_retry_ms":{"bootstrap":NOW+60000}}
        self.assertEqual(due_jobs(universe,{"A":record},now_ms=NOW),[])
        self.assertEqual(due_jobs(universe,{"A":record},now_ms=NOW+120000),[(NOW+60000,"A","bootstrap")])
    def test_homogeneous_wave_takes_oldest_kind_without_redating_others(self):
        import heapq
        heap=[(NOW-100,"A","legacy"),(NOW-90,"B","oi"),(NOW-80,"C","legacy"),
              (NOW-70,"D","oi")]
        heapq.heapify(heap)
        wave=next_wave(heap,limit=16,homogeneous=True)
        self.assertEqual([(j.symbol,j.kind,j.due_ms) for j in wave],
            [("A","legacy",NOW-100),("C","legacy",NOW-80)])
        self.assertEqual(set(heap),{(NOW-90,"B","oi"),(NOW-70,"D","oi")})
        self.assertEqual(len(next_wave(heap,limit=16,homogeneous=True)),2)
        with self.assertRaises(ValueError):next_wave([],homogeneous=1)

    def test_all_pairs_have_one_bootstrap_and_bounded_unique_waves(self):
        universe=[{"symbol":str(i)} for i in range(505)]
        heap=due_jobs(universe,{},now_ms=NOW);seen=[]
        while heap:
            wave=next_wave(heap)
            self.assertLessEqual(len(wave),8)
            seen.extend(j.symbol for j in wave)
        self.assertEqual(len(seen),505)
        self.assertEqual(len(set(seen)),505)

    def test_overdue_existing_work_precedes_missing_bootstrap(self):
        universe=[{"symbol":"A"},{"symbol":"B"}]
        records={"A":{"last_collected_ms":NOW-1000000,"last_hot_ms":NOW-61000,
            "instrument_ms":NOW,"global_tip_refreshed_ms":NOW,"detail_refreshed_ms":NOW,
            "legacy_source_ms":NOW,"references_prefetch_until_ms":NOW+1000000,
            "funding_report":"verified","funding_history":[1],"nft":NOW+1000000,
            "ls_periods":{h:{"source_ms":NOW} for h in ("1H","4H","24H")}}}
        wave=next_wave(due_jobs(universe,records,now_ms=NOW))
        self.assertEqual(wave[0].kind,"oi")
        self.assertEqual(len({j.symbol for j in wave}),len(wave))

    def test_periods_due_only_after_source_bucket_and_retry_survives_restart(self):
        record={"last_collected_ms":NOW,"ls_periods":{
            h:{"source_ms":NOW} for h in ("1H","4H","24H")}}
        universe=[{"symbol":"A"}]
        jobs=due_jobs(universe,{"A":record},now_ms=NOW+3600000)
        kinds={item[2] for item in jobs}
        self.assertIn("period_1H",kinds)
        self.assertNotIn("period_4H",kinds)
        record["schedule_retry_ms"]={"period_1H":NOW+3660000}
        jobs=due_jobs(universe,{"A":record},now_ms=NOW+3600000)
        self.assertNotIn("period_1H",{item[2] for item in jobs})
        self.assertEqual(jobs,due_jobs(universe,{"A":record},now_ms=NOW+3600000))

    def test_attempted_jobs_are_not_retried_in_same_pass(self):
        universe=[{"symbol":"A"}]
        self.assertEqual(due_jobs(universe,{},now_ms=NOW,attempted={("A","bootstrap")}),[])
        with self.assertRaises(ValueError):next_wave([],limit=17)
        with self.assertRaises(ValueError):due_jobs(universe*2,{},now_ms=NOW)

    def test_unpublished_bootstrap_priority_does_not_change_due_times_or_live_policy(self):
        import heapq
        heap=[(NOW-100000,"A","oi"),(NOW,"B","bootstrap"),(NOW,"C","bootstrap")]
        heapq.heapify(heap)
        selected=next_wave(heap,limit=1,bootstrap_first=True)
        self.assertEqual([(j.symbol,j.kind,j.due_ms) for j in selected],[("B","bootstrap",NOW)])
        self.assertEqual(set(heap),{(NOW-100000,"A","oi"),(NOW,"C","bootstrap")})
        self.assertEqual(next_wave(heap,limit=1)[0].kind,"oi")
        with self.assertRaises(ValueError):next_wave([],bootstrap_first=1)

    def test_sixteen_wave_still_has_one_job_per_pair(self):
        universe=[{"symbol":str(i)} for i in range(20)]
        wave=next_wave(due_jobs(universe,{},now_ms=NOW),limit=16)
        self.assertEqual(len(wave),16)
        self.assertEqual(len({j.symbol for j in wave}),16)

    def test_verified_funding_waits_for_cycle_not_fifteen_minute_age(self):
        universe=[{"symbol":"A"}]
        record={"last_collected_ms":NOW-2000000,"funding_refreshed_ms":NOW-2000000,
            "funding_report":"verified","funding_history":[1],"nft":NOW+1000000}
        jobs=due_jobs(universe,{"A":record},now_ms=NOW,next_funding={"A":NOW+1000000})
        self.assertNotIn("funding",{v[2] for v in jobs})
        jobs=due_jobs(universe,{"A":record},now_ms=NOW,next_funding={"A":NOW+2000000})
        self.assertIn("funding",{v[2] for v in jobs})
