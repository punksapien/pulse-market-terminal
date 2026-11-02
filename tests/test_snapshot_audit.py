import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location("audit",Path(__file__).parents[1]/"tools/probes/pulse_snapshot_audit.py")
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)

class AuditTests(unittest.TestCase):
    def test_source_failures_have_specific_pair_witnesses_and_zero_is_available(self):
        fields=[dict(status="available",value="0",expires_ms=200),dict(status="unavailable",reason="insufficient_history")]
        universe=[dict(pair_id="B-A_USDT"),dict(pair_id="B-B_USDT")]
        self.assertEqual(audit.unavailable_examples(fields,universe,100),[{"pair":"B-B_USDT","reason":"insufficient_history"}])
    def test_http_observation_clock_uses_age_and_refuses_missing_date(self):
        self.assertEqual(audit.response_clock({"Date":"Thu, 01 Jan 1970 00:00:10 GMT","Age":"2"}),12000)
        self.assertIsNone(audit.response_clock({}))
    def test_all_fields_includes_nonheadline_metrics_and_missing_rows(self):
        zero=dict(status="available",unit="fraction",value="0",asof_ms=100,expires_ms=300)
        result=audit.all_fields([{"context":{"momentum":zero}},{"other":zero}],150,200)
        self.assertEqual(result["context.momentum"]["fresh_at_response"],1)
        self.assertEqual(result["context.momentum"]["rows"],2)
        self.assertEqual(result["context.momentum"]["reasons"],{"not_published_in_row":1})

    def test_live_return_audit_respects_pinned_generation_and_expiry(self):
        minute=60000;now=1800000000000
        slots=[]
        for i in range(5):slots.append([dict(status="available",reference_minute_ms=now+i*minute-window,
            open_price="100") for window in (3600000,14400000,86400000)])
        refs=dict(mapping_version="mapping",definition_version="price_change.v1",generation_id="generation",
            ladder=dict(built_at_ms=now,start_ms=now,slots=slots))
        snapshot=dict(mapping_version="mapping",generation="generation",universe=[dict(pair_id="B-BTC_USDT")],rows=[dict(id=0,references=refs)])
        doc={"rows":[dict(id=0,status="available",price="101",source_ms=now,expires_ms=now+60000)]}
        result=audit.live_returns(snapshot,doc,now+1000)
        self.assertTrue(all(v=={"available":1} for v in result.values()))
        refs["generation_id"]="old"
        self.assertTrue(all(v=={"version_mismatch":1} for v in audit.live_returns(snapshot,doc,now+1000).values()))
        refs["generation_id"]="generation"
        self.assertTrue(all(v=={"numerator_expired":1} for v in audit.live_returns(snapshot,doc,now+60000).values()))
    def test_current_coverage_is_not_confused_with_historical_availability(self):
        fields=[dict(status="available",value="0",asof_ms=100,expires_ms=201,classification_status="classified"),
                dict(status="available",value="1",asof_ms=100,expires_ms=200),
                dict(status="unavailable",value=None,reason="insufficient_history")]
        result=audit.coverage(fields,150,200)
        self.assertEqual(result["available_at_generation"],2)
        self.assertEqual(result["fresh_at_response"],1)
        self.assertEqual(result["reasons"],{"insufficient_history":1})
        self.assertEqual(result["classified_at_generation"],1)
        self.assertIsNone(audit.coverage(fields,150,None)["fresh_at_response"])
