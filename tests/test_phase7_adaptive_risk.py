"""
Phase 7 — Tests: Adaptive Risk Engine
══════════════════════════════════════
Covers:
  A. Core logic:
    - Normal conditions → mult=1.0, mode=normal
    - Volatility: reduced / defensive / blocked
    - Drawdown: reduced / defensive / blocked
    - Performance: low WR, low PF, losing edge
    - Execution quality: slippage, fill delay, kills
    - Regime × strategy compatibility
    - Combinations of multiple factors
  B. Integration:
    - RiskDecision.to_dict() serialization
    - Disabled engine → passthrough
    - build_risk_inputs() from signal_dict
    - Governor recording of adaptive decisions
    - Prometheus metric wiring
  C. Configuration:
    - ENV loading
    - Immutable config (frozen dataclass)
"""

import pytest
from unittest.mock import patch, MagicMock
from dataclasses import FrozenInstanceError

from smc_futures.engine.risk.adaptive_risk_engine import (
    AdaptiveRiskConfig,
    AdaptiveRiskEngine,
    RiskInputs,
    RiskDecision,
    _worst_mode,
    build_risk_inputs,
)


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def _default_config(**overrides) -> AdaptiveRiskConfig:
    """Build config with explicit defaults for test isolation."""
    defaults = dict(
        enabled=True,
        min_mult=0.25,
        max_mult=1.00,
        defensive_mult=0.50,
        atr_reduced=1.5,
        atr_defensive=2.0,
        atr_block=2.8,
        block_on_extreme_vol=True,
        dd_reduced_pct=1.5,
        dd_defensive_pct=2.5,
        dd_block_pct=3.5,
        wr_window=20,
        min_wr=0.45,
        min_pf=1.05,
        max_recent_kills=2,
        max_recent_slippage_bps=35.0,
        max_recent_fill_delay_ms=2000.0,
        block_on_execution_degrade=True,
        reduce_in_low_vol=True,
        reduce_in_range_for_breakout=True,
        reduce_in_trend_for_meanrev=True,
    )
    defaults.update(overrides)
    return AdaptiveRiskConfig(**defaults)


def _engine(**config_overrides) -> AdaptiveRiskEngine:
    """Create engine with explicit config (no ENV dependency)."""
    return AdaptiveRiskEngine(config=_default_config(**config_overrides))


def _inputs(**overrides) -> RiskInputs:
    """Create normal inputs with overrides."""
    defaults = dict(
        atr_ratio=1.0,
        daily_dd_pct=0.0,
        rolling_dd_pct=0.0,
        recent_wr=None,
        recent_pf=None,
        recent_trades_count=0,
        regime="NORMAL",
        strategy_type="",
        avg_slippage_bps=0.0,
        avg_fill_delay_ms=0.0,
        recent_kills=0,
        base_risk_pct=0.01,
        base_leverage=3,
    )
    defaults.update(overrides)
    return RiskInputs(**defaults)


# ═══════════════════════════════════════════════════════════
# A1. Normal conditions
# ═══════════════════════════════════════════════════════════

class TestNormalConditions:
    """All factors nominal → no reduction."""

    def test_normal_mode_and_multiplier(self):
        """T1: Normal inputs → mult=1.0, mode=normal."""
        eng = _engine()
        d = eng.evaluate(_inputs())
        assert d.mode == "normal"
        assert d.risk_multiplier == 1.0
        assert d.size_multiplier == 1.0
        assert "all_factors_nominal" in d.reasons

    def test_effective_risk_equals_base(self):
        """T2: Normal → effective_risk = base_risk × 1.0."""
        eng = _engine()
        d = eng.evaluate(_inputs(base_risk_pct=0.02))
        assert d.effective_risk_pct == pytest.approx(0.02)

    def test_leverage_unchanged(self):
        """T3: Normal → leverage = base_leverage."""
        eng = _engine()
        d = eng.evaluate(_inputs(base_leverage=5))
        assert d.effective_leverage == 5


# ═══════════════════════════════════════════════════════════
# A2. Volatility factor
# ═══════════════════════════════════════════════════════════

