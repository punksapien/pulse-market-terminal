from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from pulse.collectors import universe_registry as registry

NOW=1800000000000


def contract(symbol, **overrides):
    return dict(symbol=symbol,status="TRADING",contractType="PERPETUAL",quoteAsset="USDT",marginAsset="USDT",**overrides)


class UniverseTests(unittest.TestCase):
    def test_full_intersection_scope_and_order_not_fixed_fifty(self):
        active=[f"B-P{i}_USDT" for i in range(504)]
        rows=[contract(f"P{i}USDT") for i in range(504)]
        selected,excluded=registry.select_pairs(active,{"symbols":rows})
        self.assertEqual(len(selected),504)
        self.assertEqual(excluded,[])
        self.assertEqual([p["pair_id"] for p in selected],sorted(active))
        self.assertEqual(registry.select_pairs(list(reversed(active)),{"symbols":list(reversed(rows))})[0],selected)

    def test_tradfi_included_spot_coinm_usdc_dated_inactive_excluded(self):
        rows=[contract("BTCUSDT"),{**contract("XAUUSDT"),"contractType":"TRADIFI_PERPETUAL"}]
        for name,updates in (("SPOT",{"contractType":"SPOT"}),("COIN",{"marginAsset":"BTC"}),
                ("USDC",{"marginAsset":"USDC"}),("DATED",{"contractType":"CURRENT_QUARTER"}),
                ("OFF",{"status":"PENDING_TRADING"})):
            rows.append({**contract(name+"USDT"),**updates})
        active=["B-"+r["symbol"][:-4]+"_USDT" for r in rows]
        selected,excluded=registry.select_pairs(active,{"symbols":rows})
        self.assertEqual([r["symbol"] for r in selected],["BTCUSDT","XAUUSDT"])
        self.assertEqual(len(excluded),5)

    def test_unrelated_native_symbol_formats_do_not_block_the_intersection(self):
        selected,excluded=registry.select_pairs(["B-BTC_USDT"],{"symbols":[
            contract("BTCUSDT"),contract("币安人生USDT"),contract("UNRELATED_USDT")]})
        self.assertEqual(selected,[{"pair_id":"B-BTC_USDT","symbol":"BTCUSDT"}])
        self.assertEqual(excluded,[])

    def test_duplicate_malformed_and_over_bound_never_truncate(self):
        cases=[(["B-BTC_USDT"]*2,{"symbols":[contract("BTCUSDT")]}),
               (["BTCUSDT"],{"symbols":[contract("BTCUSDT")]}),
               (["B-BTC_USDT"],{"symbols":[contract("BTCUSDT")]*2}),
               (["B-BTC_USDT"],{"symbols":[None]}),
               (["B-BTC_USDT"],{"symbols":[]}),
               ([f"B-P{i}_USDT" for i in range(1025)],{"symbols":[contract(f"P{i}USDT") for i in range(1025)]})]
        for active,exchange in cases:
            with self.subTest(active=active[:2]),self.assertRaises(ValueError):
                registry.select_pairs(active,exchange)


