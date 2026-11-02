"""One same-endpoint TLS handshake, certificate validation, zero Redis AUTH/data."""
import argparse
import json
from pathlib import Path
import socket
import ssl
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging


def handshake(host,port):
    context=ssl.create_default_context()
    try:
        with socket.create_connection((host,port),timeout=2) as raw:
            raw.settimeout(3)
            with context.wrap_socket(raw,server_hostname=host) as secure:
                return {"status":"tls_handshake_verified","tls_version":secure.version(),
                        "certificate_validation":True,"redis_auth_sent":False,"redis_data_commands":0}
    except ssl.SSLCertVerificationError as error:
        return {"status":"tls_certificate_verification_failed","verify_code":error.verify_code,
                "certificate_validation_disabled":False,"redis_auth_sent":False,"redis_data_commands":0}
    except (OSError,ssl.SSLError) as error:
        return {"status":"tls_handshake_failed","error_type":type(error).__name__,
                "redis_auth_sent":False,"redis_data_commands":0,
                "other_tls_endpoints_ruled_out":False}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vercel-cli",required=True)
    args=parser.parse_args()
    client=None
    try:
        # Identity/Preview-entry read only. This constructs a client but does not
        # open its plaintext connection or send its password.
        client,identity=connect_staging(args.vercel_cli,approved_plaintext=True)
        options=client.connection_pool.connection_kwargs
        result=handshake(options["host"],options["port"])
        result.update(resource="pulse-redis",environment="preview",
                      connector_changed=False,production_activation=False)
        print(json.dumps(result,indent=2))
    except Exception as error:
        print(json.dumps({"status":"probe_failed","error_type":type(error).__name__}))
        raise SystemExit(1) from None
    finally:
        if client is not None:
            client.close()
