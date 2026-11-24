"""
Risk Adaptive — Dynamic risk adjustment based on system state.
═══════════════════════════════════════════════════════════════════

Phase 12: The system self-regulates.

Rules:
  - High incident frequency → reduce risk (exposure, size, kill switch threshold)
  - Long stability → permit risk increase
  - Never exceed tier limits

Adjustments:
  - position_size_multiplier: 0.1 to 1.0 (1.0 = full profile)
  - exposure_multiplier: 0.1 to 1.0
  - kill_switch_sensitivity: 0.5 to 2.0 (higher = more sensitive)
"""

import logging
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger("smarttrade.risk_adaptive")


@dataclass
class RiskAdjustment:
    """Current risk adjustment factors."""
    position_size_multiplier: float = 1.0  # 0.1-1.0
    exposure_multiplier: float = 1.0       # 0.1-1.0
    kill_switch_sensitivity: float = 1.0   # 0.5-2.0 (higher = trigger sooner)
    reason: str = "nominal"
    last_update: float = 0.0


class RiskAdaptive:
    """
    Dynamic risk adjustment engine.

    Reads incident risk score and temporal stability to compute
    real-time risk multipliers.

    Usage:
        adaptive = RiskAdaptive()
        adj = adaptive.evaluate(risk_score=0.3, hours_since_incident=2.0)
        effective_size = base_size * adj.position_size_multiplier
    """

    # ── Thresholds ──
    SCORE_SAFE = 0.1       # Below this: full risk
    SCORE_CAUTION = 0.3    # Above this: start reducing
    SCORE_DANGER = 0.6     # Above this: aggressive reduction
    SCORE_CRITICAL = 0.8   # Above this: minimal risk

    STABILITY_BOOST_HOURS = 72.0  # 3 days of no incidents → can increase

    # ── Bounds ──
    MIN_MULTIPLIER = 0.1
    MAX_MULTIPLIER = 1.0
    MIN_SENSITIVITY = 0.5
    MAX_SENSITIVITY = 2.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._adjustment = RiskAdjustment(last_update=time.time())

    def evaluate(
        self,
        risk_score: float,
        hours_since_last_incident: float = 999.0,
        consecutive_stable_days: int = 0,
    ) -> RiskAdjustment:
        """
        Compute risk adjustment from system signals.

        Args:
            risk_score: 0.0 (safe) to 1.0 (dangerous) from IncidentMemory
            hours_since_last_incident: hours since any incident
            consecutive_stable_days: days of no critical incident

        Returns:
            RiskAdjustment with multipliers.
        """
        # ── Position size & exposure multiplier ──
        if risk_score >= self.SCORE_CRITICAL:
            mult = self.MIN_MULTIPLIER
            reason = "CRITICAL risk — minimal trading"
        elif risk_score >= self.SCORE_DANGER:
            # Linear interpolation: danger→critical maps to 0.3→0.1
            t = (risk_score - self.SCORE_DANGER) / (self.SCORE_CRITICAL - self.SCORE_DANGER)
            mult = 0.3 - (t * 0.2)
            reason = "DANGER — heavy risk reduction"
        elif risk_score >= self.SCORE_CAUTION:
            # Linear: caution→danger maps to 0.7→0.3
            t = (risk_score - self.SCORE_CAUTION) / (self.SCORE_DANGER - self.SCORE_CAUTION)
            mult = 0.7 - (t * 0.4)
            reason = "CAUTION — moderate risk reduction"
        else:
            mult = self.MAX_MULTIPLIER
            reason = "nominal"

        # ── Stability bonus (only if no recent incidents) ──
        if hours_since_last_incident >= self.STABILITY_BOOST_HOURS and risk_score < self.SCORE_CAUTION:
            # Stability doesn't increase above MAX, but ensures full allocation
            mult = self.MAX_MULTIPLIER
            reason = f"stable ({hours_since_last_incident:.0f}h no incident)"

        mult = max(self.MIN_MULTIPLIER, min(self.MAX_MULTIPLIER, mult))

        # ── Kill switch sensitivity ──
        if risk_score >= self.SCORE_DANGER:
            sensitivity = self.MAX_SENSITIVITY  # Very sensitive
        elif risk_score >= self.SCORE_CAUTION:
            sensitivity = 1.5
        else:
            sensitivity = 1.0

        # Lower sensitivity when very stable
        if consecutive_stable_days >= 7 and risk_score < self.SCORE_SAFE:
            sensitivity = self.MIN_SENSITIVITY

        sensitivity = max(self.MIN_SENSITIVITY, min(self.MAX_SENSITIVITY, sensitivity))

        adj = RiskAdjustment(
            position_size_multiplier=round(mult, 3),
            exposure_multiplier=round(mult, 3),
            kill_switch_sensitivity=round(sensitivity, 3),
            reason=reason,
            last_update=time.time(),
        )

        with self._lock:
            old = self._adjustment
            self._adjustment = adj

        if old.reason != adj.reason:
            logger.warning(
                "[RISK_ADAPTIVE] Adjustment changed: %s → %s (size=%.1f%%, sensitivity=%.1f)",
                old.reason, adj.reason, mult * 100, sensitivity,
            )

        return adj

    @property
    def current(self) -> RiskAdjustment:
        with self._lock:
            return RiskAdjustment(**asdict(self._adjustment))

    def to_dict(self) -> dict:
        adj = self.current
        return {
            "position_size_multiplier": adj.position_size_multiplier,
            "exposure_multiplier": adj.exposure_multiplier,
            "kill_switch_sensitivity": adj.kill_switch_sensitivity,
            "reason": adj.reason,
            "last_update": adj.last_update,
        }


# ─── Singleton ────────────────────────────────────────────────

_adaptive: Optional[RiskAdaptive] = None
_singleton_lock = threading.Lock()


def get_risk_adaptive() -> RiskAdaptive:
    global _adaptive
    if _adaptive is None:
        with _singleton_lock:
            if _adaptive is None:
                _adaptive = RiskAdaptive()
    return _adaptive


def reset_risk_adaptive() -> None:
    """Reset singleton (tests only)."""
    global _adaptive
    _adaptive = None