class FakeDocument:
    values={}
    def __init__(self,client,run_id,channel,identity):
        self.channel=channel;self.owner=b"owner";self.lease="pulse:stg:{"+run_id+"}:"+channel
    def acquire(self):pass
    def renew(self):pass
    def read(self,*,now_ms):
        entry=self.values.get(self.channel)
        if not entry:return dict(status="unavailable",reason="document_missing",revision=0)
        value,rev=entry
        if now_ms>=value.expires_ms:return dict(status="unavailable",reason="document_expired",revision=rev)
        return dict(status="available",payload=value.payload,revision=rev)
    def publish(self,value,*,expected_revision):
        current=self.values.get(self.channel,(None,0))[1]
        if current!=expected_revision:return [b"revision_conflict"]
        self.values[self.channel]=(value,current+1)
        return [b"published",str(current+1).encode()]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        FakeDocument.values={}
        self.client=Mock()
        self.source=Mock()
        self.now=NOW
        self.source.clock.side_effect=lambda:self.now
        self.active=["B-BTC_USDT","B-ETH_USDT","B-XAU_USDT"]
        self.failed=None
        self.instrument_calls=[]
        def request(path,params,**kwargs):
            self.now+=1
            if path.endswith("active_instruments"):return self.active
            if path.endswith("exchangeInfo"):
                return {"symbols":[contract(pair[2:].replace("_","")) for pair in self.active]}
            pair=params["pair"];self.instrument_calls.append(pair)
            if pair==self.failed:raise RuntimeError("source_circuit_open")
            return dict(pair=pair,quote_currency_short_name="USDT",settle_currency_short_name="USDT",
                funding_frequency={"B-BTC_USDT":"8","B-ETH_USDT":"4","B-XAU_USDT":"1"}[pair])
        self.source.request.side_effect=request
        self.patch=patch.object(registry,"RuntimeDocument",FakeDocument)
        self.patch.start();self.addCleanup(self.patch.stop)

    def step(self,batch=1):
        return registry.bootstrap_step(self.client,"a"*32,self.source,batch_size=batch)

    def test_resume_and_publish_only_when_complete(self):
        first=self.step()
        self.assertEqual((first["status"],first["completed"]),("incomplete",1))
        self.assertNotIn("source:registry",FakeDocument.values)
        self.step()
        result=self.step()
        self.assertEqual(result["status"],"registry_ready")
        self.assertEqual([r["funding_hours"] for r in result["universe"]],[8,4,1])
        self.assertEqual(self.instrument_calls,self.active)
        self.assertEqual(self.source.request.call_count,5,"two discovery calls, three metadata calls")
        self.assertEqual(registry.load(self.client,"a"*32,result["mapping_version"],now_ms=self.now),result["universe"])
        with self.assertRaises(ValueError):
            registry.load(self.client,"a"*32,"sha256:"+"0"*64,now_ms=self.now)

    def test_source_failure_commits_successful_prefix_without_same_step_retry(self):
        self.failed="B-ETH_USDT"
        with self.assertRaisesRegex(RuntimeError,"source_circuit_open"):self.step(3)
        progress=json.loads(FakeDocument.values["source:registry_bootstrap"][0].payload)
        self.assertEqual(len(progress["universe"]),1)
        self.assertEqual(self.instrument_calls,["B-BTC_USDT","B-ETH_USDT"])
        self.assertNotIn("source:registry",FakeDocument.values)
        self.failed=None
        self.assertEqual(self.step(3)["status"],"registry_ready")
        self.assertEqual(self.instrument_calls,["B-BTC_USDT","B-ETH_USDT","B-ETH_USDT","B-XAU_USDT"])

    def test_progress_corruption_is_not_permission_to_start_over(self):
        self.step()
        entry,rev=FakeDocument.values["source:registry_bootstrap"]
        value=json.loads(entry.payload);value["universe"][0]["funding_hours"]=0
        FakeDocument.values["source:registry_bootstrap"]=(registry.document(value,source_ms=self.now,expires_ms=self.now+10000),rev)
        with self.assertRaisesRegex(ValueError,"registry_progress_identity"):self.step()
        self.assertEqual(self.instrument_calls,["B-BTC_USDT"])

    def test_batch_bound_and_discovery_expiry_are_not_extended(self):
        for batch in (0,9,True):
            with self.assertRaises(ValueError):self.step(batch)
        self.step()
        expiry=FakeDocument.values["source:registry_bootstrap"][0].expires_ms
        self.step()
        self.assertEqual(FakeDocument.values["source:registry_bootstrap"][0].expires_ms,expiry)
        result=self.step()
        self.assertEqual(FakeDocument.values["source:registry"][0].expires_ms,expiry)
        self.now=expiry
        with self.assertRaises(RuntimeError):registry.load(self.client,"a"*32,result["mapping_version"],now_ms=self.now)
