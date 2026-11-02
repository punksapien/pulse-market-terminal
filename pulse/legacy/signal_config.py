import math
from dataclasses import dataclass, field
from typing import List

@dataclass
class RegimeConfig:
    annual_thresholds: List[float] = field(default_factory=lambda: [0.20, 0.40, 0.75, 1.20])
    names: List[str] = field(default_factory=lambda: ["Calm", "Low", "Active", "Hot", "Extreme"])

@dataclass
class DirectionConfig:
    surge_short_long_log_ratio: float = math.log(1.30)
    surge_mid_long_log_ratio: float = math.log(1.20)
    surge_short_extreme_log_ratio: float = math.log(1.40)

    rise_long_macro_log_ratio: float = math.log(1.15)
    rise_mid_long_log_ratio: float = math.log(0.95)
    rise_short_vol_vel_min: float = 0.05
    rise_mid_vol_vel_min: float = -0.05

    contracting_macro_percentile_max: float = 0.20
    contracting_mid_long_log_ratio: float = math.log(0.90)

    fall_long_macro_log_ratio: float = math.log(1.10)
    fall_mid_long_log_ratio: float = math.log(0.85)

@dataclass
class SignalConfig:
    min_quality_score: float = 0.65
    min_dollar_vol_24h: float = 50_000.0

    mom_min_log_price_chg: float = 0.02  
    mom_min_log_oi_chg: float = 0.02
    mom_max_jump_fraction: float = 0.20
    mom_max_funding_long: float = 0.08
    mom_min_funding_short: float = -0.08
    mom_min_1h_price_chg: float = 0.01
    mom_min_oi_chg: float = 0.05

    rev_min_log_price_drop: float = -0.06
    rev_min_1h_price_drop: float = -0.03
    rev_min_log_price_pump: float = 0.08
    rev_min_1h_price_pump: float = 0.04
    rev_min_jump_fraction: float = 0.20
    rev_max_log_oi_chg: float = -0.02
    rev_max_funding_long: float = -0.02
    rev_min_oi_chg_short: float = 0.02
    rev_min_funding_short: float = 0.04

    bw_max_macro_percentile: float = 0.25
    bw_min_log_oi_chg: float = 0.002
    bw_min_quality_score: float = 0.75

    rng_max_abs_log_oi_chg: float = 0.03
    rng_max_abs_funding: float = 0.02


@dataclass
class OutlookConfig:
    oi_strong_rise_log: float = 0.05
    oi_moderate_rise_log: float = 0.01
    oi_strong_fall_log: float = -0.03

    funding_neutral_max_abs: float = 0.03
    funding_extreme_min_abs: float = 0.07

    jump_fraction_smooth_max: float = 0.15
    jump_fraction_heavy_min: float = 0.35

    quality_score_high_min: float = 0.85

    # Outlook weights/scores
    oi_strong_rise_weight: int = 30
    oi_moderate_rise_weight: int = 15
    oi_strong_fall_penalty: int = 20
    
    funding_neutral_weight: int = 20
    funding_extreme_penalty: int = 15
    
    vol_expansion_weight: int = 25
    vol_deceleration_penalty: int = 10
    
    smooth_trend_weight: int = 15
    heavy_jump_penalty: int = 25
    
    high_quality_weight: int = 10
    
    compression_bonus: int = 20
    
    persist_threshold: int = 55
    fade_threshold: int = 30
    
    fade_default_confidence: int = 45


@dataclass
class SizingConfig:
    target_daily_volatility: float = 0.04
    max_leverage_ceiling: float = 25.0

    min_combined_multiplier: float = 0.10

    quality_excellent_min: float = 0.85
    quality_excellent_penalty: float = 1.0

    quality_fair_min: float = 0.70
    quality_fair_penalty: float = 0.88
    quality_poor_penalty: float = 0.70

    liquidity_excellent_min_dv: float = 1_000_000
    liquidity_excellent_penalty: float = 1.0

    liquidity_fair_min_dv: float = 500_000
    liquidity_fair_penalty: float = 0.80
    liquidity_poor_penalty: float = 0.60


@dataclass
class ReadinessConfig:
    mom_surging_weight: int = 20
    mom_jump_max: float = 0.15
    mom_jump_weight: int = 15
    mom_oi_min: float = 0.03
    mom_oi_weight: int = 15
    mom_qs_min: float = 0.80
    mom_qs_weight: int = 10
    mom_dv_min: float = 500_000
    mom_dv_weight: int = 10
    mom_accel_min: float = 0.0
    mom_accel_weight: int = 15
    mom_vel_min: float = 0.1
    mom_vel_weight: int = 15

    rev_jump_min: float = 0.30
    rev_jump_weight: int = 20
    rev_fade_weight: int = 20
    rev_oi_max: float = -0.03
    rev_oi_weight: int = 15
    rev_fund_abs_min: float = 0.05
    rev_fund_weight: int = 15
    rev_qs_min: float = 0.85
    rev_qs_weight: int = 15
    rev_rsi_oversold: float = 30.0
    rev_rsi_overbought: float = 70.0
    rev_rsi_weight: int = 15

    bw_vpct_max: float = 0.15
    bw_vpct_weight: int = 25
    bw_contracting_weight: int = 20
    bw_oi_min: float = 0.01
    bw_oi_weight: int = 20
    bw_tightening_weight: int = 20
    bw_qs_min: float = 0.85
    bw_qs_weight: int = 15


@dataclass
class QuantReasonsConfig:
    oi_surging_min: float = 0.10
    oi_building_min: float = 0.03
    oi_falling_max: float = -0.05
    oi_div_oi_max: float = -0.02
    oi_div_price_min: float = 0.02

    funding_extreme_long: float = 0.08
    funding_elevated_long: float = 0.04
    funding_neutral_max_abs: float = 0.02
    funding_extreme_short: float = -0.04

    vol_short_accel_log_ratio: float = math.log(1.30)
    vol_macro_log_ratio: float = math.log(2.0)
    vpct_coiling_max: float = 0.15

    jump_smooth_max: float = 0.10
    jump_cascade_min: float = 0.35
    jump_moderate_min: float = 0.15

    rsi_oversold_max: float = 30.0
    rsi_overbought_min: float = 70.0


@dataclass
class RationaleConfig:
    cascade_jump_min: float = 0.40
    coil_oi_min: float = 0.02
    surge_oi_min: float = 0.08
    risk_funding_min: float = 0.07
    ratio_extreme_log_min: float = math.log(2.5)
    rsi_oversold_max: float = 30.0
    oi_rising_min: float = 0.02
    oi_falling_max: float = -0.02


@dataclass
class ContextConfig:
    hot_extreme_ratio_threshold: float = 0.30


@dataclass
class DynamicsConfig:
    acceleration_min: float = 0.05
    deceleration_max: float = -0.05


@dataclass
class PlaybookConfig:
    strong_uptrend_min: float = 0.05
    moderate_uptrend_min: float = 0.01
    strong_downtrend_max: float = -0.05
    moderate_downtrend_max: float = -0.01


@dataclass
class VolSignalConfig:
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    direction: DirectionConfig = field(default_factory=DirectionConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    outlook: OutlookConfig = field(default_factory=OutlookConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    readiness: ReadinessConfig = field(default_factory=ReadinessConfig)
    quant_reasons: QuantReasonsConfig = field(default_factory=QuantReasonsConfig)
    rationale: RationaleConfig = field(default_factory=RationaleConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    dynamics: DynamicsConfig = field(default_factory=DynamicsConfig)
    playbook: PlaybookConfig = field(default_factory=PlaybookConfig)
