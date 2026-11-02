"""Real composer price bridge, not just the isolated row adapters."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from pulse.collectors import preview_compose as module
from pulse.wire.identity import digest


class ComposerPriceTests(TestCase):
    def test_stream_defers_each_full_composer_and_accounts_consumed_bytes(self):
        now=1800000000000
        meta=dict(id=0,pair_id="B-DODOX_USDT",symbol="DODOXUSDT",funding_hours=4)
        publisher=Mock(mapping_version=digest([meta]))
        publisher.register_source.return_value=(1,"sha256:"+"b"*64)
        bulk={"ts":now,"prices":{}}
        full=Mock(return_value=SimpleNamespace(allocation_bytes=7))
        with patch.multiple(module,stores=Mock(return_value=()),
                oi_values=Mock(return_value=({"status":"unavailable","reason":"source_unavailable"},{},{})),
                read_metrics=Mock(return_value={}),asdict_ls=Mock(return_value={}),add_ls=Mock(),compose=full):
            packages,status=module.prepare(None,"0"*32,[meta],Mock(),publisher,bulk,
                {"data":{},"source_ms":now},{"data":{},"source_ms":now},now_ms=now,
                cached={"DODOXUSDT":None},allow_partial=True,stream=True)
            full.assert_not_called();self.assertEqual(status["prepared_bytes"],0)
            next(packages)
            self.assertEqual(full.call_count,1);self.assertEqual(status["prepared_bytes"],7)
            self.assertIs(full.call_args.kwargs["validate"],__import__("pulse.wire.validation",fromlist=["snapshot"]).snapshot)
            packages.close()
            self.assertEqual(full.call_count,1)

    def test_partial_member_keeps_fresh_bulk_without_inventing_metrics(self):
        now=1800000000000
        meta=dict(id=0,pair_id="B-DODOX_USDT",symbol="DODOXUSDT",funding_hours=4)
        publisher=Mock(mapping_version=digest([meta]))
        publisher.register_source.return_value=(1,"sha256:"+"b"*64)
        bulk={"ts":now,"prices":{meta["pair_id"]:{"ls":"1.25","mkt":"DODOXUSDT","btST":now,"v":"1234"}}}
        built=[]
        def build(universe,rows,inputs,**kwargs):
            built.extend(rows)
            return module_build(universe,rows,inputs,**kwargs)
        module_build=module.build
        with patch.multiple(module,stores=Mock(return_value=()),
                oi_values=Mock(return_value=({"status":"unavailable","reason":"source_unavailable"},{},{})),
                read_metrics=Mock(return_value={}),asdict_ls=Mock(return_value={}),
                add_ls=Mock(),build=build,compose=Mock(return_value=SimpleNamespace(allocation_bytes=1))):
            packages,status=module.prepare(None,"0"*32,[meta],Mock(),publisher,bulk,
                {"data":{},"source_ms":now},{"data":{},"source_ms":now},now_ms=now,
                cached={"DODOXUSDT":None},allow_partial=True)
        self.assertEqual(len(packages),3)
        for row in built:
            self.assertEqual(row["price"]["value"],"1.25")
            self.assertEqual(row["volume"]["quote_24h"]["value"],"1234")
            self.assertEqual(row["oi"]["change_24h"]["status"],"unavailable")
            self.assertIsNone(row["oi"]["change_24h"]["value"])
            self.assertEqual(row["funding"]["native_estimate"]["status"],"unavailable")

    def test_real_composer_keeps_price_volume_and_exact_minute_references(self):
        now = 1800000000000
        pair = "B-BTC_USDT"
        meta = dict(id=0, pair_id=pair, symbol="BTCUSDT", funding_hours=4)
        record = dict(last_collected_ms=now, instrument={}, instrument_ms=now,
                      opens=[[now - window, "100"] for window in module.WINDOW_MS.values()])
        state = Mock()
        state.read.return_value = record
        state.read_many.return_value = {"BTCUSDT": record}
        publisher = Mock(mapping_version=digest([meta]))
        publisher.register_source.return_value = (1, "sha256:" + "b" * 64)
        bulk = {"ts": now, "prices": {pair: {"ls": "107.125", "mkt": "BTCUSDT", "btST": now, "v": "1234"}}}
        built = []
        real_build = module.build

        def build(universe, rows, inputs, *, horizon, now_ms,**kwargs):
            built.extend(rows)
            return real_build(universe, rows, inputs, horizon=horizon, now_ms=now_ms,**kwargs)

        with patch.multiple(module, stores=Mock(return_value=()),
                oi_values=Mock(return_value=({}, {}, {})), read_metrics=Mock(return_value={}),
                asdict_ls=Mock(return_value={}), inputs=Mock(side_effect=ValueError("unavailable")),
                add_oi=Mock(), add_ls=Mock(), add_funding=Mock(), build=build,
                live=Mock(), compose_live_price=Mock(return_value={}),
                compose=Mock(return_value=SimpleNamespace(allocation_bytes=1))):
            packages, status = module.prepare(None, "0" * 32, [meta], state, publisher, bulk,
                {"data": {}, "source_ms": now}, {"data": {}, "source_ms": now}, now_ms=now)
        self.assertEqual(len(packages), 3)
        self.assertEqual(status["pairs"], 1)
        self.assertEqual(set(status["phase_ms"]),{"sources","rows","witnesses","assembly","validated_wire"})
        self.assertTrue(all(type(v) is int and v>=0 for v in status["phase_ms"].values()))
        self.assertEqual(len(built), 3)
        registrations={call.args[0]:call.args[1] for call in publisher.register_source.call_args_list}
        import json
        self.assertEqual(set(registrations),{"bulk","info","premium","pairs:0"})
        self.assertEqual(json.loads(registrations["pairs:0"])["BTCUSDT"]["retained"]["last_collected_ms"],now)
        for row in built:
            self.assertEqual(row["price"]["value"], "107.125")
            self.assertEqual(row["volume"]["quote_24h"]["value"], "1234")
            self.assertEqual(row["references"]["status"], "available")
            for change in row["changes"].values():
                self.assertEqual(change["status"], "available")
                self.assertEqual(change["value"], "0.07125")
                self.assertEqual(change["input_refs"],["pairs:0","bulk"])
