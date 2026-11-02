import copy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools/probes"))
import pulse_cohort_bootstrap as bootstrap
import pulse_volatility_real_parity as parity
from pulse_lua_budget import instrument, Timed
from pulse.collectors.cohort import select, CANARIES, CHANGED, COMMODITIES


class CohortTests(unittest.TestCase):
    def test_timing_wrapper_preserves_body_and_unwraps_results(self):
        body="return {'fixture'}"
        wrapped=instrument(body)
        self.assertIn(body,wrapped)
        self.assertEqual(wrapped.count("redis.call('TIME')"),2)
        client=Mock()
        client.script_load.return_value="fixture-sha"
        client.evalsha.return_value=([b"fixture"],25)
        timed=Timed(client)
        self.assertEqual(timed.eval(body,0),[b"fixture"])
        self.assertEqual(timed.eval(body,0),[b"fixture"])
        self.assertEqual(client.script_load.call_count,1)
        self.assertEqual(sum(len(v) for v in timed.samples.values()),2)
        client.evalsha.return_value=([], -1)
        with self.assertRaises(RuntimeError): timed.eval(body,0)
    def sources(self):
        symbols = list(CANARIES)+list(CHANGED)+list(COMMODITIES)+[f"TEST{i}USDT" for i in range(60)]
        active = ["B-"+s[:-4]+"_USDT" for s in symbols]
        ticker = {"prices":{p:{"mkt":s} for p,s in zip(active,symbols)}}
        exchange = {"symbols":[{"symbol":s,"quoteAsset":"USDT","marginAsset":"USDT",
            "status":"TRADING","contractType":"PERPETUAL","onboardDate":i} for i,s in enumerate(symbols)]}
        return active,ticker,exchange

    def test_manifest_is_seeded_coverage_checked_and_order_independent(self):
        active,ticker,exchange = self.sources()
        a = select(active,ticker,exchange,now_ms=1000000)
        b = select(active[::-1],ticker,exchange,now_ms=1000000)
        self.assertEqual(a,b)
        self.assertEqual(len(a["pairs"]),50)
        self.assertEqual([r["symbol"] for r in a["pairs"][:5]],list(CANARIES))
        self.assertTrue(set(CHANGED) <= {r["symbol"] for r in a["pairs"]})
        self.assertNotEqual(a,select(active,ticker,exchange,now_ms=1000000,seed=7))

    def test_identity_missing_coverage_duplicates_and_short_universe_refuse(self):
        sources = self.sources()
        for kind in ("duplicate","missing","mapping","commodity","size","shape"):
            active,ticker,exchange = copy.deepcopy(sources)
            if kind == "duplicate": active.append(active[0])
            if kind == "missing": active.remove("B-BTC_USDT")
            if kind == "mapping": ticker["prices"]["B-BTC_USDT"]["mkt"] = "ETHUSDT"
            if kind == "commodity":
                active = [p for p in active if p[2:-5]+"USDT" not in COMMODITIES]
            if kind == "size": active = active[:20]
            if kind == "shape": active = None
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                select(active,ticker,exchange,now_ms=1000000)

    def test_pair_dimensions_are_not_shrunk(self):
        specs = bootstrap.pair_specs({"symbol":"BTCUSDT","pair_id":"B-BTC_USDT"})
        self.assertEqual([s.capacity for s in specs],[1536,320,320,320])
        self.assertEqual(sum(s.nbytes for s in specs),51856)

    def test_dispatch_denial_and_hard_response_stop_all_workers(self):
        client = Mock()
        client.time.return_value = (1800000000,0)
        with patch.object(bootstrap,"source_admission"):
            dispatch = bootstrap.Dispatch(client,None)
        dispatch.header = {"status":200,"used_weight_1m":1,"provider_date_ms":1800000000000}
        admission = Mock()
        admission.reserve.return_value = [b"budget_exhausted:1"]
        result = dispatch.admit(admission,{"history":1,"weight":0})
        self.assertEqual(result[0],b"budget_exhausted:1")
        self.assertTrue(dispatch.stop.is_set())
        admission.reserve.reset_mock()
        self.assertEqual(dispatch.admit(admission,{"history":1,"weight":0}),[b"cohort_stopped"])
        admission.reserve.assert_not_called()
        for status in (0,403,418,429,451,500):
            dispatch.stop.clear()
            admission.feedback.return_value = {"used_weight_1m":-1}
            dispatch.feedback(admission,status,{})
            self.assertTrue(dispatch.stop.is_set())

    def test_real_candle_comparator_uses_tolerance_but_exact_labels(self):
        expected = {"pair":{"score":.1,"label":"Active","count":4}}
        self.assertEqual(parity.compare(expected,expected),(3,[]))
        tiny = copy.deepcopy(expected); tiny["pair"]["score"] += 1e-15
        self.assertEqual(parity.compare(tiny,expected)[1],[])
        for field,value in (("label","Hot"),("count",4.),("score",.2)):
            bad = copy.deepcopy(expected); bad["pair"][field]=value
            self.assertEqual(parity.compare(bad,expected)[1],["pair:"+field])
        self.assertIn("row_keys",parity.compare({},expected)[1])


if __name__ == "__main__":
    unittest.main()
