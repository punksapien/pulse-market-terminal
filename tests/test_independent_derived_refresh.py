from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock,patch
from pulse.collectors import preview_compose as module


class IndependentDerivedTests(TestCase):
    def test_only_selected_group_changes_and_inputs_are_not_mutated(self):
        meta=dict(pair_id="B-BTC_USDT",symbol="BTCUSDT",mapping_version="mapping")
        prior={"derived":dict(pair_id=meta["pair_id"],mapping_version="mapping",current={"source_ms":10},
            changes={},baselines={"old":1},ls={"original_expiry":20})}
        original=deepcopy(prior)
        for update in ("oi","ls","all"):
            oi=Mock(return_value=({"source_ms":30},{},{"new":1}))
            ls=Mock(return_value="fresh-ls")
            with patch.multiple(module,stores=Mock(return_value=(1,2,3,4)),oi_values=oi,
                    read_metrics=ls,asdict_ls=Mock(return_value={"fresh_expiry":40})):
                value=module.retain_derived(None,"a"*32,meta,prior,now_ms=30,update=update)
            self.assertEqual(oi.call_count,int(update!="ls"))
            self.assertEqual(ls.call_count,int(update!="oi"))
            if update=="oi":self.assertEqual(value["derived"]["ls"],original["derived"]["ls"])
            if update=="ls":self.assertEqual(value["derived"]["current"],original["derived"]["current"])
            self.assertEqual(prior,original)

    def test_missing_derived_uses_both_and_foreign_identity_refuses(self):
        meta=dict(pair_id="B-BTC_USDT",symbol="BTCUSDT",mapping_version="mapping")
        with patch.multiple(module,stores=Mock(return_value=(1,2,3,4)),
                oi_values=Mock(return_value=({}, {}, {})),read_metrics=Mock(),asdict_ls=Mock(return_value={})):
            value=module.retain_derived(None,"a"*32,meta,{},now_ms=30,update="oi")
            self.assertIn("ls",value["derived"])
            self.assertIn("current",value["derived"])
        for derived in ({"mapping_version":"other","pair_id":meta["pair_id"]},
                        {"mapping_version":"mapping","pair_id":"B-ETH_USDT"}):
            with self.assertRaisesRegex(ValueError,"derived_identity_refused"):
                module.retain_derived(None,"a"*32,meta,{"derived":derived},now_ms=30,update="oi")
