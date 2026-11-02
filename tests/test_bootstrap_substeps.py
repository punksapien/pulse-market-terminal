from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch

from pulse.collectors import preview_pair as module


class BootstrapSubstepsTests(TestCase):
    def test_real_collector_preserves_validated_steps_and_resumes_without_redownload(self):
        now=1800000000000
        source=Mock();source.clock.return_value=now;source.last_source_ms=now
        instrument={"pair":"B-BTC_USDT","funding_frequency":8}
        source.request.return_value={"instrument":instrument}
        point=SimpleNamespace(source_ms=now)
        meta={"pair_id":"B-BTC_USDT","symbol":"BTCUSDT","funding_hours":8,"mapping_version":"v1"}
        bulk={"prices":{"B-BTC_USDT":{"mkt":"BTCUSDT"}}}
        history=[]
        with ExitStack() as stack:
            for name,value in (("stores",tuple(Mock() for _ in range(4))),
                ("parse_current",point),("parse_history",[point]),("parse_ls",[point]),
                ("inputs",(SimpleNamespace(coindcx_hours=8),None))):
                stack.enter_context(patch.object(module,name,return_value=value))
            stack.enter_context(patch.object(module,"save_points"))
            stack.enter_context(patch.object(module,"collect_funding",
                side_effect=lambda c,r,m,s,b,i,p,prior,**kw:{**prior,"funding_report":"verified"}))
            refs=stack.enter_context(patch.object(module,"collect_references",
                side_effect=RuntimeError("worker_source_budget_exhausted")))
            legacy=stack.enter_context(patch.object(module,"refresh_legacy"))
            def collect(prior):
                return module.collect(None,"a"*32,meta,source,bulk,{"data":{},"source_ms":now},
                    {"data":{"BTCUSDT":{}}},prior,all_mode=True,checkpoint=lambda row:history.append(row))
            with self.assertRaisesRegex(RuntimeError,"worker_source_budget_exhausted"):collect(None)
            self.assertEqual(len(history),6)
            partial=history[-1]
            self.assertEqual(partial["cold_source_ms"],now)
            self.assertEqual(partial["global_source_ms"],now)
            self.assertEqual(partial["top_source_ms"],now)
            self.assertEqual(partial["funding_report"],"verified")
            self.assertTrue(all("last_collected_ms" not in row for row in history))
            legacy.assert_not_called()
            source.clock.return_value=now+20000;source.request.reset_mock()
            refs.side_effect=lambda m,s,b,prior:{**prior,"opens":[[now,"100"]]}
            result=collect(partial)
            source.request.assert_not_called()
            self.assertEqual(result["last_collected_ms"],now+20000)
            self.assertEqual(result["instrument_ms"],now)
            self.assertEqual(result["cold_source_ms"],now)
            legacy.assert_called_once()
