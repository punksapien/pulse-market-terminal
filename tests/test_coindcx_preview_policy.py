import time
from unittest import TestCase
from unittest.mock import Mock,patch

from pulse.collectors.coindcx_policy import ensure_ready,TRANSITION,OLD_COOLDOWN,READY
from pulse.collectors.coindcx_admission import CoinDCXAdmission
from pulse.collectors.preview_sources import Sources,DispatchPacer,AdmissionDenied
from pulse.collectors.preview_runtime import run


class PreviewPolicyTests(TestCase):
    def test_transition_seeds_both_windows_without_reset_and_waits(self):
        client=Mock();ledger=Mock(keys=['meta'],mapping='mapping')
        ledger.reconcile.return_value=[b'reconciled',b'1800000000000']
        client.eval.return_value=[b'waiting',b'1800000060001',b'10']
        self.assertEqual(ensure_ready(client,ledger,'cooldown'),(False,1800000060001))
        ledger.reconcile.assert_called_once_with({'minute':720,'requests':12},evidence_id='approved_preview12_initial_full_windows')
        client.eval.assert_called_once_with(TRANSITION,4,OLD_COOLDOWN,READY,'cooldown','meta','mapping')
        ledger.reconcile.return_value=[b'cannot_reset_live_budget']
        client.eval.return_value=[b'ready',b'1800000060001',b'10']
        self.assertTrue(ensure_ready(client,ledger,'cooldown')[0])
        ledger.reconcile.return_value=[b'orphaned_budget_ledger']
        with self.assertRaises(RuntimeError):ensure_ready(client,ledger,'cooldown')

    def test_reserve_charges_second_and_minute_and_refuses_before_ready(self):
        client=Mock();client.get.return_value=None
        policy=CoinDCXAdmission(client,'vercel-bom1-all-v1-dcx12',preview_policy=True)
        policy.ledger.reserve=Mock(return_value=[b'reserved'])
        self.assertEqual(policy.reserve({}),[b'coindcx_policy_not_ready'])
        policy.ledger.reserve.assert_not_called()
        policy.policy_ready=True
        policy.reserve({})
        policy.ledger.reserve.assert_called_once_with({'minute':1,'requests':1})
        self.assertEqual(policy.ledger.limits['minute'].cap,720)
        self.assertEqual(policy.ledger.limits['requests'].cap,12)
        old=CoinDCXAdmission(client,'old')
        self.assertEqual(list(old.ledger.limits),['requests'])
        self.assertEqual(old.ledger.limits['requests'].cap,8)

    def test_source_wait_precedes_any_http_and_binance_policy_is_unchanged(self):
        source=Sources(Mock(),scope='vercel-bom1-all-v1',deadline=time.monotonic()+60,preview_dcx12=True)
        self.assertEqual(source.binance.limits['history'].cap,850)
        self.assertEqual(source.binance.scope,'vercel-bom1-all-v1-binance')
        with patch('pulse.collectors.all_pair_policy.ensure_ready',return_value=(True,1)),\
             patch.object(source.binance,'preflight'),patch.object(source.dcx,'prepare',return_value=False),\
             patch('pulse.collectors.preview_sources.fetch') as fetch:
            with self.assertRaises(AdmissionDenied):source.initialise()
            fetch.assert_not_called()
        with self.assertRaises(ValueError):Sources(Mock(),scope='operator-preview-c1',deadline=10,preview_dcx12=True)

    def test_production_never_uses_preview12_and_pacer_is_bounded(self):
        with patch('pulse.collectors.preview_runtime.connect') as connect:
            with self.assertRaisesRegex(ValueError,'preview_dcx_policy_scope'):
                run(run_id='a'*32,mapping_version='sha256:'+'b'*64,duration_ms=55000,
                    env=dict(VERCEL_ENV='production',PULSE_RELEASE_TOP350='1',PULSE_UNIVERSE_MODE='all',PULSE_PREVIEW_DCX12='1'))
            connect.assert_not_called()
        for gap in (.083,.151,True):
            with self.assertRaises(ValueError):DispatchPacer(coindcx_gap=gap)
        self.assertEqual(DispatchPacer(coindcx_gap=.084).coindcx_gap,.084)
