from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from pulse.collectors import preview_pair as pair
import test_runtime_funding as fixture

NOW=1800000000000
META=dict(pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=4,mapping_version="fixture")

class FundingRefreshTests(unittest.TestCase):
    def test_stale_instrument_is_refreshed_before_settlement_verification(self):
        report=fixture.fixture(4);source=self.source()
        prior=dict(instrument={},instrument_ms=NOW-130000)
        def checked_inputs(*args,**kwargs):
            self.assertEqual(kwargs["instrument_ms"],NOW)
            return report.metadata,None
        with patch.object(pair,"collect_instrument",return_value={**prior,"instrument_ms":NOW}) as refresh,\
                patch.object(pair,"inputs",side_effect=checked_inputs),\
                patch.object(pair,"history",return_value=(report.latest,)),\
                patch.object(pair,"settled_report",return_value=report):
            result=pair.collect_funding(None,"run",META,source,{},dict(data={},source_ms=NOW),dict(data={"BTCUSDT":{}}),prior)
        refresh.assert_called_once()
        self.assertEqual(result["funding_refreshed_ms"],NOW)
        self.assertEqual(prior["instrument_ms"],NOW-130000)

    def test_failed_instrument_dependency_never_freshens_last_good_report(self):
        source=self.source();prior=dict(instrument={},instrument_ms=NOW-130000,funding_report="retained",funding_refreshed_ms=NOW-999999)
        before=deepcopy(prior)
        with patch.object(pair,"collect_instrument",side_effect=ValueError("instrument_provider_time_missing_or_stale")):
            with self.assertRaises(ValueError):pair.collect_funding(None,"run",META,source,{}, {}, {},prior)
        self.assertEqual(prior,before);source.request.assert_not_called()
    def source(self):
        source=Mock();source.clock.return_value=NOW;source.request.return_value=[]
        return source

    def test_verified_refresh_updates_report_but_not_context(self):
        report=fixture.fixture(4);source=self.source()
        prior=dict(instrument={},instrument_ms=NOW,last_collected_ms=NOW-999999)
        with patch.object(pair,"inputs",return_value=(report.metadata,None)),patch.object(pair,"history",return_value=(report.latest,)),patch.object(pair,"settled_report",return_value=report):
            result=pair.collect_funding(None,"run",META,source,{},dict(data={},source_ms=NOW),dict(data={"BTCUSDT":{}}),prior)
        self.assertIn("funding_report",result);self.assertEqual(result["funding_refreshed_ms"],NOW)
        self.assertEqual(result["last_collected_ms"],prior["last_collected_ms"])
        self.assertNotIn("funding_report",prior);source.request.assert_called_once()

    def test_failed_refresh_keeps_last_good_and_does_not_advance_freshness(self):
        report=fixture.fixture(4);source=self.source()
        prior=dict(instrument={},instrument_ms=NOW,funding_report="last-good",funding_refreshed_ms=NOW-999999,nft=1)
        before=deepcopy(prior)
        failed=SimpleNamespace(verified_at_ms=NOW,last=SimpleNamespace(status="unavailable",reason="latest_unverified"))
        with patch.object(pair,"inputs",return_value=(report.metadata,None)),patch.object(pair,"history",return_value=()),patch.object(pair,"settled_report",return_value=failed):
            result=pair.collect_funding(None,"run",META,source,{},dict(data={},source_ms=NOW),dict(data={"BTCUSDT":{}}),prior)
        self.assertEqual(result["funding_report"],"last-good")
        self.assertEqual(result["funding_refreshed_ms"],before["funding_refreshed_ms"])
        self.assertEqual(result["nft"],1);self.assertEqual(result["funding_refresh_reason"],"latest_unverified")

    def test_same_cycle_current_report_has_no_exchange_call(self):
        report=fixture.fixture(4);source=self.source()
        prior=dict(instrument={},instrument_ms=NOW,funding_report="current",funding_history=[[NOW,"0.01"]],funding_refreshed_ms=NOW-1000,nft=report.metadata.next_funding_ms)
        with patch.object(pair,"inputs",return_value=(report.metadata,None)):
            result=pair.collect_funding(None,"run",META,source,{},dict(data={},source_ms=NOW),dict(data={"BTCUSDT":{}}),prior)
        self.assertEqual(result,prior);source.request.assert_not_called()
