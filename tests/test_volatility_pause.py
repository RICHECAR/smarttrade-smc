"""
Tests for Phase 9 — Volatility Pause Logic

Covers:
  - Extreme ATR triggers pause
  - Elevated ATR + high fallback triggers pause
  - Hysteresis (pause stays until conditions normalize)
  - Normal conditions → no pause
  - Risk factor and freeze flags
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.reporting.volatility_pause import (
    evaluate_volatility_pause,
    VOL_NORMAL,
    VOL_ELEVATED,
    VOL_EXTREME,
    VOL_PAUSE_ATR_EXTREME,
    VOL_PAUSE_ATR_ELEVATED,
    VOL_PAUSE_FALLBACK_COMBO,
    VOL_PAUSE_RISK_FACTOR,
    VOL_RESUME_ATR_MAX,
    VOL_RESUME_FALLBACK_MAX,
)


class TestVolatilityPauseEntry:

    def test_extreme_atr_triggers_pause(self):
        result = evaluate_volatility_pause(atr_ratio=2.5)
        assert result["pause_active"] is True
        assert result["volatility_state"] == VOL_EXTREME
        assert result["freeze_adaptive"] is True
        assert result["risk_factor"] == VOL_PAUSE_RISK_FACTOR

    def test_elevated_atr_plus_fallback_triggers_pause(self):
        result = evaluate_volatility_pause(atr_ratio=1.6, fallback_rate=0.45)
        assert result["pause_active"] is True
        assert result["volatility_state"] == VOL_ELEVATED

    def test_elevated_atr_alone_no_pause(self):
        result = evaluate_volatility_pause(atr_ratio=1.6, fallback_rate=0.2)
        assert result["pause_active"] is False
        assert result["volatility_state"] == VOL_NORMAL

    def test_normal_conditions(self):
        result = evaluate_volatility_pause(atr_ratio=1.0, fallback_rate=0.1)
        assert result["pause_active"] is False
        assert result["volatility_state"] == VOL_NORMAL
        assert result["risk_factor"] == 1.0
        assert result["freeze_adaptive"] is False


class TestVolatilityHysteresis:

    def test_pause_maintained_when_still_elevated(self):
        """Once paused, stay paused until ATR < 1.3 AND fallback < 30%."""
        result = evaluate_volatility_pause(
            atr_ratio=1.35,
            fallback_rate=0.2,
            current_pause_active=True,
        )
        assert result["pause_active"] is True
        assert "hysteresis" in result["reason"].lower()

    def test_pause_resumes_when_normalized(self):
        """Resume when both ATR and fallback are below thresholds."""
        result = evaluate_volatility_pause(
            atr_ratio=1.1,
            fallback_rate=0.1,
            current_pause_active=True,
        )
        assert result["pause_active"] is False
        assert result["volatility_state"] == VOL_NORMAL

    def test_pause_maintained_when_fallback_high(self):
        result = evaluate_volatility_pause(
            atr_ratio=1.0,
            fallback_rate=0.35,
            current_pause_active=True,
        )
        assert result["pause_active"] is True


class TestVolatilityEdgeCases:

    def test_exact_extreme_threshold(self):
        """ATR ratio exactly at threshold → not triggered (need >)."""
        result = evaluate_volatility_pause(atr_ratio=VOL_PAUSE_ATR_EXTREME)
        assert result["pause_active"] is False

    def test_just_above_extreme(self):
        result = evaluate_volatility_pause(atr_ratio=VOL_PAUSE_ATR_EXTREME + 0.01)
        assert result["pause_active"] is True

    def test_risk_factor_value(self):
        result = evaluate_volatility_pause(atr_ratio=2.5)
        assert result["risk_factor"] == 0.70
