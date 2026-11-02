import unittest
from pulse.collectors import preview_runtime as rt
from pulse.collectors.binance_transport import AdmissionDenied

UNIVERSE = [dict(id=i, pair_id=f"B-{s}_USDT", symbol=f"{s}USDT", funding_hours=8) for i, s in enumerate(("AAA", "BBB", "CCC"))]


class Pub:
    def __init__(self, *a, busy=False, fail_publish=False):
        self.busy, self.fail_publish = busy, fail_publish
        self.log = []

    def acquire(self):
        if self.busy:
            raise RuntimeError("publication_lease_busy")
        self.log.append("acquire")

    def renew(self):
        self.log.append("renew")

    def release(self):
        self.log.append("release")

    def publish(self, packages, *, expected_revisions):
        self.log.append(("publish", expected_revisions))
        if self.fail_publish:
            raise RuntimeError("publication_promotion_refused:input_revision_conflict")
        return {"status": "published", "channels": [p.channel for p in packages], "artifacts": 7}


class State:
    def __init__(self, records=None):
        self.records = dict(records or {})
        self.writes = []

    def read(self, name):
        return self.records.get(name)

    def read_many(self, names):
        return {name:self.read(name) for name in names}

    def write(self, name, value):
        self.records[name] = value
        self.writes.append(name)


class Src:
    calls = 3

    def __init__(self, *a, **k):
        self.t = 1_800_000_000_000
        self.deadline=k.get("deadline",0)
        self.initialised = False

    def clock(self):
        self.t += 1000
        return self.t

    def initialise(self):
        self.initialised = True


class Doc:
    def __init__(self, client, run_id, channel, mapping):
        self.channel = channel

    def read(self, now_ms):
        return {"revision": 4 if self.channel.startswith("live") else 2}


class Pkg:
    def __init__(self, channel):
        self.channel = channel


class Client:
    closed = False

    def info(self,section):
        assert section=="memory"
        return {"used_memory":10000000}

    def close(self):
        self.closed = True


def deps(**over):
    base = dict(
        parallel=False,
        connect=lambda env: Client(), Sources=Src, Publisher=Pub, State=lambda pub: STATE, Document=Doc,
        load_registry=lambda *a, **k: UNIVERSE, fetch_bulk=lambda s: {"ts": 1, "prices": {}},
        fetch_metadata=lambda s: ({"data": {}, "source_ms": 1}, {"data": {}, "source_ms": 1}),
        collect=lambda client, run_id, meta, src, bulk, info, prem, prior, **kw: {"last_collected_ms": src.clock()},
        prepare=lambda *a, **k: ([Pkg("snapshot:1h:manifest"), Pkg("live_price_v1")], {"status": "prepared", "pairs": 3}),
        prepare_price=lambda universe, pub, batch, now_ms: Pkg("live_price_v1"), sleep=lambda s: None,
        prepare_funding=lambda *args, **kwargs: Pkg("live_funding_v1"),
        collect_ls_tip=lambda client,run_id,meta,src,prior,**kw:{**(prior or {}),"global_tip_refreshed_ms":(stamp:=src.clock()),"ls_windows_refreshed_ms":stamp},
        collect_funding=lambda client,run_id,meta,src,bulk,info,premium,prior:{**(prior or {}),"funding_refreshed_ms":src.clock()},
        collect_references=lambda meta,src,bulk,prior:{**(prior or {}),"opens":[[1,"100"]],"references_prefetch_until_ms":src.clock()+1200000},
        collect_detail=lambda client,run_id,meta,src,prior:{**(prior or {}),"detail_refreshed_ms":src.clock()},
        prepare_histories=lambda *args,**kwargs:[],
        bootstrap_registry=lambda client, run_id, src: {"universe": UNIVERSE, "mapping_version": "sha256:" + "b" * 64},
        now=iter(range(0, 10_000)).__next__)
    base.update(over)
    return base


def heavy(pub):
    """publications that carry the snapshot channels (the fast price path publishes live_price_v1 alone)"""
    return [x for x in pub.log if isinstance(x, tuple) and "snapshot:1h:manifest" in x[1]]


