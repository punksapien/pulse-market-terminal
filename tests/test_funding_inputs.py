from decimal import Decimal
import unittest

from pulse.collectors.funding_inputs import fraction, hours, history, info_index, inputs
from pulse.metrics.funding import settled_report, settled_view, current_delta

NOW=1800000000000


class FundingInputsTests(unittest.TestCase):
    def raw(self):
        instrument={"pair":"B-BTC_USDT","quote_currency_short_name":"USDT",
                    "settle_currency_short_name":"USDT","funding_frequency":8}
        ticker={"ts":NOW,"prices":{"B-BTC_USDT":{"mkt":"BTCUSDT","bmST":NOW,
                 "fr":Decimal("0.0001"),"efr":Decimal("0.00015")}}}
        premium={"symbol":"BTCUSDT","time":NOW,"nextFundingTime":NOW+28800000-1000}
        return instrument,ticker,premium

    def test_decimal_source_is_exact_and_float_never_silently_coerced(self):
        for raw in ("0.0001",Decimal("0.0001")):
            self.assertEqual(fraction(raw),Decimal(".0001"))
        for raw in (True,.0001,None,"NaN","Infinity","1e-29","9"*29):
            with self.subTest(raw=raw),self.assertRaises(ValueError): fraction(raw)
        for raw in (1,4,8,"4.0",Decimal(8)):
            self.assertIn(hours(raw),(1,4,8))
        for raw in (2,True,4.0):
            with self.assertRaises(ValueError): hours(raw)

    def test_scope_duplicates_rate_types_and_saturation(self):
        raw={"symbol":"BTCUSDT","fundingTime":NOW-1000,"fundingRate":"0.0001"}
        events=history([raw,raw],"B-BTC_USDT","BTCUSDT",start_ms=NOW-100000,end_ms=NOW)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].rate_type,"Regular")
        for payload in ([raw]*512,[{**raw,"symbol":"ETHUSDT"}],
                        [raw,{**raw,"fundingRate":"0.0002"}],
                        [{**raw,"fundingTime":NOW+1}]):
            with self.assertRaises(ValueError):
                history(payload,"B-BTC_USDT","BTCUSDT",start_ms=NOW-100000,end_ms=NOW)
        special=history([{**raw,"rateType":"Special"},{**raw,"rateType":"FutureType"}],
            "B-BTC_USDT","BTCUSDT",start_ms=NOW-100000,end_ms=NOW)
        self.assertEqual({e.rate_type for e in special},{"Special","FutureType"})

    def test_info_absence_requires_complete_list_not_an_error_object(self):
        self.assertEqual(info_index([]),{})
        for raw in ({"code":403},[{}],[{"symbol":"BTCUSDT"},{"symbol":"BTCUSDT"}]):
            with self.assertRaises(ValueError): info_index(raw)

    def test_source_times_identity_and_verified_retention(self):
        instrument,ticker,premium=self.raw()
        meta,estimate=inputs("B-BTC_USDT","BTCUSDT",instrument,ticker,{},premium,
                             instrument_ms=NOW,info_ms=NOW)
        self.assertIsNone(meta.binance_hours)
        self.assertEqual(meta.source_ms,NOW)
        raw=[{"symbol":"BTCUSDT","fundingTime":NOW-1000-i*28800000,"fundingRate":"0.0001"} for i in range(24)]
        events=history(raw,"B-BTC_USDT","BTCUSDT",start_ms=NOW-8*86400000,end_ms=NOW)
        report=settled_report(events,meta,now_ms=NOW)
        self.assertEqual(report.cumulative_1d.fraction,Decimal("0.0003"))
        self.assertEqual(current_delta(estimate,report,now_ms=NOW).bps,Decimal("0.5"))
        self.assertEqual(settled_view(report,now_ms=NOW+130000).last.status,"available")
        self.assertEqual(report.verified_at_ms,NOW)
        ticker["prices"]["B-BTC_USDT"]["mkt"]="ETHUSDT"
        with self.assertRaises(ValueError):
            inputs("B-BTC_USDT","BTCUSDT",instrument,ticker,{},premium,instrument_ms=NOW,info_ms=NOW)


if __name__=="__main__": unittest.main()
