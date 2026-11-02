import hashlib
import inspect
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from pulse.legacy.scoring import (LWConfig, analyse_instrument_lw, score_cross_section,
                                 compute_ewma_vol, compute_jump_variation, _score_band, _scale_bands)
from pulse.legacy.vol_core import VolatilityConfig, _yang_zhang_vol
from pulse.metrics.volatility import evaluate_v1
from volatility_fixture import candles


class VolatilityParityTests(unittest.TestCase):
    def test_exact_oracle_delegation_all_resolutions_horizons_and_frozen_outputs(self):
        fixture = json.loads((Path(__file__).parent/"fixtures/volatility/v1.json").read_text())
        for resolution in ("60","240","1D"):
            for horizon in ("1H","4H","24H"):
                frame = candles(resolution)
                self.assertEqual(hashlib.sha256(frame.to_json(orient="split",double_precision=15).encode()).hexdigest(),fixture["inputs"][resolution])
                cfg = LWConfig(VolatilityConfig(resolution=resolution),scale_horizon=horizon)
                direct = {"B-BTC_USDT":analyse_instrument_lw("B-BTC_USDT",frame,cfg)}
                rows, summary = score_cross_section(direct,cfg)
                with patch("requests.sessions.Session.request",side_effect=AssertionError("network forbidden")):
                    actual, actual_rows, actual_summary = evaluate_v1({"B-BTC_USDT":frame},cfg)
                # Ignore NaN equality by canonicalising the full numeric trees.
                self.assertEqual(json.dumps(actual,sort_keys=True),json.dumps(direct,sort_keys=True))
                self.assertEqual(json.dumps(actual_rows,sort_keys=True),json.dumps(rows,sort_keys=True))
                self.assertEqual(json.dumps(actual_summary,sort_keys=True),json.dumps(summary,sort_keys=True))
                expected = fixture["outputs"][resolution+"/"+horizon]
                for key, encoded in expected.items():
                    value = actual["B-BTC_USDT"][key]
                    if isinstance(value,str):
                        self.assertEqual(value,encoded)
                    else:
                        expected_value=float.fromhex(encoded)
                        self.assertTrue(math.isfinite(value) and math.isfinite(expected_value))
                        self.assertTrue(math.isclose(value,expected_value,rel_tol=1e-12,abs_tol=1e-15),
                                        (resolution,horizon,key,value,expected_value))

    def test_ewma_previous_return_recursion_and_yz_sample_variance(self):
        returns = pd.Series([1.,2.,3.,4.])
        result = compute_ewma_vol(returns,lam=.5)
        np.testing.assert_array_equal(result.values,np.sqrt([1.,1.,2.5,5.75]))
        frame = candles("60",22)
        o,h,l,c = (frame[key] for key in ("open","high","low","close"))
        result = _yang_zhang_vol(o,h,l,c,21)
        self.assertTrue(result.iloc[:21].isna().all())
        k = .34/(1.34+22/20)
        oc = np.log(o/c.shift(1)).iloc[-21:]
        co = np.log(c/o).iloc[-21:]
        rs = (np.log(h/c)*np.log(h/o)+np.log(l/c)*np.log(l/o)).iloc[-21:]
        expected = math.sqrt(max(0,oc.var(ddof=1)+k*co.var(ddof=1)+(1-k)*rs.mean()))
        self.assertAlmostEqual(result.iloc[-1],expected,places=15)

    def test_null_warmup_short_history_and_jump_clipping(self):
        self.assertTrue(math.isnan(compute_jump_variation(pd.Series([0.]*9))["jump_fraction"]))
        self.assertEqual(compute_jump_variation(pd.Series([0.]*10))["jump_fraction"],0)
        cfg = LWConfig(VolatilityConfig(resolution="60"),scale_horizon="1H")
        short,rows,_ = evaluate_v1({"B-BTC_USDT":candles("60",5)},cfg)
        self.assertIn("error",short["B-BTC_USDT"])
        self.assertEqual(rows,[])
        for jump, correction in ((-1,1),(0,1),(.95,math.sqrt(20)),(2,math.sqrt(20)),(float("nan"),1)):
            info = {"sigma_yz":float("nan"),"sigma_ewma":.1,"jump_fraction":jump}
            rows,_ = score_cross_section({"B-BTC_USDT":info},cfg)
            self.assertAlmostEqual(rows[0]["volatility_score"],.035*correction,places=15)

    def test_strict_band_edges(self):
        for seconds in (3600,14400,86400):
            bands = _scale_bands(seconds)
            for i,(low,high,label) in enumerate(bands):
                self.assertEqual(_score_band(low,seconds),label)
                if math.isfinite(high):
                    self.assertEqual(_score_band(np.nextafter(high,-np.inf),seconds),label)
                    self.assertEqual(_score_band(high,seconds),bands[i+1][2])
