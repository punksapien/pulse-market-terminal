"""ADR-0041's eleven fixture families, generated from exact fraction strings."""
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext

from pulse.metrics.funding import (FundingEstimate, FundingMetadata, Settlement,
                                   HOUR, GRACE, settled_report, current_delta, native_estimate)

D = Decimal
T = 1793001600000


def history(h=4, count=169):
    return [Settlement("BTCUSDT", T - (count-1-i)*h*HOUR, D("0.0001") + D(i)*D("0.000001")) for i in range(count)]


def metadata(rows, h=4):
    latest = max((row for row in rows if row.rate_type == "Regular"), key=lambda row: row.time_ms)
    return FundingMetadata("BTCUSDT", "mapping-v1", h, h, T + h*HOUR, latest.rate, T+10000,
                           cap=D("0.0002"), floor=D("-0.00005"))


def estimate(meta, rate="0.00012"):
    return FundingEstimate(meta.pair_id, meta.mapping_version, meta.coindcx_hours, meta.next_funding_ms,
                           meta.settled_rate, D(rate), meta.source_ms)


class FundingTests(unittest.TestCase):
    def report(self, rows, h=4, **kwargs):
        return settled_report(rows, metadata(rows, h), now_ms=T+10000, **kwargs)

    def test_01_counts_exact_sums_and_settled_window_differences(self):
        for h, n1, n7 in ((1,24,168), (4,6,42), (8,3,21)):
            rows = history(h)
            result = self.report(rows, h)
            for count, cumulative, delta in ((n1,result.cumulative_1d,result.delta_1d),(n7,result.cumulative_7d,result.delta_7d)):
                self.assertEqual(cumulative.fraction, sum(row.rate for row in rows[-count:]))
                self.assertEqual(delta.fraction, rows[-1].rate - rows[-count-1].rate)
                self.assertEqual(cumulative.assurance, "window_consistent_provisional")
            self.assertEqual(result.last.level_basis_bps, D("0.01")*8/h)

    def test_02_missing_middle_does_not_disable_consecutive_latest(self):
        rows = history()
        rows.pop(-4)
        result = self.report(rows)
        self.assertEqual(result.last.status, "available")
        self.assertEqual(result.cumulative_1d.reason, "incomplete_or_interval_changed_in_window")

    def test_03_interval_change_in_both_directions(self):
        for old, new in ((4,1),(1,4)):
            rows = [Settlement("BTCUSDT", T-new*HOUR-(167-i)*old*HOUR, D("0.0001")) for i in range(167)]
            rows += [Settlement("BTCUSDT",T-new*HOUR,D("0.00011")), Settlement("BTCUSDT",T,D("0.00012"))]
            result = self.report(rows, new)
            self.assertEqual(result.last.status, "available")
            self.assertEqual(result.cumulative_1d.reason, "incomplete_or_interval_changed_in_window")

    def test_04_jitter_inclusive_boundaries(self):
        for jitter, valid in ((6,True),(5000,True),(5001,False),(-5000,True),(-5001,False)):
            rows = history()
            rows[-2] = replace(rows[-2], time_ms=rows[-2].time_ms-jitter)
            self.assertEqual(self.report(rows).last.status, "available" if valid else "unavailable")
        rows = history()
        meta = metadata(rows)
        for jitter, valid in ((5000,True),(5001,False)):
            result = settled_report(rows, replace(meta, next_funding_ms=meta.next_funding_ms+jitter), now_ms=T+10000)
            self.assertEqual(result.last.status, "available" if valid else "unavailable")

    def test_05_anchor_fr_and_interval_authority(self):
        rows = history()
        meta = metadata(rows)
        for replacement, reason in (({"next_funding_ms":T+HOUR},"latest_unverified"),
                                    ({"settled_rate":D("0.9")},"latest_unverified"),
                                    ({"binance_hours":8},"interval_sources_disagree"),
                                    ({"coindcx_hours":2},"interval_unsupported")):
            self.assertEqual(settled_report(rows, replace(meta, **replacement), now_ms=T+10000).last.reason, reason)
        rows = history(8)
        self.assertEqual(settled_report(rows, replace(metadata(rows,8), binance_hours=None), now_ms=T+10000).last.status,"available")

    def test_06_special_ties_unknown_types_and_per_window_independence(self):
        rows = history()
        base = self.report(rows)
        special = replace(rows[-1], rate_type="Special", rate=D("99"))
        result = self.report(rows+[special])
        self.assertEqual(result.cumulative_1d, base.cumulative_1d)
        self.assertEqual(result.excluded_special, 1)
        unknown = replace(rows[-4], rate_type="Mystery")
        result = self.report(rows+[unknown])
        self.assertEqual(result.cumulative_1d.reason,"unknown_rate_type")
        self.assertEqual(result.last.status,"available")

    def test_07_due_pending_expiry_and_fresh_estimate_cannot_extend(self):
        rows = history()
        for delay, reason in ((4*HOUR-1,None),(4*HOUR,"settlement_pending"),(4*HOUR+GRACE-1,"settlement_pending"),(4*HOUR+GRACE,"expired")):
            meta = replace(metadata(rows), source_ms=T+delay)
            result = settled_report(rows, meta, now_ms=T+delay)
            self.assertEqual(result.last.reason,reason)
            if reason:
                self.assertIsNone(current_delta(estimate(meta),result,now_ms=T+delay).fraction)

    def test_08_short_history_windows_are_independent(self):
        result = self.report(history(4,6))
        self.assertEqual(result.cumulative_1d.status,"available")
        self.assertEqual(result.cumulative_7d.reason,"insufficient_history")
        self.assertEqual(result.delta_1d.reason,"insufficient_history")
        self.assertEqual(self.report(history(4,1)).last.reason,"insufficient_history")

    def test_09_page_ties_dedup_conflict_and_input_order(self):
        rows = history()
        result = self.report(rows)
        self.assertEqual(self.report(list(reversed(rows))+rows[-2:]),result)
        conflict = replace(rows[-1], rate=D("0.123"))
        self.assertEqual(self.report(rows+[conflict]).last.reason,"conflicting_duplicate")

    def test_10_signed_bps_level_basis_current_delta_negative_zero(self):
        for first, last, bps in (("0.0001","0.00015","0.5"),("0.00002","-0.00002","-0.4"),("0","-0","0")):
            rows = [Settlement("BTCUSDT",T-4*HOUR,D(first)),Settlement("BTCUSDT",T,D(last))]
            result = self.report(rows)
            self.assertEqual(result.last.bps,D(bps))
            self.assertEqual(result.last.level_basis_bps,abs(D(bps))*2)
        rows = history()
        rows[-1] = replace(rows[-1], rate=D("0.0002"))
        meta = metadata(rows)
        result = current_delta(estimate(meta), self.report(rows), now_ms=T+10000)
        self.assertEqual(result.bps,D("-0.8"))
        self.assertEqual(result.classification_status,"unclassified")
        self.assertIsNone(result.level_basis_bps)

    def test_11_audit_mismatch_quarantines_cumulative_only(self):
        result = self.report(history(),audit_mismatch=True)
        self.assertEqual(result.cumulative_1d.reason,"audit_mismatch")
        self.assertEqual(result.cumulative_7d.reason,"audit_mismatch")
        self.assertEqual(result.last.status,"available")
        self.assertEqual(result.delta_1d.status,"available")

    def test_current_delta_pairing_expiry_and_future(self):
        rows = history()
        meta = metadata(rows)
        report = self.report(rows)
        live = estimate(meta)
        for replacement in ({"mapping_version":"v2"},{"hours":8},{"next_funding_ms":T+8*HOUR},{"settled_rate":D(0)},{"pair_id":"ETHUSDT"}):
            self.assertEqual(current_delta(replace(live,**replacement),report,now_ms=T+10000).reason,"cycle_identity_mismatch")
        self.assertEqual(current_delta(replace(live,source_ms=T+15001),report,now_ms=T+10000).reason,"future_estimate")
        self.assertEqual(current_delta(replace(live,source_ms=T-120000),report,now_ms=T+10000).reason,"stale_estimate")
        self.assertEqual(current_delta(replace(live,source_ms=T-1),report,now_ms=T+10000).reason,"cycle_identity_mismatch")

    def test_native_rate_flags_anchor_apr_independent_of_missing_history(self):
        rows = history(1)
        meta = metadata(rows,1)
        result = native_estimate(estimate(meta,"0.0000125"),meta,now_ms=T+10000)
        self.assertEqual(result["anchor_distance_fraction"],0)
        self.assertEqual(result["apr_simple_fraction"],D("0.1095"))
        meta = replace(meta,cap=None)
        result = native_estimate(estimate(meta,"-0.00005"),meta,now_ms=T+10000)
        self.assertIsNone(result["at_cap"])
        self.assertTrue(result["at_floor"])
        self.assertEqual(result["direction"],"shorts_pay")

    def test_malformed_resources_and_decimal_context_independence(self):
        rows = history()
        meta = metadata(rows)
        for bad in ([replace(rows[-1],rate=D("NaN"))], [replace(rows[-1],time_ms=T+15001)], rows*4):
            self.assertEqual(settled_report(bad,meta,now_ms=T+10000).last.status,"unavailable")
        expected = self.report(rows)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(self.report(rows), expected)
        with self.assertRaises(ValueError):
            settled_report(rows,meta,now_ms=True)
