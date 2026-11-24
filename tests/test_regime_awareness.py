"""
Tests for Phase 9 — Regime Awareness Orchestrator

Covers:
  - Combined evaluation with all 3 sub-systems
  - Priority: recovery > vol_pause > regime
  - Regime-only mode (no pause, no recovery)
  - Volatility pause override of regime caps
  - Recovery override of everything
  - Integration with adaptive weight and capital scaling
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reporting.regime_awareness import (
    evaluate_regime_awareness,
)
from scripts.reporting.regime_detector import REGIME_TREND, REGIME_RANGE, REGIME_CHOP
from scripts.reporting.volatility_pause import VOL_NORMAL, VOL_EXTREME
from scripts.reporting.drawdown_recovery import RECOVERY_PROGRESSIVE_STEPS


class TestRegimeOnly:
    """Test regime classification without pause or recovery."""

    def test_trend_regime_full_freedom(self):
        result = evaluate_regime_awareness(adx=30, atr_ratio=1.0, bos_confirm_rate=0.6)
        assert result["regime"] == REGIME_TREND
        assert result["adaptive_weight_cap"] == 0.5
        assert result["scaling_cap_factor"] == 1.0
        assert result["freeze_adaptive"] is False
        assert result["block_scale_up"] is False

    def test_range_regime_moderate_caps(self):
        result = evaluate_regime_awareness(adx=18, atr_ratio=1.0, bos_confirm_rate=0.5)
        assert result["regime"] == REGIME_RANGE
        assert result["adaptive_weight_cap"] == 0.3
        assert result["scaling_cap_factor"] == 0.8

    def test_chop_regime_aggressive_caps(self):
        result = evaluate_regime_awareness(adx=10, atr_ratio=1.0, bos_confirm_rate=0.2)
        assert result["regime"] == REGIME_CHOP
        assert result["adaptive_weight_cap"] == 0.1
        assert result["scaling_cap_factor"] == 0.5
        assert result["risk_reduction_factor"] == 0.7


class TestVolatilityPauseOverride:
    """Volatility pause should override regime caps."""

    def test_vol_pause_freezes_adaptive(self):
        """Even in TREND, extreme ATR → freeze adaptive."""
        result = evaluate_regime_awareness(
            adx=30,
            atr_ratio=2.5,  # extreme
            bos_confirm_rate=0.6,
        )
        assert result["freeze_adaptive"] is True
        assert result["vol_pause_active"] is True
        assert "VOL_PAUSE" in result["effective_regime"]

    def test_vol_pause_reduces_risk(self):
        result = evaluate_regime_awareness(
            adx=30,
            atr_ratio=2.5,
            bos_confirm_rate=0.6,
        )
        # Regime TREND risk_factor=1.0 * vol_pause=0.7 = 0.7
        assert result["risk_reduction_factor"] < 1.0

    def test_vol_pause_caps_scaling(self):
        result = evaluate_regime_awareness(
            adx=30,
            atr_ratio=2.5,
            bos_confirm_rate=0.6,
        )
        assert result["scaling_cap_factor"] <= 0.5


class TestRecoveryOverride:
    """Recovery mode overrides everything (highest priority)."""

    def test_recovery_overrides_trend(self):
        """Even in TREND, DD > 5% → recovery mode governs."""
        result = evaluate_regime_awareness(
            adx=30,
            atr_ratio=1.0,
            bos_confirm_rate=0.6,
            current_dd_pct=6.0,
        )
        assert result["recovery_active"] is True
        assert result["effective_regime"] == "RECOVERY"
        assert result["freeze_adaptive"] is True
        assert result["block_scale_up"] is True
        assert result["adaptive_weight_cap"] == 0.0

    def test_recovery_risk_factor_compounds(self):
        """Recovery risk_factor compounds with regime factor."""
        result = evaluate_regime_awareness(
            adx=10,
            atr_ratio=1.0,
            bos_confirm_rate=0.5,
            current_dd_pct=6.0,
        )
        # RANGE risk=0.85 * recovery step 0 risk=0.50 = 0.425
        assert result["risk_reduction_factor"] < 0.5

    def test_recovery_with_vol_pause_triple_effect(self):
        """Recovery + vol pause + chop regime → maximum restriction."""
        result = evaluate_regime_awareness(
            adx=10,
            atr_ratio=2.5,
            bos_confirm_rate=0.2,
            current_dd_pct=6.0,
        )
        assert result["recovery_active"] is True
        assert result["vol_pause_active"] is True
        assert result["regime"] == REGIME_CHOP
        assert result["effective_regime"] == "RECOVERY"
        # risk = chop(0.7) * vol(0.7) * recovery(0.5) = 0.245
        assert result["risk_reduction_factor"] < 0.3


class TestEffectiveRegimeLabeling:

    def test_pure_regime_label(self):
        result = evaluate_regime_awareness(adx=30, atr_ratio=1.0, bos_confirm_rate=0.6)
        assert result["effective_regime"] == REGIME_TREND

    def test_vol_pause_label(self):
        result = evaluate_regime_awareness(adx=30, atr_ratio=2.5, bos_confirm_rate=0.6)
        assert "VOL_PAUSE" in result["effective_regime"]

    def test_recovery_label(self):
        result = evaluate_regime_awareness(adx=30, atr_ratio=1.0, bos_confirm_rate=0.6, current_dd_pct=6.0)
        assert result["effective_regime"] == "RECOVERY"


class TestRegimeWarnings:

    def test_warnings_from_vol_pause(self):
        result = evaluate_regime_awareness(adx=30, atr_ratio=2.5, bos_confirm_rate=0.6)
        assert any("Volatility pause" in w for w in result["warnings"])

    def test_warnings_from_recovery(self):
        result = evaluate_regime_awareness(adx=30, atr_ratio=1.0, bos_confirm_rate=0.6, current_dd_pct=6.0)
        assert any("Recovery mode" in w for w in result["warnings"])

    def test_no_warnings_in_normal(self):
        result = evaluate_regime_awareness(adx=30, atr_ratio=1.0, bos_confirm_rate=0.6)
        assert len(result["warnings"]) == 0


class TestRegimeResultStructure:

    def test_all_keys_present(self):
        result = evaluate_regime_awareness(adx=20, atr_ratio=1.0, bos_confirm_rate=0.5)
        expected = [
            "regime", "regime_reason", "volatility_state", "vol_pause_active",
            "recovery_active", "recovery_step", "recovery_total_steps",
            "adaptive_weight_cap", "scaling_cap_factor", "risk_reduction_factor",
            "freeze_adaptive", "block_scale_up", "effective_regime", "warnings", "details",
        ]
        for k in expected:
            assert k in result, f"Missing key: {k}"

    def test_details_has_subsystems(self):
        result = evaluate_regime_awareness(adx=20, atr_ratio=1.0, bos_confirm_rate=0.5)
        details = result["details"]
        assert "regime" in details
        assert "volatility" in details
        assert "recovery" in details
