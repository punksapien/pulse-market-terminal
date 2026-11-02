# Exchange probes (read-only)

These scripts produced the evidence in [docs/exchange_data_sources.md](../../docs/exchange_data_sources.md) and the ADRs. They make **public market-data requests only**: no credentials, and they never touch application Redis. Run them from a dev machine, never from production. Keep request rates polite (each script paces itself).

| Script | What it measures |
|---|---|
| `rest_probe.py` / `rest_validate.py` | REST endpoints on CoinDCX, Binance and Bybit: status, latency, size, fields, cross-venue agreement |
| `stream_probe.py` | Concurrent WebSocket streams: rate, bandwidth, event → receive latency, CoinDCX-vs-Binance lag |
| `scale_probe.py` | One-connection scale (Binance 1,006 streams, CoinDCX 100 channels), upstream cadence, CoinDCX candle resolutions |
| `ls_cadence.py` | How often CoinDCX L/S changes upstream (15-min poll) |
| `vol_bench.py` | CPU cost of the existing volatility calculation (imports repo code read-only) |
| `ladder_size.py` | Reference-ladder payload size from real CoinDCX 1-min opens (ADR-0019) |
| `record_24h_fixture.py` | Records `tests/fixtures/price_change/` fixtures proving the 24h method (ADR-0005) |

Setup: `pip install requests websockets "python-socketio[client]==4.6.1" "python-engineio==3.14.2" aiohttp brotli numpy pandas`. Run from this folder. On a TLS-inspecting network, set `PULSE_EXTRA_CA_FILE=/path/to/corp_ca.pem` (verification stays on). Python builds without a system CA store also need `SSL_CERT_FILE` pointing at certifi's bundle for WebSocket libraries.

Raw outputs (`*.json` results) are written to the current folder and are **not** committed. Commit only small, sanitized fixtures under `tests/fixtures/`.