class TestVolatilityFactor:
    """ATR ratio-based adjustments."""

    def test_below_reduced_threshold(self):
        """T4: atr_ratio=1.4 (< 1.5) → no volatility adjustment."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=1.4))
        assert d.mode == "normal"
        assert d.risk_multiplier == 1.0

    def test_reduced_mode(self):
        """T5: atr_ratio=1.6 → reduced, mult=0.75."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=1.6))
        assert d.mode == "reduced"
        assert d.risk_multiplier == pytest.approx(0.75)
        assert any("vol_reduced" in r for r in d.reasons)

    def test_defensive_mode(self):
        """T6: atr_ratio=2.2 → defensive, mult=0.50."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=2.2))
        assert d.mode == "defensive"
        assert d.risk_multiplier == pytest.approx(0.50)
        assert any("vol_defensive" in r for r in d.reasons)

    def test_defensive_halves_leverage(self):
        """T7: Defensive mode → leverage halved."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=2.2, base_leverage=6))
        assert d.effective_leverage == 3  # 6 // 2

    def test_blocked_on_extreme_vol(self):
        """T8: atr_ratio=3.0 → blocked."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=3.0))
        assert d.mode == "blocked"
        assert d.risk_multiplier == 0.0
        assert any("vol_extreme" in r for r in d.reasons)

    def test_blocked_vol_disabled(self):
        """T9: block_on_extreme_vol=False → not blocked, defensive instead."""
        eng = _engine(block_on_extreme_vol=False)
        d = eng.evaluate(_inputs(atr_ratio=3.0))
        # Should apply atr_defensive (2.0 threshold passed), not block
        assert d.mode == "defensive"
        assert d.risk_multiplier > 0.0


# ═══════════════════════════════════════════════════════════
# A3. Drawdown factor
# ═══════════════════════════════════════════════════════════

class TestDrawdownFactor:
    """Drawdown-based adjustments."""

    def test_no_drawdown(self):
        """T10: dd=0 → no adjustment."""
        eng = _engine()
        d = eng.evaluate(_inputs(daily_dd_pct=0.0))
        assert d.mode == "normal"
        assert d.risk_multiplier == 1.0

    def test_reduced_drawdown(self):
        """T11: dd=1.8% → reduced, mult=0.70."""
        eng = _engine()
        d = eng.evaluate(_inputs(daily_dd_pct=1.8))
        assert d.mode == "reduced"
        assert d.risk_multiplier == pytest.approx(0.70)
        assert any("dd_reduced" in r for r in d.reasons)

    def test_defensive_drawdown(self):
        """T12: dd=2.8% → defensive, mult=0.50."""
        eng = _engine()
        d = eng.evaluate(_inputs(daily_dd_pct=2.8))
        assert d.mode == "defensive"
        assert d.risk_multiplier == pytest.approx(0.50)
        assert any("dd_defensive" in r for r in d.reasons)

    def test_blocked_drawdown(self):
        """T13: dd=4.0% → blocked."""
        eng = _engine()
        d = eng.evaluate(_inputs(daily_dd_pct=4.0))
        assert d.mode == "blocked"
        assert d.risk_multiplier == 0.0
        assert any("dd_critical" in r for r in d.reasons)

    def test_rolling_dd_takes_priority(self):
        """T14: rolling_dd > daily_dd → uses worst."""
        eng = _engine()
        d = eng.evaluate(_inputs(daily_dd_pct=0.5, rolling_dd_pct=4.0))
        assert d.mode == "blocked"


# ═══════════════════════════════════════════════════════════
# A4. Performance factor (WR / PF)
# ═══════════════════════════════════════════════════════════

class TestPerformanceFactor:
    """Win rate and profit factor adjustments."""

    def test_insufficient_data_no_adjustment(self):
        """T15: Too few trades → no performance adjustment."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            recent_wr=0.20, recent_pf=0.5,
            recent_trades_count=3,  # < 5 minimum
        ))
        assert d.mode == "normal"
        assert d.risk_multiplier == 1.0

    def test_low_wr_proportional_reduction(self):
        """T16: WR=0.30 (< 0.45) → proportional reduction."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            recent_wr=0.30, recent_pf=1.5,
            recent_trades_count=20,
        ))
        # wr_ratio = 0.30 / 0.45 ≈ 0.6667
        expected_mult = max(0.5, 0.30 / 0.45)
        assert d.risk_multiplier == pytest.approx(expected_mult, abs=0.01)
        assert any("low_wr" in r for r in d.reasons)

    def test_very_low_wr_floored_at_05(self):
        """T17: WR=0.10 → mult floored at 0.50 (from WR alone)."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            recent_wr=0.10, recent_pf=1.5,
            recent_trades_count=20,
        ))
        # wr_ratio = 0.10/0.45 ≈ 0.22 → clamped to 0.5
        assert d.risk_multiplier == pytest.approx(0.50, abs=0.01)

    def test_losing_edge_pf_below_1(self):
        """T18: PF < 1.0 → ×0.60, defensive mode."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            recent_wr=0.50, recent_pf=0.80,
            recent_trades_count=20,
        ))
        assert d.risk_multiplier == pytest.approx(0.60, abs=0.01)
        assert d.mode == "defensive"
        assert any("losing_edge" in r for r in d.reasons)

    def test_weak_pf_mild_reduction(self):
        """T19: PF between 1.0 and min_pf → ×0.85, reduced mode."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            recent_wr=0.50, recent_pf=1.02,
            recent_trades_count=20,
        ))
        assert d.risk_multiplier == pytest.approx(0.85, abs=0.01)
        assert d.mode == "reduced"
        assert any("weak_pf" in r for r in d.reasons)


