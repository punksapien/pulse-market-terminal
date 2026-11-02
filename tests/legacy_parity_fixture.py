"""Deterministic offline inputs captured before ADR-0029 moves; not market data."""
import hashlib
import json
import math

import pandas as pd


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def frozen_outputs(core, scoring, signals, oi):
    outputs = {}
    for resolution, horizon in (('60', '1H'), ('240', '4H'), ('1D', '24H')):
        cfg = scoring.LWConfig(base=core.VolatilityConfig(resolution=resolution), scale_horizon=horizon)
        stamps = pd.date_range('2025-01-01', periods=256,
                               freq=f'{core._resolution_seconds(resolution)}s', tz='UTC')
        results = {}
        for kind in ('smooth', 'flat', 'jump', 'short', 'empty'):
            rows = []
            for i in range(256):
                price = 100 if kind == 'flat' else 100 * math.exp(.0002*i + .01*math.sin(i/9))
                if kind == 'jump' and i > 220:
                    price *= 1.3
                rows.append({'timestamp': stamps[i], 'open': price,
                             'high': price if kind == 'flat' else price*1.004,
                             'low': price if kind == 'flat' else price*.996,
                             'close': price if kind == 'flat' else price*1.001,
                             'volume': 1000+i})
            frame = pd.DataFrame(rows)
            if kind == 'short':
                frame = frame.head(8)
            if kind == 'empty':
                frame = frame.head(0)
            instrument = f'B-{kind.upper()}_USDT'
            results[instrument] = scoring.analyse_instrument_lw(instrument, frame, cfg)
        summary, context = scoring.score_cross_section(results, cfg)
        outputs[resolution] = digest(core._serialise({'summary': summary, 'context': context, 'instruments': results}))

    enriched = []
    for change in (-.2, -.06, -.02, 0, .02, .06, .2):
        for oi_change in (None, -.1, 0, .1):
            for short_vol in (.001, .01, .04):
                token = {'volatility_score': .04, 'vol_1h': short_vol, 'vol_4h': .01,
                         'vol_24h': .015, 'vol_7d_avg': .02, 'vol_percentile_30d': .1,
                         'price_change_24h': change, 'price_change_1h': 0,
                         'oi_available': oi_change is not None, 'oi_24h_change': oi_change,
                         'quality_score': 1, 'dollar_vol_24h': 5e6, 'volume_24h': 1e5,
                         'funding_rate': -.1 if change < -.1 else .1 if change > .1 else 0,
                         'jump_fraction': .5 if abs(change) > .1 else .05,
                         'price_tightening': True, 'rsi_14': 50}
                enriched.append(signals.enrich_token(token))
    outputs['signals_84'] = digest(core._serialise(enriched))
    outputs['market_context'] = digest(core._serialise(signals.compute_market_context(enriched)))
    histories = (
        [],
        [{'timestamp': 1000, 'sumOpenInterest': '10', 'sumOpenInterestValue': '100'},
         {'timestamp': 2000, 'sumOpenInterest': '12', 'sumOpenInterestValue': '125'}],
        [{'timestamp': 2000, 'openInterest': '12'}, {'timestamp': 1000, 'openInterest': '10'}],
        [{'timestamp': 'invalid', 'sumOpenInterestValue': 'bad'}, None],
    )
    outputs['oi'] = digest([oi.compute_oi_metrics(history, provider='bybit' if i == 2 else 'binance',
                                                price_hint=10, source_symbol='B-BTC_USDT')
                            for i, history in enumerate(histories)])
    return outputs
