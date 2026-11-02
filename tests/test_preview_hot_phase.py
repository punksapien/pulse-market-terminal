import unittest
from unittest.mock import patch,Mock
from pulse.collectors import preview_pair as pair
from pulse.store.capsule_model import CapsuleSpec
from pulse.collectors.preview_batch import collect_batch

NOW=1800000000000
META=dict(pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=8)

class Source:
    calls=0
    last_source_ms=NOW
    def clock(self):return NOW
    def request(self,path,params,**kwargs):
        self.calls+=1
        if path.endswith("openInterest"):return dict(symbol="BTCUSDT",openInterest="100",time=NOW)
        return dict(pair="B-BTC_USDT",quote_currency_short_name="USDT",settle_currency_short_name="USDT",funding_frequency="8")

class HotTests(unittest.TestCase):
    def test_hot_updates_only_own_observation_and_instrument(self):
        own=Mock(spec=());own.spec=CapsuleSpec("B-BTC_USDT","BTCUSDT","oi","quantity",60000,1536,900000,900000,True)
        prior=dict(last_collected_ms=NOW-100000,legacy_source_ms=NOW-500000,references_refreshed_ms=NOW-200000)
        source=Source()
        with patch.object(pair,"stores",return_value=(own,None,None,None)),patch.object(pair,"save_points") as save:
            result=pair.collect_hot(None,"run",META,source,prior)
        self.assertEqual(source.calls,2);save.assert_called_once()
        self.assertEqual(result["last_hot_ms"],NOW);self.assertEqual(result["oi_source_ms"],NOW)
        for key,value in prior.items():self.assertEqual(result[key],value)
        self.assertNotIn("last_hot_ms",prior)

    def test_hot_batch_does_not_skip_based_on_context_completion(self):
        calls=[]
        def collect(meta,source,prior):calls.append(meta["symbol"]);return {**prior,"last_hot_ms":NOW}
        result=collect_batch([META],{"BTCUSDT":{"last_collected_ms":NOW,"last_hot_ms":NOW-60000}},
            collect=collect,make_source=Source,check_lease=lambda:None,timestamp_field="last_hot_ms")
        self.assertEqual(result["collected"],1);self.assertEqual(calls,["BTCUSDT"])

    def test_bad_instrument_never_marks_hot_complete(self):
        own=Mock();own.spec=CapsuleSpec("B-BTC_USDT","BTCUSDT","oi","quantity",60000,1536,900000,900000,True)
        source=Source();original=source.request
        def request(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            if path.endswith("instrument"):value["pair"]="B-ETH_USDT"
            return value
        source.request=request
        with patch.object(pair,"stores",return_value=(own,None,None,None)),patch.object(pair,"save_points"):
            with self.assertRaisesRegex(ValueError,"hot_instrument_identity"):pair.collect_hot(None,"run",META,source,{})

    def test_future_progress_never_suppresses_a_hot_poll(self):
        calls=[]
        def collect(meta,source,prior):calls.append(1);return {"last_hot_ms":NOW}
        result=collect_batch([META],{"BTCUSDT":{"last_hot_ms":NOW+100000}},collect=collect,
            make_source=Source,check_lease=lambda:None,timestamp_field="last_hot_ms")
        self.assertEqual(result["collected"],1);self.assertEqual(calls,[1])

    def test_stale_or_future_metadata_never_marks_hot_complete(self):
        own=Mock();own.spec=CapsuleSpec("B-BTC_USDT","BTCUSDT","oi","quantity",60000,1536,900000,900000,True)
        for stamp in (NOW-120000,NOW+5001):
            source=Source();source.last_source_ms=stamp
            with self.subTest(stamp=stamp),patch.object(pair,"stores",return_value=(own,None,None,None)),patch.object(pair,"save_points"):
                with self.assertRaisesRegex(ValueError,"instrument_provider_time_missing_or_stale"):
                    pair.collect_hot(None,"run",META,source,{})