# ═══════════════════════════════════════════════════════════
# A5. Execution quality factor
# ═══════════════════════════════════════════════════════════

class TestExecutionQualityFactor:
    """Slippage, fill delay, kills."""

    def test_high_slippage_reduction(self):
        """T20: slippage > threshold → ×0.75, reduced."""
        eng = _engine()
        d = eng.evaluate(_inputs(avg_slippage_bps=50.0))
        assert d.risk_multiplier == pytest.approx(0.75)
        assert d.mode == "reduced"
        assert any("exec_slippage" in r for r in d.reasons)

    def test_high_fill_delay_reduction(self):
        """T21: fill_delay > threshold → ×0.80, reduced."""
        eng = _engine()
        d = eng.evaluate(_inputs(avg_fill_delay_ms=3000.0))
        assert d.risk_multiplier == pytest.approx(0.80)
        assert d.mode == "reduced"
        assert any("exec_fill_delay" in r for r in d.reasons)

    def test_kills_block(self):
        """T22: recent_kills >= max → blocked."""
        eng = _engine()
        d = eng.evaluate(_inputs(recent_kills=3))
        assert d.mode == "blocked"
        assert d.risk_multiplier == 0.0
        assert any("exec_kills" in r for r in d.reasons)

    def test_kills_no_block_if_disabled(self):
        """T23: block_on_execution_degrade=False → defensive instead of block."""
        eng = _engine(block_on_execution_degrade=False)
        d = eng.evaluate(_inputs(recent_kills=3))
        assert d.mode == "defensive"
        assert d.risk_multiplier > 0.0

    def test_combined_slippage_and_delay(self):
        """T24: Both slippage + delay degraded → multiplicative."""
        eng = _engine()
        d = eng.evaluate(_inputs(avg_slippage_bps=50, avg_fill_delay_ms=3000))
        expected = 0.75 * 0.80
        assert d.risk_multiplier == pytest.approx(expected, abs=0.01)


# ═══════════════════════════════════════════════════════════
# A6. Regime factor
# ═══════════════════════════════════════════════════════════

class TestRegimeFactor:
    """Regime × strategy compatibility."""

    def test_high_vol_regime(self):
        """T25: HIGH_VOL regime → ×0.85, reduced."""
        eng = _engine()
        d = eng.evaluate(_inputs(regime="HIGH_VOL"))
        assert d.risk_multiplier == pytest.approx(0.85)
        assert "regime_high_vol" in d.reasons

    def test_low_vol_regime(self):
        """T26: LOW_VOL regime → ×0.90."""
        eng = _engine()
        d = eng.evaluate(_inputs(regime="LOW_VOL"))
        assert d.risk_multiplier == pytest.approx(0.90)
        assert "regime_low_vol" in d.reasons

    def test_range_vs_breakout(self):
        """T27: RANGE + breakout → ×0.80, reduced."""
        eng = _engine()
        d = eng.evaluate(_inputs(regime="RANGE", strategy_type="breakout"))
        assert d.risk_multiplier == pytest.approx(0.80)
        assert "regime_range_vs_breakout" in d.reasons

    def test_trend_vs_meanrev(self):
        """T28: TREND + mean_reversion → ×0.80, reduced."""
        eng = _engine()
        d = eng.evaluate(_inputs(regime="TREND", strategy_type="mean_reversion"))
        assert d.risk_multiplier == pytest.approx(0.80)
        assert "regime_trend_vs_meanrev" in d.reasons

    def test_compatible_regime_no_reduction(self):
        """T29: TREND + breakout → no regime adjustment."""
        eng = _engine()
        d = eng.evaluate(_inputs(regime="TREND", strategy_type="breakout"))
        assert d.mode == "normal"
        assert d.risk_multiplier == 1.0

    def test_regime_reductions_disabled(self):
        """T30: Disable all regime reductions → no adjustment."""
        eng = _engine(
            reduce_in_low_vol=False,
            reduce_in_range_for_breakout=False,
            reduce_in_trend_for_meanrev=False,
        )
        d = eng.evaluate(_inputs(regime="LOW_VOL"))
        # Only HIGH_VOL is always-on
        assert d.risk_multiplier == 1.0


