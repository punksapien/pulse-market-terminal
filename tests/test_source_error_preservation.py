import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError,URLError
from pulse.collectors.binance_transport import fetch


class SourceErrorTests(unittest.TestCase):
    def test_failed_bootstrap_does_not_mask_http_or_transport(self):
        for error in (HTTPError("https://fapi.binance.com/x",403,"refused",{},None),URLError("timeout")):
            admission=Mock();admission.reserve.return_value=[b"reserved"]
            admission.feedback.side_effect=RuntimeError("bootstrap_header_unverified")
            opener=Mock();opener.open.side_effect=error
            with patch("pulse.collectors.binance_transport.build_opener",return_value=opener):
                if isinstance(error,HTTPError):
                    with self.assertRaisesRegex(RuntimeError,"http_403") as raised:
                        fetch("/fapi/v1/openInterest",{},admission,{"history":0,"weight":1},None)
                    self.assertEqual(raised.exception.source_status,403)
                else:
                    with self.assertRaises(URLError):
                        fetch("/fapi/v1/openInterest",{},admission,{"history":0,"weight":1},None)
            admission.feedback.assert_called_once()

    def test_timeout_keeps_safe_endpoint_and_observed_budget(self):
        admission=Mock();admission.reserve.return_value=[b"reserved"]
        opener=Mock();opener.open.side_effect=TimeoutError("sensitive text")
        with patch("pulse.collectors.binance_transport.build_opener",return_value=opener):
            with self.assertRaises(TimeoutError) as caught:
                fetch("/fapi/v1/openInterest",{},admission,{"history":0,"weight":1},None,timeout=3)
        self.assertEqual(caught.exception.source_endpoint,"openInterest")
        self.assertEqual(caught.exception.source_timeout_ms,3000)
        self.assertGreaterEqual(caught.exception.source_elapsed_ms,0)
        admission.feedback.assert_called_once_with(0,{})
