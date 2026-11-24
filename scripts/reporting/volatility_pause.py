"""
Volatility Pause Logic — Phase 9

Detects extreme volatility conditions and produces a pause state that:
  - Freezes adaptive weight promotion
  - Reduces risk_pct temporarily
  - Auto-releases when conditions normalize

Triggers:
  - ATR ratio > 2.0 (extreme expansion above 20-period average)
  - Combined: ATR ratio > 1.5 AND taker fallback rate > 40%

Design:
  - Pure functions, deterministic, fully testable
  - Called from DailyReporter.evaluate_regime_step()

Author: SMC Trading Bot Framework — Phase 9
"""

import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────
VOL_PAUSE_ATR_EXTREME = 2.0       # ATR ratio > 2.0 → immediate pause
VOL_PAUSE_ATR_ELEVATED = 1.5      # ATR ratio > 1.5 + fallback condition
VOL_PAUSE_FALLBACK_COMBO = 0.40   # fallback rate > 40% (combined trigger)
VOL_PAUSE_RISK_FACTOR = 0.70      # reduce risk_pct by 30% during pause
VOL_RESUME_ATR_MAX = 1.3          # ATR ratio must drop to < 1.3 to resume
VOL_RESUME_FALLBACK_MAX = 0.30    # fallback rate must drop < 30% to resume

# ── Volatility States ────────────────────────────────────────────────
VOL_NORMAL = "NORMAL"
VOL_ELEVATED = "ELEVATED"
VOL_EXTREME = "EXTREME"


def evaluate_volatility_pause(
    atr_ratio: float,
    fallback_rate: float = 0.0,
    current_pause_active: bool = False,
) -> Dict[str, Any]:
    """
    Evaluate whether a volatility pause should be active.

    Args:
        atr_ratio:            Current ATR / avg ATR (>1.0 = expanding)
        fallback_rate:        Taker fallback rate from execution quality (0.0-1.0)
        current_pause_active: Whether a pause is currently active (for hysteresis)

    Returns:
        Dict with keys:
            pause_active: bool
            volatility_state: str (NORMAL / ELEVATED / EXTREME)
            risk_factor: float (1.0 = no reduction, <1.0 = reduced)
            freeze_adaptive: bool (block all adaptive promotions)
            reason: str
    """
    # ── Extreme volatility: immediate pause ──
    if atr_ratio > VOL_PAUSE_ATR_EXTREME:
        logger.warning(
            f"[VOL_PAUSE] EXTREME: ATR_ratio={atr_ratio:.2f} > "
            f"{VOL_PAUSE_ATR_EXTREME} → pause active"
        )
        return {
            "pause_active": True,
            "volatility_state": VOL_EXTREME,
            "risk_factor": VOL_PAUSE_RISK_FACTOR,
            "freeze_adaptive": True,
            "reason": f"ATR_ratio={atr_ratio:.2f} > {VOL_PAUSE_ATR_EXTREME} (extreme)",
        }

    # ── Elevated + poor execution: combined trigger ──
    if atr_ratio > VOL_PAUSE_ATR_ELEVATED and fallback_rate > VOL_PAUSE_FALLBACK_COMBO:
        logger.warning(
            f"[VOL_PAUSE] ELEVATED: ATR_ratio={atr_ratio:.2f} + "
            f"fallback_rate={fallback_rate*100:.1f}% → pause active"
        )
        return {
            "pause_active": True,
            "volatility_state": VOL_ELEVATED,
            "risk_factor": VOL_PAUSE_RISK_FACTOR,
            "freeze_adaptive": True,
            "reason": (
                f"ATR_ratio={atr_ratio:.2f} > {VOL_PAUSE_ATR_ELEVATED} AND "
                f"fallback_rate={fallback_rate*100:.1f}% > {VOL_PAUSE_FALLBACK_COMBO*100:.0f}%"
            ),
        }

    # ── Hysteresis: if pause was active, require conditions to normalize ──
    if current_pause_active:
        if atr_ratio > VOL_RESUME_ATR_MAX or fallback_rate > VOL_RESUME_FALLBACK_MAX:
            return {
                "pause_active": True,
                "volatility_state": VOL_ELEVATED,
                "risk_factor": VOL_PAUSE_RISK_FACTOR,
                "freeze_adaptive": True,
                "reason": (
                    f"Pause maintained (hysteresis): "
                    f"ATR_ratio={atr_ratio:.2f}, fallback={fallback_rate*100:.1f}% "
                    f"— need ATR<{VOL_RESUME_ATR_MAX} AND fallback<{VOL_RESUME_FALLBACK_MAX*100:.0f}%"
                ),
            }
        else:
            logger.info(
                f"[VOL_PAUSE] RESUME: ATR_ratio={atr_ratio:.2f}, "
                f"fallback={fallback_rate*100:.1f}% — conditions normalized"
            )

    # ── Normal: no pause ──
    return {
        "pause_active": False,
        "volatility_state": VOL_NORMAL,
        "risk_factor": 1.0,
        "freeze_adaptive": False,
        "reason": f"ATR_ratio={atr_ratio:.2f} normal, fallback={fallback_rate*100:.1f}% OK",
    }