# ═══════════════════════════════════════════════════════════
# A7. Factor combinations
# ═══════════════════════════════════════════════════════════

class TestFactorCombinations:
    """Multiple factors active simultaneously."""

    def test_vol_plus_drawdown(self):
        """T31: Reduced vol + reduced dd → multiplicative."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=1.6, daily_dd_pct=1.8))
        expected = 0.75 * 0.70
        assert d.risk_multiplier == pytest.approx(expected, abs=0.01)

    def test_multiple_reduced_escalates_to_defensive(self):
        """T32: Multiple reductions below defensive_mult → defensive."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            atr_ratio=1.6,      # ×0.75 → reduced
            daily_dd_pct=1.8,   # ×0.70 → reduced
        ))
        # 0.75 × 0.70 = 0.525 → above defensive_mult (0.50)
        assert d.mode == "reduced"

        # Stronger combination → below defensive_mult threshold
        d2 = eng.evaluate(_inputs(
            atr_ratio=1.6,     # ×0.75
            daily_dd_pct=1.8,  # ×0.70
            avg_slippage_bps=50,  # ×0.75
        ))
        # 0.75 × 0.70 × 0.75 = 0.39375 → clamped to min_mult=0.25? No,
        # 0.39 < 0.50 (defensive_mult) → defensive mode
        assert d2.mode == "defensive"

    def test_any_block_overrides_all(self):
        """T33: One blocked factor → entire decision blocked."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            atr_ratio=3.0,     # blocked
            daily_dd_pct=0.0,  # nominal
        ))
        assert d.mode == "blocked"
        assert d.risk_multiplier == 0.0

    def test_min_mult_floor(self):
        """T34: Score never goes below min_mult (unless blocked)."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            atr_ratio=2.2,         # ×0.50 (defensive)
            daily_dd_pct=2.8,      # ×0.50 (defensive)
            avg_slippage_bps=50,   # ×0.75
        ))
        # Raw = 0.50 × 0.50 × 0.75 = 0.1875 → clamped to 0.25
        assert d.risk_multiplier == pytest.approx(0.25)
        assert d.mode == "defensive"


# ═══════════════════════════════════════════════════════════
# B1. RiskDecision serialization
# ═══════════════════════════════════════════════════════════

class TestRiskDecisionSerialization:
    """to_dict() output correctness."""

    def test_to_dict_keys(self):
        """T35: to_dict() has expected keys."""
        d = RiskDecision()
        out = d.to_dict()
        expected_keys = {
            "risk_multiplier", "effective_risk_pct", "effective_leverage",
            "size_multiplier", "mode", "reasons", "inputs",
        }
        assert set(out.keys()) == expected_keys

    def test_to_dict_values_rounded(self):
        """T36: Values are properly rounded."""
        d = RiskDecision(
            risk_multiplier=0.123456789,
            effective_risk_pct=0.0012345678,
            size_multiplier=0.987654321,
        )
        out = d.to_dict()
        assert out["risk_multiplier"] == 0.1235
        assert out["effective_risk_pct"] == 0.001235
        assert out["size_multiplier"] == 0.9877


# ═══════════════════════════════════════════════════════════
# B2. Engine disabled (passthrough)
# ═══════════════════════════════════════════════════════════

