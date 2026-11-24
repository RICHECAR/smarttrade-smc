"""
Tests for Phase 9 — Market Regime Detector

Covers:
  - ADX computation from candle data
  - ATR compression ratio
  - BOS confirmation rate
  - Regime classification (TREND / RANGE / CHOP)
  - Regime caps lookup
  - Full analysis with raw and pre-computed inputs
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reporting.regime_detector import (
    compute_adx,
    compute_atr_ratio,
    compute_bos_confirm_rate,
    classify_regime,
    get_regime_caps,
    analyze_market_regime,
    REGIME_TREND,
    REGIME_RANGE,
    REGIME_CHOP,
    ADX_TREND_MIN,
    ADX_RANGE_MAX,
    ATR_RATIO_EXPANDING,
    BOS_CONFIRM_TREND,
    BOS_CONFIRM_CHOP,
    MIN_BOS_EVENTS,
)


# ── ADX Tests ────────────────────────────────────────────────────────

class TestComputeADX:

    def test_empty_candles_returns_zero(self):
        assert compute_adx([]) == 0.0

    def test_insufficient_candles_returns_zero(self):
        candles = [{"high": 10, "low": 9, "close": 9.5}] * 10
        assert compute_adx(candles, period=14) == 0.0

    def test_trending_candles_high_adx(self):
        """Monotonically rising candles should produce ADX > 25."""
        candles = []
        for i in range(50):
            candles.append({
                "high": 100 + i * 2.0,
                "low": 99 + i * 2.0,
                "close": 99.5 + i * 2.0,
            })
        adx = compute_adx(candles, period=14)
        assert adx > ADX_TREND_MIN, f"ADX={adx} should be > {ADX_TREND_MIN} for trend"

    def test_flat_candles_low_adx(self):
        """Flat candles oscillating around same level → low ADX."""
        candles = []
        for i in range(50):
            mid = 100 + (i % 2) * 0.5
            candles.append({
                "high": mid + 0.5,
                "low": mid - 0.5,
                "close": mid,
            })
        adx = compute_adx(candles, period=14)
        assert adx < ADX_TREND_MIN, f"ADX={adx} should be < {ADX_TREND_MIN} for range"

    def test_returns_float(self):
        candles = [{"high": 100 + i, "low": 99 + i, "close": 99.5 + i} for i in range(30)]
        adx = compute_adx(candles, period=14)
        assert isinstance(adx, float)


# ── ATR Ratio Tests ──────────────────────────────────────────────────

class TestATRRatio:

    def test_empty_returns_one(self):
        assert compute_atr_ratio([]) == 1.0

    def test_single_value_returns_one(self):
        assert compute_atr_ratio([1.0]) == 1.0

    def test_constant_series_returns_one(self):
        vals = [1.0] * 20
        ratio = compute_atr_ratio(vals)
        assert ratio == 1.0

    def test_expanding_atr(self):
        """Last value much higher than average → ratio > 1.5."""
        vals = [1.0] * 19 + [3.0]
        ratio = compute_atr_ratio(vals)
        assert ratio > 1.4, f"Ratio={ratio} should show expansion"

    def test_compressing_atr(self):
        """Last value much lower than average → ratio < 0.8."""
        vals = [2.0] * 19 + [0.5]
        ratio = compute_atr_ratio(vals)
        assert ratio < 0.8, f"Ratio={ratio} should show compression"


# ── BOS Confirm Rate Tests ───────────────────────────────────────────

class TestBOSConfirmRate:

    def test_insufficient_events_returns_one(self):
        assert compute_bos_confirm_rate(2, 1) == 1.0

    def test_all_confirmed(self):
        assert compute_bos_confirm_rate(10, 10) == 1.0

    def test_none_confirmed(self):
        assert compute_bos_confirm_rate(10, 0) == 0.0

    def test_half_confirmed(self):
        rate = compute_bos_confirm_rate(10, 5)
        assert rate == 0.5


# ── Regime Classification Tests ──────────────────────────────────────

class TestClassifyRegime:

    def test_trend_high_adx_good_bos(self):
        regime, reason = classify_regime(adx=30, atr_ratio=1.0, bos_confirm_rate=0.6)
        assert regime == REGIME_TREND
        assert "ADX=30.0" in reason

    def test_range_moderate_adx(self):
        regime, reason = classify_regime(adx=18, atr_ratio=1.0, bos_confirm_rate=0.5)
        assert regime == REGIME_RANGE

    def test_chop_low_adx_high_atr_expansion(self):
        regime, reason = classify_regime(adx=15, atr_ratio=1.8, bos_confirm_rate=0.5)
        assert regime == REGIME_CHOP

    def test_chop_low_adx_low_bos(self):
        regime, reason = classify_regime(adx=18, atr_ratio=1.0, bos_confirm_rate=0.2)
        assert regime == REGIME_CHOP

    def test_trend_requires_bos_confirmation(self):
        """High ADX but low BOS → NOT trend."""
        regime, _ = classify_regime(adx=30, atr_ratio=1.0, bos_confirm_rate=0.3)
        assert regime != REGIME_TREND

    def test_range_when_high_adx_but_low_bos(self):
        """ADX=30 but BOS=40% (not trend, not chop) → RANGE."""
        regime, _ = classify_regime(adx=30, atr_ratio=1.0, bos_confirm_rate=0.4)
        assert regime == REGIME_RANGE


# ── Regime Caps Tests ────────────────────────────────────────────────

class TestRegimeCaps:

    def test_trend_caps(self):
        caps = get_regime_caps(REGIME_TREND)
        assert caps["adaptive_weight_cap"] == 0.5
        assert caps["scaling_cap_factor"] == 1.0
        assert caps["risk_reduction_factor"] == 1.0

    def test_range_caps(self):
        caps = get_regime_caps(REGIME_RANGE)
        assert caps["adaptive_weight_cap"] == 0.3
        assert caps["scaling_cap_factor"] == 0.8

    def test_chop_caps(self):
        caps = get_regime_caps(REGIME_CHOP)
        assert caps["adaptive_weight_cap"] == 0.1
        assert caps["scaling_cap_factor"] == 0.5
        assert caps["risk_reduction_factor"] == 0.7

    def test_unknown_regime_defaults_to_range(self):
        caps = get_regime_caps("UNKNOWN_REGIME")
        assert caps["adaptive_weight_cap"] == 0.3  # RANGE default


# ── Full Analysis Tests ──────────────────────────────────────────────

class TestAnalyzeMarketRegime:

    def test_precomputed_trend(self):
        result = analyze_market_regime(adx=30, atr_ratio=1.0, bos_confirm_rate=0.6)
        assert result["regime"] == REGIME_TREND
        assert result["adaptive_weight_cap"] == 0.5

    def test_precomputed_chop(self):
        result = analyze_market_regime(adx=10, atr_ratio=2.0, bos_confirm_rate=0.2)
        assert result["regime"] == REGIME_CHOP
        assert result["adaptive_weight_cap"] == 0.1

    def test_no_adx_defaults_to_range(self):
        result = analyze_market_regime(adx=0.0, atr_ratio=1.0, bos_confirm_rate=1.0)
        assert result["regime"] == REGIME_RANGE
        assert len(result["warnings"]) > 0  # should warn about missing ADX

    def test_raw_candles_computes_adx(self):
        """Provide raw candles → ADX computed internally."""
        candles = [
            {"high": 100 + i * 2.0, "low": 99 + i * 2.0, "close": 99.5 + i * 2.0}
            for i in range(50)
        ]
        result = analyze_market_regime(candles=candles, bos_confirm_rate=0.7)
        assert result["adx"] > 0
        assert result["regime"] in (REGIME_TREND, REGIME_RANGE, REGIME_CHOP)

    def test_raw_atr_values_computes_ratio(self):
        vals = [1.0] * 19 + [3.0]
        result = analyze_market_regime(adx=10, atr_values=vals, bos_confirm_rate=0.5)
        assert result["atr_ratio"] > 1.4

    def test_raw_bos_counts_computes_rate(self):
        result = analyze_market_regime(adx=30, atr_ratio=1.0, bos_total=10, bos_confirmed=8)
        assert result["bos_confirm_rate"] == 0.8
        assert result["regime"] == REGIME_TREND

    def test_result_has_all_keys(self):
        result = analyze_market_regime(adx=20, atr_ratio=1.0, bos_confirm_rate=0.5)
        expected_keys = [
            "regime", "reason", "adx", "atr_ratio", "bos_confirm_rate",
            "adaptive_weight_cap", "scaling_cap_factor", "risk_reduction_factor",
            "description", "warnings",
        ]
        for k in expected_keys:
            assert k in result, f"Missing key: {k}"
