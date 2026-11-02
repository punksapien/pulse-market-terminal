"""ADR-0033: legacy in-memory scaling with explicit cross-sectional scorer."""
from pulse.legacy.vol_core import _resolution_seconds, _finite
from pulse.legacy.scoring import _parse_horizon_to_seconds


def scale_and_score_report(base_report, target_horizon, cfg, *, score):
    """
    In-memory re-scaling of standard deviations, sparklines, and timeseries charts,
    followed by cross-sectional scoring. Executed in <5ms.
    """
    score_cross_section = score
    import copy
    import math
    
    report = copy.deepcopy(base_report)
    
    res = report["meta"].get("resolution", "60")
    candle_secs = _resolution_seconds(res)
    target_secs = _parse_horizon_to_seconds(target_horizon)
    S = math.sqrt(target_secs / candle_secs)
    
    # Update scaling metadata
    report["meta"]["scale_horizon"] = target_horizon
    report["meta"]["target_seconds"] = target_secs
    report["meta"]["scale_factor"] = S
    
    # Scale instrument level volatility metrics
    for inst, info in report["instruments"].items():
        if "error" in info:
            continue
        info.setdefault("meta", {})["scale_factor"] = S
        
        # Linear scaling of vol scores
        for k in ("sigma_yz", "sigma_ewma", "bvol_window", "vol_1h", "vol_4h", "vol_24h", "vol_7d", "vol_7d_avg", "vol_30d_avg"):
            if k in info and info[k] is not None:
                info[k] = info[k] * S
        
        # Scale sparkline vectors
        if "vol_sparkline_24h" in info and info["vol_sparkline_24h"]:
            info["vol_sparkline_24h"] = [round(v * S, 5) for v in info["vol_sparkline_24h"]]
            
        # Scale downsampled timeseries charts
        if "timeseries" in info:
            ts = info["timeseries"]
            for k in ("yz", "ewma", "bvol"):
                if k in ts and isinstance(ts[k], dict) and "y" in ts[k]:
                    ts[k]["y"] = [v * S for v in ts[k]["y"]]
                    
    # Recompute cross-sectional scores and guidance
    summary_rows, cross_section = score_cross_section(report["instruments"], cfg)
    
    if cfg.min_volume_usd > 0:
        summary_rows = [
            r for r in summary_rows
            if _finite(r.get("volume_24h"), 0) >= cfg.min_volume_usd * 1e6
        ]
        
    report["summary"] = summary_rows
    report["cross_section"] = cross_section
    return report