class TestDisabledEngine:
    """Engine disabled → neutral pass-through."""

    def test_disabled_returns_neutral(self):
        """T37: Disabled engine → mult=1.0, mode=normal."""
        eng = _engine(enabled=False)
        d = eng.evaluate(_inputs(atr_ratio=5.0, daily_dd_pct=10.0))
        assert d.mode == "normal"
        assert d.risk_multiplier == 1.0
        assert "adaptive_risk_disabled" in d.reasons

    def test_disabled_preserves_base_values(self):
        """T38: Disabled → base risk/leverage unchanged."""
        eng = _engine(enabled=False)
        d = eng.evaluate(_inputs(base_risk_pct=0.03, base_leverage=10))
        assert d.effective_risk_pct == 0.03
        assert d.effective_leverage == 10


# ═══════════════════════════════════════════════════════════
# B3. build_risk_inputs
# ═══════════════════════════════════════════════════════════

class TestBuildRiskInputs:
    """Signal dict → RiskInputs conversion."""

    def test_basic_signal_dict(self):
        """T39: ATR info extracted from signal_dict."""
        signal = {
            "atr_info": {"current_atr": 200, "avg_atr": 100},
            "regime": "TREND",
            "setup": "breakout",
        }
        ri = build_risk_inputs(signal, base_risk_pct=0.01, base_leverage=5)
        assert ri.atr_ratio == pytest.approx(2.0)
        assert ri.regime == "TREND"
        assert ri.strategy_type == "breakout"
        assert ri.base_risk_pct == 0.01
        assert ri.base_leverage == 5

    def test_missing_atr_info(self):
        """T40: No atr_info → atr_ratio=0.0 (no ATR data, current=0)."""
        signal = {}
        ri = build_risk_inputs(signal)
        # No ATR data → current_atr=0, avg_atr fallback=1 → ratio=0/1=0
        assert ri.atr_ratio == pytest.approx(0.0)

    def test_zero_avg_atr_safe(self):
        """T41: avg_atr=0 → atr_ratio=1.0 (no div by zero)."""
        signal = {"atr_info": {"current_atr": 100, "avg_atr": 0}}
        ri = build_risk_inputs(signal)
        assert ri.atr_ratio == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════
# B4. Governor recording
# ═══════════════════════════════════════════════════════════

class TestGovernorRecording:
    """Governor records adaptive risk decisions."""

    def _fresh_governor(self):
        from smc_futures.observability.performance_governor import PerformanceGovernor
        return PerformanceGovernor()

    def test_record_adaptive_decision(self):
        """T42: record_adaptive_risk_decision populates snapshot."""
        gov = self._fresh_governor()
        gov.record_adaptive_risk_decision(
            mode="reduced",
            risk_multiplier=0.75,
            effective_leverage=3,
            effective_risk_pct=0.0075,
            reasons=["vol_reduced(atr_ratio=1.60)"],
            symbol="BTCUSDT",
            strategy="DefaultSMC",
        )
        snap = gov.snapshot()
        ar = snap["adaptive_risk"]
        assert ar["total_decisions"] == 1
        assert ar["by_mode"]["reduced"] == 1

    def test_record_blocked_decision(self):
        """T43: Blocked decisions tracked separately."""
        gov = self._fresh_governor()
        gov.record_adaptive_risk_decision(
            mode="blocked",
            risk_multiplier=0.0,
            effective_leverage=0,
            effective_risk_pct=0.0,
            reasons=["vol_extreme(atr_ratio=3.50)"],
            symbol="BTCUSDT",
            strategy="DefaultSMC",
        )
        snap = gov.snapshot()
        ar = snap["adaptive_risk"]
        assert ar["by_mode"]["blocked"] == 1

    def test_multiplier_stats(self):
        """T44: Multiplier stats computed correctly."""
        gov = self._fresh_governor()
        for mult in [0.50, 0.75, 1.00]:
            gov.record_adaptive_risk_decision(
                mode="reduced",
                risk_multiplier=mult,
                effective_leverage=3,
                effective_risk_pct=0.01 * mult,
                reasons=["test"],
                symbol="BTCUSDT",
                strategy="DefaultSMC",
            )
        snap = gov.snapshot()
        ar = snap["adaptive_risk"]
        mult_stats = ar["multiplier"]
        assert mult_stats["count"] == 3
        assert mult_stats["mean"] == pytest.approx(0.75, abs=0.01)
        assert mult_stats["max"] == pytest.approx(1.00)


# ═══════════════════════════════════════════════════════════
# B5. Prometheus metrics wiring
# ═══════════════════════════════════════════════════════════

