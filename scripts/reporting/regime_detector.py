"""
Market Regime Detector — Phase 9

Classifies market into TREND / RANGE / CHOP using three orthogonal signals:
  1. ADX (directional strength)
  2. ATR compression ratio (current vs rolling average)
  3. BOS confirmation rate (confirmed / total BOS events)

Each regime maps to caps on adaptive weight and capital scaling
via a deterministic decision table.

Design:
  - Pure functions, deterministic, fully testable
  - Called from DailyReporter.evaluate_regime_step()
  - Inputs are pre-computed by the caller (no DB/API calls here)

Author: SMC Trading Bot Framework — Phase 9
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Regime Types ──────────────────────────────────────────────────────
REGIME_TREND = "TREND"
REGIME_RANGE = "RANGE"
REGIME_CHOP = "CHOP"

# ── ADX Thresholds ────────────────────────────────────────────────────
ADX_TREND_MIN = 25.0       # ADX >= 25 → directional trend
ADX_RANGE_MAX = 20.0       # ADX < 20  → range-bound

# ── ATR Compression Ratio Thresholds ─────────────────────────────────
# ratio = current_atr / avg_atr_20
ATR_RATIO_COMPRESSED = 0.8   # ratio < 0.8 → compression (range-like)
ATR_RATIO_EXPANDING = 1.5    # ratio > 1.5 → expansion (potentially choppy)

# ── BOS Confirmation Rate Thresholds ─────────────────────────────────
BOS_CONFIRM_TREND = 0.50    # >= 50% confirmed → trending structure
BOS_CONFIRM_CHOP = 0.30     # < 30% confirmed → choppy / fake BOS

# ── Decision Table: Regime → Caps ─────────────────────────────────────
REGIME_CAPS = {
    REGIME_TREND: {
        "adaptive_weight_cap": 0.5,      # full freedom
        "scaling_cap_factor": 1.0,       # no restriction
        "risk_reduction_factor": 1.0,    # no reduction
        "description": "Trending market — full adaptive & scaling freedom",
    },
    REGIME_RANGE: {
        "adaptive_weight_cap": 0.3,      # cap at 30%
        "scaling_cap_factor": 0.8,       # reduce max scaling by 20%
        "risk_reduction_factor": 0.85,   # 15% risk reduction
        "description": "Range-bound market — moderate cap on adaptive & scaling",
    },
    REGIME_CHOP: {
        "adaptive_weight_cap": 0.1,      # cap at 10% (near shadow)
        "scaling_cap_factor": 0.5,       # halve max scaling
        "risk_reduction_factor": 0.7,    # 30% risk reduction
        "description": "Choppy market — aggressive caps, minimal exposure",
    },
}

# ── Minimum data requirements ─────────────────────────────────────────
MIN_BOS_EVENTS = 3          # need ≥3 BOS events to compute confirm rate
MIN_ATR_PERIODS = 5         # need ≥5 ATR observations for average


# ── ADX Computation (from candle data) ────────────────────────────────

def compute_adx(candles: List[Dict[str, float]], period: int = 14) -> float:
    """
    Compute ADX from OHLC candle data.

    Each candle dict expected keys: high, low, close.
    Returns ADX value (0-100). Returns 0.0 if insufficient data.

    Uses Wilder's smoothing method.
    """
    if len(candles) < period + 1:
        return 0.0

    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        prev_high = candles[i - 1]["high"]
        prev_low = candles[i - 1]["low"]

        # True Range
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        tr_list.append(tr)

        # Directional Movement
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    if len(tr_list) < period:
        return 0.0

    # Wilder's smoothing (initial SMA, then EMA-like)
    atr = sum(tr_list[:period]) / period
    plus_di_smooth = sum(plus_dm_list[:period]) / period
    minus_di_smooth = sum(minus_dm_list[:period]) / period

    dx_list = []
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        plus_di_smooth = (plus_di_smooth * (period - 1) + plus_dm_list[i]) / period
        minus_di_smooth = (minus_di_smooth * (period - 1) + minus_dm_list[i]) / period

        if atr > 0:
            plus_di = 100 * plus_di_smooth / atr
            minus_di = 100 * minus_di_smooth / atr
        else:
            plus_di = 0.0
            minus_di = 0.0

        di_sum = plus_di + minus_di
        if di_sum > 0:
            dx = 100 * abs(plus_di - minus_di) / di_sum
        else:
            dx = 0.0
        dx_list.append(dx)

    if not dx_list:
        return 0.0

    # ADX = smoothed DX (Wilder)
    adx = sum(dx_list[:period]) / period if len(dx_list) >= period else sum(dx_list) / len(dx_list)
    for i in range(period, len(dx_list)):
        adx = (adx * (period - 1) + dx_list[i]) / period

    return round(adx, 2)


def compute_atr_ratio(
    atr_values: List[float],
    lookback: int = 20,
) -> float:
    """
    Compute ATR compression/expansion ratio.

    ratio = current_atr / avg(last `lookback` ATR values)

    Returns:
        float: ratio > 1.0 = expanding, < 1.0 = compressing.
        Returns 1.0 if insufficient data.
    """
    if not atr_values or len(atr_values) < 2:
        return 1.0

    current = atr_values[-1]
    history = atr_values[-lookback:] if len(atr_values) >= lookback else atr_values
    avg_atr = sum(history) / len(history)

    if avg_atr <= 0:
        return 1.0

    return round(current / avg_atr, 4)


def compute_bos_confirm_rate(
    bos_total: int,
    bos_confirmed: int,
) -> float:
    """
    Compute BOS confirmation rate.

    Returns:
        float: ratio of confirmed BOS / total BOS attempts.
        Returns 1.0 if no BOS events (assume neutral).
    """
    if bos_total < MIN_BOS_EVENTS:
        return 1.0  # insufficient data → assume neutral (don't penalize)

    return round(bos_confirmed / bos_total, 4)


# ── Core Classification ──────────────────────────────────────────────

def classify_regime(
    adx: float,
    atr_ratio: float,
    bos_confirm_rate: float,
) -> Tuple[str, str]:
    """
    Classify market regime from 3 orthogonal indicators.

    Decision logic:
      1. TREND: ADX >= 25 AND bos_confirm >= 50%
      2. CHOP:  ADX < 25 AND (atr_ratio > 1.5 OR bos_confirm < 30%)
      3. RANGE: everything else (moderate ADX, compressed ATR, decent BOS)

    Returns:
        (regime, reason) — regime is one of TREND/RANGE/CHOP
    """
    # ── TREND: strong direction confirmed by structure ──
    if adx >= ADX_TREND_MIN and bos_confirm_rate >= BOS_CONFIRM_TREND:
        return REGIME_TREND, (
            f"ADX={adx:.1f}>={ADX_TREND_MIN}, "
            f"BOS_confirm={bos_confirm_rate*100:.0f}%>={BOS_CONFIRM_TREND*100:.0f}%"
        )

    # ── CHOP: weak direction + expansion or fake BOS ──
    if adx < ADX_TREND_MIN and (
        atr_ratio > ATR_RATIO_EXPANDING or bos_confirm_rate < BOS_CONFIRM_CHOP
    ):
        reasons = []
        if atr_ratio > ATR_RATIO_EXPANDING:
            reasons.append(f"ATR_ratio={atr_ratio:.2f}>{ATR_RATIO_EXPANDING}")
        if bos_confirm_rate < BOS_CONFIRM_CHOP:
            reasons.append(f"BOS_confirm={bos_confirm_rate*100:.0f}%<{BOS_CONFIRM_CHOP*100:.0f}%")
        return REGIME_CHOP, f"ADX={adx:.1f}<{ADX_TREND_MIN}, " + ", ".join(reasons)

    # ── RANGE: moderate, contained ──
    return REGIME_RANGE, (
        f"ADX={adx:.1f}<{ADX_TREND_MIN}, "
        f"ATR_ratio={atr_ratio:.2f} contained, "
        f"BOS_confirm={bos_confirm_rate*100:.0f}% adequate"
    )


def get_regime_caps(regime: str) -> Dict[str, Any]:
    """Get the decision table caps for a given regime."""
    return {**REGIME_CAPS.get(regime, REGIME_CAPS[REGIME_RANGE])}


# ── Full Analysis ─────────────────────────────────────────────────────

def analyze_market_regime(
    adx: float = 0.0,
    atr_ratio: float = 1.0,
    bos_confirm_rate: float = 1.0,
    candles: Optional[List[Dict[str, float]]] = None,
    atr_values: Optional[List[float]] = None,
    bos_total: int = 0,
    bos_confirmed: int = 0,
) -> Dict[str, Any]:
    """
    Full regime analysis. Accepts either pre-computed metrics or raw data.

    If candles are provided, ADX is computed from them.
    If atr_values are provided, atr_ratio is computed from them.
    If bos_total/bos_confirmed are provided, bos_confirm_rate is computed.

    Pre-computed values (adx, atr_ratio, bos_confirm_rate) take precedence
    when they are non-default AND raw data is also provided.

    Returns:
        Dict with keys:
            regime, reason, adx, atr_ratio, bos_confirm_rate,
            adaptive_weight_cap, scaling_cap_factor, risk_reduction_factor,
            description, warnings
    """
    warnings = []

    # ── Compute metrics from raw data if provided ──
    computed_adx = adx
    if candles and adx == 0.0:
        computed_adx = compute_adx(candles)
        if computed_adx == 0.0 and len(candles) > 0:
            warnings.append("ADX computation returned 0 — insufficient candle data")

    computed_atr_ratio = atr_ratio
    if atr_values and atr_ratio == 1.0:
        computed_atr_ratio = compute_atr_ratio(atr_values)

    computed_bos_rate = bos_confirm_rate
    if bos_total > 0 and bos_confirm_rate == 1.0:
        computed_bos_rate = compute_bos_confirm_rate(bos_total, bos_confirmed)

    # ── Data quality warnings ──
    if computed_adx == 0.0:
        warnings.append("ADX not available — regime defaults to RANGE")
        computed_adx = ADX_RANGE_MAX  # default to range-like

    # ── Classify ──
    regime, reason = classify_regime(
        adx=computed_adx,
        atr_ratio=computed_atr_ratio,
        bos_confirm_rate=computed_bos_rate,
    )

    caps = get_regime_caps(regime)

    logger.info(
        f"[REGIME] {regime}: {reason} "
        f"(cap={caps['adaptive_weight_cap']}, "
        f"scale={caps['scaling_cap_factor']})"
    )

    return {
        "regime": regime,
        "reason": reason,
        "adx": computed_adx,
        "atr_ratio": computed_atr_ratio,
        "bos_confirm_rate": computed_bos_rate,
        "adaptive_weight_cap": caps["adaptive_weight_cap"],
        "scaling_cap_factor": caps["scaling_cap_factor"],
        "risk_reduction_factor": caps["risk_reduction_factor"],
        "description": caps["description"],
        "warnings": warnings,
    }
