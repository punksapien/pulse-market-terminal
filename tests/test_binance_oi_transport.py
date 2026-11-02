import json
import unittest
from unittest.mock import Mock, MagicMock, patch
from urllib.error import HTTPError

from pulse.collectors.binance_oi import fetch_current, fetch_history, MAX_RESPONSE
from pulse.collectors.oi_history import history_spec
from pulse.collectors.binance_transport import AdmissionDenied
from pulse.collectors.binance_transport import fetch
from pulse.store.capsule_model import CapsuleSpec


class OITransportTests(unittest.TestCase):
    def test_public_query_identity_precision_and_response_caps(self):
        self.response.read.return_value=b'[0.00000000000000000001]'
        with patch("pulse.collectors.binance_transport.build_opener",return_value=self.opener):
            result=fetch("/fixture",{"margin_currency_short_name[]":"USDT"},self.admission,
                         {"history":1,"weight":0},None,host="https://api.coindcx.com",decimal_numbers=True)
        from decimal import Decimal
        self.assertEqual(result,[Decimal("0.00000000000000000001")])
        request=self.opener.open.call_args.args[0]
        self.assertIn("margin_currency_short_name[]=USDT",request.full_url)
        self.assertEqual(request.get_header("User-agent"),"CoinDCX-Pulse/0.1")
        for options in ({"host":"https://example.com"},{"max_response":0},{"max_response":True},
                        {"max_response":4*1024*1024+1},{"timeout":0},{"timeout":9},{"timeout":True},{"timeout":float("nan")}):
            with self.assertRaises(ValueError): fetch("/fixture",{},self.admission,{"history":1,"weight":0},None,**options)
    def setUp(self):
        self.spec=CapsuleSpec("B-ETH_USDT","ETHUSDT","oi","quantity",60000,1536,900000,900000,True)
        self.history=history_spec("B-ETH_USDT","ETHUSDT")
        self.admission=Mock()
        self.admission.reserve.return_value=[b"reserved",b"1000"]
        self.opener=MagicMock()
        self.response=self.opener.open.return_value.__enter__.return_value
        self.response.status=200

    def call(self,function,spec,*,now_ms=10000000):
        with patch("pulse.collectors.binance_transport.build_opener",return_value=self.opener):
            return function(spec,self.admission,clock=lambda:now_ms)

    def test_current_and_history_costs_identity_and_native_units(self):
        self.response.read.return_value=b'{"symbol":"ETHUSDT","time":10000000,"openInterest":"110.00"}'
        result=self.call(fetch_current,self.spec)
        self.assertEqual(result.values,(110.,))
        self.assertEqual(result.sources,("110.00",))
        self.admission.reserve.assert_called_once_with({"history":0,"weight":1})
        request=self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,"https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"],8)
        self.response.read.return_value=json.dumps([{"symbol":"ETHUSDT","timestamp":"6400000","sumOpenInterest":"100"}]).encode()
        result=self.call(fetch_history,self.history)
        self.assertEqual(result[0].values,(100.,))
        self.admission.reserve.assert_called_with({"history":1,"weight":0})
        self.assertIn("period=5m&limit=320",self.opener.open.call_args.args[0].full_url)

    def test_denial_and_maintenance_never_open_socket(self):
        for status in (b"budget_reconciliation_required",b"budget_pruned_retry",b"budget_quarantined",b"budget_exhausted:1"):
            self.admission.reserve.return_value=[status]
            with self.assertRaisesRegex(AdmissionDenied,"admission_denied") as error:
                self.call(fetch_current,self.spec)
            self.assertEqual(error.exception.admission_reason,status.decode("ascii"))
        self.opener.open.assert_not_called()

    def test_no_retry_on_http_timeout_oversize_or_bad_data(self):
        for error in (TimeoutError(),HTTPError("https://fapi.binance.com",429,"",{},None)):
            self.opener.open.side_effect=error
            self.opener.open.reset_mock()
            self.admission.reserve.reset_mock()
            self.admission.feedback.reset_mock()
            with self.assertRaises((TimeoutError,RuntimeError)):
                self.call(fetch_current,self.spec)
            self.assertEqual(self.admission.reserve.call_count,1)
            self.assertEqual(self.opener.open.call_count,1)
            self.assertEqual(self.admission.feedback.call_count,1)
            self.assertEqual(self.admission.feedback.call_args.args[0],429 if isinstance(error,HTTPError) else 0)
        self.opener.open.side_effect=None
        for payload in (b"x"*(MAX_RESPONSE+1),b"not json",b'{"symbol":"BTCUSDT"}',
                        b'{"symbol":"ETHUSDT","time":10005001,"openInterest":"1"}'):
            self.response.read.return_value=payload
            with self.assertRaises((ValueError,RuntimeError)):
                self.call(fetch_current,self.spec)
        self.response.status=503
        with self.assertRaisesRegex(RuntimeError,"http_503"):
            self.call(fetch_current,self.spec)

    def test_invalid_descriptor_and_clock_do_not_reserve(self):
        for function,spec in ((fetch_current,self.history),(fetch_history,self.spec)):
            with self.assertRaises(ValueError):
                self.call(function,spec)
        for function,spec in ((fetch_current,self.spec),(fetch_history,self.history)):
            with self.assertRaises(ValueError):
                function(spec,self.admission,clock=True)
        self.admission.reserve.assert_not_called()