class TestPrometheusMetrics:
    """P7 metrics exist and are updated."""

    def test_metrics_defined(self):
        """T45: All 5 P7 metrics are importable."""
        from smc_futures.observability.metrics import (
            ADAPTIVE_RISK_MULTIPLIER,
            ADAPTIVE_RISK_MODE_TOTAL,
            ADAPTIVE_RISK_BLOCK_TOTAL,
            ADAPTIVE_LEVERAGE,
            ADAPTIVE_EFFECTIVE_RISK_PCT,
        )
        # Just verify they exist (no-op if prometheus not running)
        assert ADAPTIVE_RISK_MULTIPLIER is not None
        assert ADAPTIVE_RISK_MODE_TOTAL is not None
        assert ADAPTIVE_RISK_BLOCK_TOTAL is not None
        assert ADAPTIVE_LEVERAGE is not None
        assert ADAPTIVE_EFFECTIVE_RISK_PCT is not None


# ═══════════════════════════════════════════════════════════
# C1. Configuration
# ═══════════════════════════════════════════════════════════

class TestConfiguration:
    """Config loading and immutability."""

    def test_config_frozen(self):
        """T46: AdaptiveRiskConfig is frozen (immutable)."""
        cfg = _default_config()
        with pytest.raises(FrozenInstanceError):
            cfg.enabled = False  # type: ignore

    def test_max_mult_never_above_1(self):
        """T47: Engine clamps multiplier ≤ max_mult=1.0."""
        eng = _engine(max_mult=1.0)
        d = eng.evaluate(_inputs(atr_ratio=0.5))  # Very low vol
        assert d.risk_multiplier <= 1.0

    def test_config_from_env(self):
        """T48: Config reads from ENV variables."""
        with patch.dict("os.environ", {
            "ADAPTIVE_RISK_ENABLED": "false",
            "ADAPTIVE_RISK_MIN_MULT": "0.10",
            "ADAPTIVE_RISK_MAX_MULT": "0.95",
        }):
            cfg = AdaptiveRiskConfig.from_env()
            assert cfg.enabled is False
            assert cfg.min_mult == pytest.approx(0.10)
            assert cfg.max_mult == pytest.approx(0.95)


# ═══════════════════════════════════════════════════════════
# C2. Utility functions
# ═══════════════════════════════════════════════════════════

class TestUtilities:
    """Helper function correctness."""

    def test_worst_mode_escalation(self):
        """T49: _worst_mode returns most restrictive."""
        assert _worst_mode("normal", "reduced") == "reduced"
        assert _worst_mode("reduced", "defensive") == "defensive"
        assert _worst_mode("defensive", "blocked") == "blocked"
        assert _worst_mode("blocked", "normal") == "blocked"

    def test_worst_mode_same(self):
        """T50: _worst_mode with identical modes."""
        assert _worst_mode("normal", "normal") == "normal"
        assert _worst_mode("blocked", "blocked") == "blocked"


# ═══════════════════════════════════════════════════════════
# C3. Edge cases and effective values
# ═══════════════════════════════════════════════════════════

class TestEdgeCases:
    """Boundary conditions and derived values."""

    def test_blocked_leverage_is_zero(self):
        """T51: Blocked mode → leverage=0."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=3.0))
        assert d.effective_leverage == 0

    def test_effective_risk_reduced(self):
        """T52: Effective risk = base × multiplier."""
        eng = _engine()
        d = eng.evaluate(_inputs(base_risk_pct=0.02, atr_ratio=1.6))
        assert d.effective_risk_pct == pytest.approx(0.02 * 0.75, abs=0.001)

    def test_decision_inputs_populated(self):
        """T53: Decision includes input snapshot for audit."""
        eng = _engine()
        d = eng.evaluate(_inputs(atr_ratio=1.6, daily_dd_pct=1.0, regime="TREND"))
        assert d.inputs["atr_ratio"] == pytest.approx(1.6)
        assert d.inputs["daily_dd_pct"] == pytest.approx(1.0)
        assert d.inputs["regime"] == "TREND"

    def test_wr_above_min_no_adjustment(self):
        """T54: WR above threshold → no WR penalty."""
        eng = _engine()
        d = eng.evaluate(_inputs(
            recent_wr=0.55, recent_pf=1.5,
            recent_trades_count=20,
        ))
        assert d.risk_multiplier == 1.0
        assert d.mode == "normal"
