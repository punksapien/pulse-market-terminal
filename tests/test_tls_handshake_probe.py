import importlib.util
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import MagicMock,patch

PROBES=Path(__file__).resolve().parents[1]/"tools/probes"
spec=importlib.util.spec_from_file_location("tls_handshake_probe",PROBES/"pulse_staging_tls_handshake.py")
module=importlib.util.module_from_spec(spec)
sys.path.insert(0,str(PROBES))
try:
    spec.loader.exec_module(module)
finally:
    sys.path.pop(0)


class TLSHandshakeTests(unittest.TestCase):
    def test_validated_handshake_sends_no_redis_auth(self):
        context=MagicMock()
        context.wrap_socket.return_value.__enter__.return_value.version.return_value="TLSv1.3"
        with patch.object(module.ssl,"create_default_context",return_value=context),patch.object(module.socket,"create_connection"):
            result=module.handshake("fixture.invalid",6380)
        self.assertEqual(result["status"],"tls_handshake_verified")
        self.assertFalse(result["redis_auth_sent"])
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"],"fixture.invalid")

    def test_timeout_has_no_plaintext_or_unverified_fallback(self):
        with patch.object(module.socket,"create_connection",side_effect=TimeoutError()):
            result=module.handshake("fixture.invalid",6380)
        self.assertEqual(result["status"],"tls_handshake_failed")
        self.assertFalse(result["redis_auth_sent"])
        self.assertFalse(result["other_tls_endpoints_ruled_out"])

    def test_certificate_error_never_disables_verification(self):
        error=ssl.SSLCertVerificationError(1,"synthetic")
        error.verify_code=10
        with patch.object(module.socket,"create_connection",side_effect=error):
            result=module.handshake("fixture.invalid",6380)
        self.assertEqual(result["status"],"tls_certificate_verification_failed")
        self.assertEqual(result["verify_code"],10)
        self.assertFalse(result["certificate_validation_disabled"])