def go(cycles=1, **over):
    global STATE
    STATE = over.pop("state", State())
    pubs = []
    original = over.get("Publisher", Pub)

    def make(*a):
        pub = original(*a) if original is not Pub else Pub()
        pubs.append(pub)
        return pub
    over["Publisher"] = make if "Publisher" not in over else over["Publisher"]
    result = rt.run(run_id="a" * 32, mapping_version="sha256:" + "b" * 64, duration_ms=55000, env={}, deps=deps(**over), max_cycles=cycles)
    return result, pubs, STATE


class RuntimeTests(unittest.TestCase):
    def test_unknown_universe_mode_refuses_before_connect(self):
        from unittest.mock import Mock
        connect=Mock()
        with self.assertRaisesRegex(ValueError,"worker_universe_mode"):
            rt.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
                env={"PULSE_UNIVERSE_MODE":"unknown"},deps={"connect":connect})
        connect.assert_not_called()

    def test_all_mode_resumes_partial_registry_without_collecting_or_publishing(self):
        from unittest.mock import patch
        from pulse.collectors import universe_registry
        state=State();pub=Pub()
        injected=deps(State=lambda p:state,Publisher=lambda *a:pub)
        injected.pop("load_registry");injected.pop("bootstrap_registry")
        with patch.object(universe_registry,"load",side_effect=RuntimeError("registry_unavailable")) as load, \
                patch.object(universe_registry,"bootstrap_step",return_value={"status":"incomplete","completed":8,"total":505}) as step:
            value=rt.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
                env={"PULSE_UNIVERSE_MODE":"all"},deps=injected,max_cycles=1)
        load.assert_called_once();step.assert_called_once()
        self.assertEqual((value["registry"],value["registry_completed"],value["registry_total"]),("building",8,505))
        self.assertEqual(state.writes,[])
        self.assertFalse(heavy(pub))

    def test_reference_repair_precedes_slow_context_and_preserves_progress(self):
        events=[]
        def references(meta,src,bulk,prior):
            events.append("references")
            return {**prior,"opens":[[1,"100"]],"references_prefetch_until_ms":src.clock()+1200000}
        def context(client,run_id,meta,src,bulk,info,prem,prior,**kw):
            self.assertTrue(prior["opens"])
            events.append("context")
            return {**prior,"last_collected_ms":src.clock()}
        result,_,_=go(parallel=True,collect_hot=lambda *args:{},collect_references=references,collect=context)
        self.assertEqual(events,["references"]*3+["context"]*3)
        self.assertEqual(result["references_repaired"],3)

    def test_completed_publication_has_no_deliberate_idle_tail(self):
        result,_,_=go(sleep=lambda seconds:self.fail("must hand off, not idle after publish"))
        self.assertEqual(result["status"],"published")

    def test_hot_cohort_finishes_before_any_context_and_has_separate_counts(self):
        events=[]
        def hot(client,run_id,meta,source,prior):
            events.append(("hot",meta["symbol"]))
            return {"last_hot_ms":source.clock(),"oi_source_ms":source.clock()}
        def collect(client,run_id,meta,source,bulk,info,premium,prior,**kw):
            self.assertTrue(kw["funding_already_checked"])
            events.append(("context",meta["symbol"]))
            return {**prior,"last_collected_ms":source.clock()}
        result,_,state=go(parallel=True,collect_hot=hot,collect=collect)
        self.assertEqual(result["hot_collected"],3)
        self.assertEqual(result["collected"],3)
        self.assertEqual([kind for kind,symbol in events],["hot"]*3+["context"]*3)
        self.assertEqual(result["ls_tip_collected"],3)
        self.assertEqual(len(state.writes),18)

    def test_hot_denial_never_starts_context_collection(self):
        def hot(*args):raise AdmissionDenied("circuit_open")
        result,_,state=go(parallel=True,collect_hot=hot,collect=lambda *args:self.fail("context must not start"))
        self.assertEqual(result["stopped"],"admission:circuit_open")
        self.assertEqual(result["hot_collected"],0)
        self.assertEqual(state.writes,[])

    def test_unique_hot_coverage_survives_a_later_cycle_that_skips_fresh_rows(self):
        def hot(client,run_id,meta,source,prior):return {"last_hot_ms":source.clock(),"oi_source_ms":source.clock()}
        result,_,_=go(cycles=2,parallel=True,collect_hot=hot,
            collect=lambda client,run_id,meta,source,bulk,info,premium,prior,**kw:{**prior,"last_collected_ms":source.clock()})
        self.assertEqual(result["hot_collected"],3)

    def test_happy_path_publishes_with_current_revisions_and_releases(self):
        result, pubs, state = go()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["collected"], 3)
        self.assertEqual(sorted(state.writes), ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
        publish = heavy(pubs[0])[0]
        self.assertEqual(publish[1], {"snapshot:1h:manifest": 2, "live_price_v1": 4})
        self.assertEqual(pubs[0].log[-1], "release")

    def test_lease_busy_does_nothing(self):
        result, pubs, state = go(Publisher=lambda *a: Pub(busy=True))
        self.assertEqual(result, {"status": "lease_busy"})
        self.assertEqual(state.writes, [])

    def test_cold_start_is_incomplete_and_never_publishes_a_partial_universe(self):
        result, pubs, _ = go(prepare=lambda *a, **k: (None, {"status": "incomplete", "pending_pairs": ["CCCUSDT"]}))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["pending_pairs"], ["CCCUSDT"])
        self.assertFalse(heavy(pubs[0]), "no snapshot is published for a partial universe")
        self.assertTrue(result["price_publishes"] >= 1, "the live price needs only the bulk batch and the registry, so it still runs")

    def test_time_budget_stops_the_loop_and_progress_is_kept(self):
        def collect(client, run_id, meta, src, bulk, info, prem, prior):
            if meta["symbol"] == "BBBUSDT":
                raise RuntimeError("worker_source_budget_exhausted")
            return {"last_collected_ms": src.clock()}
        result, pubs, state = go(collect=collect, prepare=lambda *a, **k: (None, {"status": "incomplete", "pending_pairs": ["BBBUSDT", "CCCUSDT"]}))
        self.assertEqual(result["stopped"], "time_budget")
        self.assertEqual(state.writes, ["AAAUSDT"])
        self.assertEqual(result["status"], "incomplete")

    def test_admission_denial_stops_without_retry(self):
        calls = []

        def collect(client, run_id, meta, src, bulk, info, prem, prior):
            calls.append(meta["symbol"])
            raise AdmissionDenied("budget_exhausted:1")
        result, pubs, state = go(collect=collect, prepare=lambda *a, **k: (None, {"status": "incomplete"}))
        self.assertEqual(calls, ["AAAUSDT"])
        self.assertEqual(result["stopped"], "admission:budget_exhausted:1")

    def test_one_bad_pair_never_stops_the_others_and_is_never_marked_complete(self):
        def collect(client, run_id, meta, src, bulk, info, prem, prior):
            if meta["symbol"] == "BBBUSDT":
                raise ValueError("pair_ticker_identity")
            return {"last_collected_ms": src.clock()}
        result, pubs, state = go(collect=collect, prepare=lambda *a, **k: (None, {"status": "incomplete"}))
        self.assertEqual(result["failed"], {"BBBUSDT": "pair_ticker_identity"})
        self.assertEqual(sorted(state.writes), ["AAAUSDT", "CCCUSDT"])

    def test_fenced_out_is_not_swallowed(self):
        def collect(*a, **k):
            raise RuntimeError("publication_fenced_out")
        with self.assertRaises(RuntimeError):
            go(collect=collect)

    def test_oldest_first_and_recent_pairs_are_skipped(self):
        order = []
        base = 1_800_000_000_000
        state = State({"AAAUSDT": {"last_collected_ms": base + 500}, "BBBUSDT": {"last_collected_ms": 5}, "CCCUSDT": None})

        def collect(client, run_id, meta, src, bulk, info, prem, prior):
            order.append(meta["symbol"])
            return {"last_collected_ms": src.clock()}
        result, _, _ = go(state=state, collect=collect)
        self.assertEqual(order, ["CCCUSDT", "BBBUSDT"], "never-collected first, then oldest; AAA is fresh and skipped")
        self.assertEqual(result["skipped"], 1)

    def test_source_failure_before_inputs_leaves_the_last_pointer_untouched(self):
        def broken(s):
            raise ValueError("bulk_shape")
        result, pubs, state = go(fetch_bulk=broken)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stopped"], "bulk_shape")
        self.assertFalse([x for x in pubs[0].log if isinstance(x, tuple)], "nothing is published without the shared inputs")
        self.assertEqual(pubs[0].log[-1], "release")

    def test_publish_refusal_propagates_and_still_releases(self):
        pubs = []

        def make(*a):
            pub = Pub(fail_publish=True)
            pubs.append(pub)
            return pub
        with self.assertRaises(RuntimeError):
            go(Publisher=make)
        self.assertEqual(pubs[0].log[-1], "release")

    def test_compose_failure_drains_live_thread_before_releasing_and_closing(self):
        import threading
        import time
        entered = threading.Event()
        pubs = []
        class CheckedPub(Pub):
            def release(self):
                self.live_at_release = any(t.name == "pulse-price-ticks" and t.is_alive() for t in threading.enumerate())
                super().release()
        def make(*args):
            pub = CheckedPub()
            pubs.append(pub)
            return pub
        def price(*args, **kwargs):
            entered.set()
            time.sleep(.05)
            return Pkg("live_price_v1")
        def broken(*args, **kwargs):
            self.assertTrue(entered.wait(1))
            raise RuntimeError("publication_promotion_refused:invalid_time")
        with self.assertRaises(RuntimeError) as caught:
            go(Publisher=make, prepare=broken, prepare_price=price)
        self.assertEqual(caught.exception.worker_stage, "compose")
        self.assertFalse(pubs[0].live_at_release)

    def test_loop_publishes_several_times_within_the_budget_and_stops_when_time_runs_out(self):
        clock = iter(range(0, 10_000, 3)).__next__          # advancing clock includes history timing reads
        result, pubs, state = go(cycles=None, now=clock)
        self.assertGreaterEqual(result["publishes"], 2)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["cycles"], result["publishes"])
        self.assertEqual(len(heavy(pubs[0])), result["publishes"])

    def test_history_failure_does_not_rollback_published_table(self):
        def failed(*args,**kwargs):raise ValueError("history_capsule_identity")
        result,_,_=go(prepare_histories=failed)
        self.assertEqual(result["status"],"published")
        self.assertEqual(result["history_error"],"history_capsule_identity")

    def test_history_runs_before_slow_context_can_exhaust_the_budget(self):
        events=[]
        def history(*args,**kwargs):events.append("history");return []
        def context(*args,**kwargs):events.append("context");return {"last_collected_ms":1}
        go(prepare_histories=history,collect=context)
        self.assertEqual(events[0],"history")
        self.assertIn("context",events)

    def test_loop_stops_after_a_cycle_that_did_not_publish(self):
        calls = []

        def prepare(*a, **k):
            calls.append(1)
            return (None, {"status": "incomplete", "pending_pairs": ["CCCUSDT"]})
        result, pubs, _ = go(cycles=None, prepare=prepare, now=iter(range(0, 10_000, 2)).__next__)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "incomplete")

    def test_admission_reason_is_reported_not_just_a_generic_message(self):
        def init_fail():
            raise AdmissionDenied("header_probe_busy")

        class S(Src):
            def initialise(self):
                init_fail()
        result, _, _ = go(Sources=S)
        self.assertEqual(result["stopped"], "admission:header_probe_busy")

    def test_price_ticks_run_on_their_own_thread_while_pairs_are_collected(self):
        import time as real_time
        seen = []

        def slow_collect(client, run_id, meta, src, bulk, info, prem, prior):
            real_time.sleep(0.05)               # pair collection takes time; the ticker must not wait for it
            seen.append(meta["symbol"])
            return {"last_collected_ms": src.clock()}
        result, pubs, _ = go(collect=slow_collect)
        self.assertGreaterEqual(result["price_publishes"], 1)
        price_publishes = [x for x in pubs[0].log if isinstance(x, tuple) and set(x[1]) == {"live_price_v1"}]
        self.assertGreaterEqual(len(price_publishes), 1)
        self.assertEqual(sorted(seen), ["AAAUSDT", "BBBUSDT", "CCCUSDT"])

    def test_funding_keeps_publishing_during_slow_collection_and_refreshes_due_metadata(self):
        import threading
        import time
        from unittest.mock import patch
        metadata_calls=[]
        def metadata(source):
            if threading.current_thread().name=="pulse-price-ticks":metadata_calls.append(1)
            return ({"data":{},"source_ms":1},{"source_ms":1,"data":{
                meta["symbol"]:{"nextFundingTime":source.clock()-1} for meta in UNIVERSE}})
        def slow(*args):
            time.sleep(.1)
            return {"last_collected_ms":1}
        with patch.object(rt,"PRICE_PERIOD_MS",1),patch.object(rt,"FUNDING_PERIOD_MS",1):
            result,_,_=go(fetch_metadata=metadata,collect=slow)
        self.assertGreaterEqual(result["funding_publishes"],2)
        self.assertGreaterEqual(len(metadata_calls),2)

    def test_a_failing_price_tick_never_aborts_the_pass_but_a_fenced_owner_does(self):
        def broken(universe, pub, batch, now_ms):
            raise ValueError("ticker_identity_mismatch")
        result, _, state = go(prepare_price=broken)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["price_error"], "ticker_identity_mismatch")
        self.assertEqual(result["collected"], 3)

        def fenced(universe, pub, batch, now_ms):
            raise RuntimeError("publication_fenced_out")
        with self.assertRaises(RuntimeError):
            go(prepare_price=fenced)

    def test_lapsed_registry_is_rebuilt_in_the_pass_and_collection_resumes_next_time(self):
        calls = []

        def load(*a, **k):
            raise RuntimeError("registry_unavailable")
        result, pubs, state = go(load_registry=load, bootstrap_registry=lambda c, r, s: calls.append(1) or {"universe": UNIVERSE, "mapping_version": "sha256:" + "b" * 64})
        self.assertEqual(calls, [1])
        self.assertEqual(result["registry"], "rebuilt")
        self.assertEqual(state.writes, [], "the pass ends after the rebuild; pairs are collected on the next pass")

    def test_rebuilt_registry_with_a_changed_mapping_is_refused(self):
        def load(*a, **k):
            raise RuntimeError("registry_unavailable")
        result, _, state = go(load_registry=load, bootstrap_registry=lambda c, r, s: {"universe": UNIVERSE, "mapping_version": "sha256:" + "c" * 64})
        self.assertEqual(result["stopped"], "registry_mapping_conflict")
        self.assertEqual(state.writes, [])

    def test_live_envelope_constant_matches_the_contract_bound(self):
        # a 60 s funding envelope once reached production and failed every pass with live_envelope_expiry
        from pulse.collectors.preview_compose import LIVE_ENVELOPE_MS
        from pulse.wire.semantics import validate_live
        universe = [dict(id=0, pair_id="B-AAA_USDT", symbol="AAAUSDT", funding_hours=8)]
        from pulse.wire.identity import digest
        mapping = digest(universe)
        base = {"schema": "pulse.live_funding.v1", "mapping_version": mapping, "sequence": 1, "generated_ms": 1_800_000_000_000,
                "rows": [{"id": 0}], "inputs": {}}
        try:
            validate_live({**base, "expires_ms": base["generated_ms"] + LIVE_ENVELOPE_MS}, universe, mapping)
        except (ValueError, KeyError, TypeError) as exc:
            # later row checks may refuse this skeleton document; only the envelope rule matters here
            self.assertNotEqual(str(exc), "live_envelope_expiry")
        with self.assertRaises(ValueError) as caught:
            validate_live({**base, "expires_ms": base["generated_ms"] + LIVE_ENVELOPE_MS + 1}, universe, mapping)
        self.assertEqual(str(caught.exception), "live_envelope_expiry")


if __name__ == "__main__":
    unittest.main()
