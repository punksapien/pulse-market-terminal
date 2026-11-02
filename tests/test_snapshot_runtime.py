from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch

from pulse.collectors import snapshot_runtime as module
from pulse.collectors.preview_compose import prepare_funding,prepare,prepare_price
from pulse.wire.publication import byte_digest
from tests import test_preview_funding_lane as funding_fixture


class SnapshotRuntimeTests(TestCase):
    def test_funding_absence_uses_only_verified_price_and_expired_empty_metadata(self):
        now,universe,records,publisher,bulk,info,premium=funding_fixture.FundingLaneTests().fixture(4)
        publication=prepare_price(universe,publisher,bulk,now_ms=now)
        bodies=dict(publication.blobs)
        for call in publisher.register_source.call_args_list:bodies[byte_digest(call.args[1])]=call.args[1]
        artifacts=Mock();artifacts.blob.side_effect=lambda key:bodies[key]
        def document(client,run,channel,mapping):
            value=Mock();value.read.return_value=({"status":"unavailable"} if channel=="live_funding_v1"
                else {"status":"available","payload":publication.pointer})
            return value
        with patch.object(module,"RuntimeDocument",side_effect=document),patch.object(module,"PublishedArtifacts",return_value=artifacts):
            self.assertEqual(module.shared_inputs(None,"a"*32,publisher.mapping_version,universe,now_ms=now),
                (bulk,{"data":{},"source_ms":0},{"data":{},"source_ms":0}))
            artifacts.blob.side_effect=lambda key:b"tampered"
            with self.assertRaisesRegex(ValueError,"snapshot_live_hash_refused"):
                module.shared_inputs(None,"a"*32,publisher.mapping_version,universe,now_ms=now)

    def test_heartbeat_stops_on_lease_loss_without_retry(self):
        publisher=Mock();publisher.renew.side_effect=RuntimeError("secret must never escape")
        stop=Mock();stop.wait.return_value=False
        errors=[]
        module.renew_while_owned(publisher,stop,errors)
        self.assertEqual(errors,["publication_heartbeat_failed"])
        publisher.renew.assert_called_once();stop.wait.assert_called_once_with(5)

    def test_heartbeat_shutdown_performs_no_renewal(self):
        publisher=Mock();stop=Mock();stop.wait.return_value=True
        module.renew_while_owned(publisher,stop,[])
        publisher.renew.assert_not_called()

    def test_schema_diagnostic_never_includes_rejected_value(self):
        from pulse.wire.validation import snapshot
        with self.assertRaisesRegex(ValueError,"^snapshot_schema:$"):
            snapshot({"secret":"redis://must-not-appear"})

    def test_approved_partial_publication_preserves_pending_member(self):
        _,publisher,state,d=self.rig(missing=True)
        state.read.return_value=None
        result=module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
            env={"PULSE_UNIVERSE_MODE":"all","PULSE_PER_FIELD_PUBLICATION_APPROVED":"1"},deps=d)
        self.assertEqual(result["pending_pairs"],1)
        self.assertEqual(result["pairs"],1)
        self.assertEqual(publisher.publish.call_count,3)
        self.assertTrue(all(c.kwargs["allow_partial"] for c in d["prepare"].call_args_list))
        self.assertEqual(d["prepare"].call_args.kwargs["cached"],{"BTCUSDT":None})

    def test_partial_witness_does_not_redate_retained_source(self):
        from pulse.collectors.preview_compose import witness_clock
        self.assertEqual(witness_clock({"instrument_ms":12,"last_hot_ms":20},100),12)
        self.assertEqual(witness_clock({},100),100)
        self.assertEqual(witness_clock({"last_collected_ms":9},100),9)

    def rig(self,missing=False):
        client=Mock();publisher=Mock();state=Mock()
        universe=[{"symbol":"BTCUSDT"}]
        state.read_many.return_value={"BTCUSDT":None if missing else {"last_collected_ms":1,"derived":{}}}
        state.read.return_value={"index":0}
        def compose(*args,**kwargs):
            return [SimpleNamespace(channel="snapshot:"+horizon.lower()+":manifest",allocation_bytes=123)
                for horizon in kwargs["horizons"]],{}
        d=dict(connect=Mock(return_value=client),Publisher=Mock(return_value=publisher),
            State=Mock(return_value=state),load=Mock(return_value=universe),inputs=Mock(return_value=({}, {}, {})),
            prepare=Mock(side_effect=compose),Document=Mock(return_value=SimpleNamespace(read=lambda **kw:{"revision":0})),
            clock=lambda c:1800000000000,now=lambda:0)
        return client,publisher,state,d

    def test_three_atomic_horizons_use_separate_lane_and_zero_sources(self):
        client,publisher,state,d=self.rig()
        result=module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
            env={"PULSE_UNIVERSE_MODE":"all"},deps=d)
        self.assertEqual(result["horizons"],["1H","4H","24H"])
        self.assertEqual(result["source_dispatches"],0)
        self.assertEqual(publisher.publish.call_count,3)
        self.assertEqual(d["prepare"].call_count,1)
        self.assertEqual(d["prepare"].call_args.kwargs["horizons"],("1H","4H","24H"))
        self.assertTrue(all(len(c.args[0])==1 for c in publisher.publish.call_args_list))
        self.assertEqual([c.args[1]["index"] for c in state.write.call_args_list],[1,2,0])
        self.assertEqual(d["Publisher"].call_args.kwargs,{"lane":"snapshot"})
        client.close.assert_called_once();publisher.release.assert_called_once()

    def test_incomplete_bootstrap_never_composes_or_reads_live_inputs(self):
        client,publisher,state,d=self.rig(missing=True)
        result=module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
            env={"PULSE_UNIVERSE_MODE":"all"},deps=d)
        self.assertEqual(result["pending_pairs"],1)
        d["prepare"].assert_not_called();d["inputs"].assert_not_called();publisher.publish.assert_not_called()

    def test_stream_promotes_before_next_compose_and_stops_before_budget(self):
        _,publisher,state,d=self.rig();wall=[0];events=[]
        def prepare(*args,**kwargs):
            self.assertTrue(kwargs["stream"])
            def packages():
                try:
                    for h in kwargs["horizons"]:
                        events.append("compose:"+h);wall[0]+=19
                        yield SimpleNamespace(channel="snapshot:"+h.lower()+":manifest",allocation_bytes=123)
                finally:events.append("closed")
            return packages(),{}
        publisher.publish.side_effect=lambda packages,**kw:events.append("publish:"+packages[0].channel)
        d.update(prepare=prepare,now=lambda:wall[0])
        result=module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
            env={"PULSE_UNIVERSE_MODE":"all"},deps=d)
        self.assertEqual(result["horizons"],["1H","4H"])
        self.assertEqual(events,["compose:1H","publish:snapshot:1h:manifest",
            "compose:4H","publish:snapshot:4h:manifest","closed"])
        self.assertEqual(state.write.call_args.args,("SNAPSHOT_CURSOR",{"index":2}))

    def test_wrong_stream_channel_is_never_promoted(self):
        _,publisher,state,d=self.rig()
        d["prepare"]=lambda *a,**kw:(iter([SimpleNamespace(channel="live_price_v1")]),{})
        with self.assertRaisesRegex(ValueError,"snapshot_package_horizons_refused"):
            module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
                env={"PULSE_UNIVERSE_MODE":"all"},deps=d)
        publisher.publish.assert_not_called();state.write.assert_not_called()

    def test_initial_publication_waits_for_genuine_current_oi(self):
        _,publisher,state,d=self.rig()
        state.read.return_value=None
        result=module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
            env={"PULSE_UNIVERSE_MODE":"all"},deps=d)
        self.assertEqual(result["pending_refresh_pairs"],1)
        publisher.publish.assert_not_called();d["inputs"].assert_not_called()

    def test_initial_expiry_guard_boundaries(self):
        now=1800000000000
        good={"derived":{"current":{"status":"available","source_ms":now,"expires_ms":now+55001}}}
        self.assertEqual(module.initial_oi_pending({"A":good},now_ms=now,reserve_ms=55000),[])
        for change in ({"expires_ms":now+55000},{"source_ms":now+5001},
                       {"source_ms":True},{"expires_ms":"future"},{"status":"unavailable"}):
            row={"derived":{"current":{**good["derived"]["current"],**change}}}
            self.assertEqual(module.initial_oi_pending({"A":row},now_ms=now,reserve_ms=55000),["A"])

    def test_pending_diagnostics_are_bounded_and_never_return_raw_records(self):
        records={'A':dict(instrument={'private':'never'},last_hot_ms=1,
            collection_failure={'job':'bootstrap','reason':'funding_interval_unsupported','unsafe':'redis://secret'})}
        value=module.pending_details(records,['A'])
        self.assertEqual(value[0]['present'],['instrument'])
        self.assertEqual(value[0]['failure'],{'job':'bootstrap','reason':'funding_interval_unsupported'})
        self.assertNotIn('secret',str(value));self.assertNotIn('private',str(value))

    def test_partial_completion_retains_cursor_for_next_horizon(self):
        client,publisher,state,d=self.rig()
        wall=[0]
        def compose(*args,**kwargs):
            wall[0]=45
            return [SimpleNamespace(channel="snapshot:"+h.lower()+":manifest",allocation_bytes=123)
                for h in kwargs["horizons"]],{}
        def promote(*args,**kwargs):wall[0]=53
        publisher.publish.side_effect=promote
        d.update(prepare=compose,now=lambda:wall[0])
        result=module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
            env={"PULSE_UNIVERSE_MODE":"all"},deps=d)
        self.assertEqual(result["horizons"],["1H"])
        state.write.assert_called_once_with("SNAPSHOT_CURSOR",{"index":1})

    def test_over_budget_preparation_never_promotes_or_advances_cursor(self):
        _,publisher,state,d=self.rig();wall=[0];old=d["prepare"]
        def prepare(*a,**kw):
            packages,status=old(*a,**kw);wall[0]=54;return packages,status
        d.update(prepare=prepare,now=lambda:wall[0])
        result=module.run(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000,
            env={"PULSE_UNIVERSE_MODE":"all"},deps=d)
        self.assertEqual(result["horizons"],[]);publisher.publish.assert_not_called();state.write.assert_not_called()

    def test_shared_inputs_are_hash_pinned_and_keep_original_clocks(self):
        args=funding_fixture.FundingLaneTests().fixture(4)
        now,universe,records,publisher,bulk,info,premium=args
        p=prepare_funding(universe,records,publisher,bulk,info,premium,now_ms=now)
        bodies=dict(p.blobs)
        for call in publisher.register_source.call_args_list:
            bodies[byte_digest(call.args[1])]=call.args[1]
        artifacts=Mock();artifacts.blob.side_effect=lambda key:bodies[key]
        doc=Mock();doc.read.return_value={"status":"available","payload":p.pointer}
        with patch.object(module,"RuntimeDocument",return_value=doc),patch.object(module,"PublishedArtifacts",return_value=artifacts):
            result=module.shared_inputs(None,"a"*32,p.mapping_version,universe,now_ms=now)
            self.assertEqual(result,(bulk,info,premium))
            artifacts.blob.side_effect=lambda key:b"tampered"
            with self.assertRaisesRegex(ValueError,"snapshot_live_hash_refused"):
                module.shared_inputs(None,"a"*32,p.mapping_version,universe,now_ms=now)
            doc.read.return_value={"status":"unavailable"}
            with self.assertRaisesRegex(RuntimeError,"snapshot_shared_inputs_unavailable"):
                module.shared_inputs(None,"a"*32,p.mapping_version,universe,now_ms=now)

    def test_invalid_horizon_selection_refuses_before_store_access(self):
        for horizons in ((),("bad",),("1H","1H")):
            with self.assertRaisesRegex(ValueError,"compose_horizons_refused"):
                prepare(None,None,None,None,None,None,None,None,now_ms=0,horizons=horizons)
