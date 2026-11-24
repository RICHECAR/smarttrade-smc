"""
Learning Pace Controller
════════════════════════

Prevents overreaction to recent data by dynamically slowing
or freezing adaptation under adverse conditions:

  • Unstable regime           → SLOW
  • Weak evidence             → SLOW
  • Noisy execution           → SLOW
  • High portfolio stress     → FROZEN
  • Multiple brakes active    → FROZEN

Outputs an adaptation_multiplier (0 = frozen, 0.5 = slow, 1 = normal)
that downstream engines use to attenuate adaptation magnitude.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from backend.services.trading.governance_models import (
    LearningBrake,
    LearningPace,
    PaceAssessment,
)

logger = logging.getLogger(__name__)


class LearningPaceController:
    """
    Evaluates system conditions and outputs a pace / multiplier
    that governs how aggressively the system adapts.
    """

    # ── Thresholds ───────────────────────────────────────────────
    REGIME_STABILITY_SLOW: float = 0.40    # below → slow
    REGIME_STABILITY_FREEZE: float = 0.20  # below → freeze
    EVIDENCE_STRENGTH_SLOW: float = 0.50
    EVIDENCE_STRENGTH_FREEZE: float = 0.25
    EXECUTION_NOISE_SLOW: float = 0.50
    EXECUTION_NOISE_FREEZE: float = 0.80
    PORTFOLIO_STRESS_SLOW: float = 0.50
    PORTFOLIO_STRESS_FREEZE: float = 0.75
    # Multiplier values
    MULTIPLIER_NORMAL: float = 1.0
    MULTIPLIER_SLOW: float = 0.30
    MULTIPLIER_FROZEN: float = 0.0
    # How many individual "slow" brakes trigger a freeze
    MULTI_BRAKE_FREEZE_COUNT: int = 3

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            if hasattr(self, key.upper()):
                setattr(self, key.upper(), value)
        self._last_assessment: Optional[PaceAssessment] = None

    # ── Public API ───────────────────────────────────────────────

    def assess(
        self,
        *,
        regime_stability: float = 1.0,
        evidence_strength: float = 1.0,
        execution_noise: float = 0.0,
        portfolio_stress: float = 0.0,
    ) -> PaceAssessment:
        """
        Evaluate conditions and return a pace assessment.

        All inputs are 0-1 normalised:
          regime_stability:  1 = very stable, 0 = chaotic
          evidence_strength: 1 = strong data, 0 = no data
          execution_noise:   0 = clean fills, 1 = extreme noise
          portfolio_stress:  0 = calm, 1 = extreme stress
        """

        active_brakes: List[str] = []
        freeze_brakes: List[str] = []

        # Regime stability
        if regime_stability < self.REGIME_STABILITY_FREEZE:
            freeze_brakes.append("unstable_regime_severe")
        elif regime_stability < self.REGIME_STABILITY_SLOW:
            active_brakes.append("unstable_regime")

        # Evidence strength
        if evidence_strength < self.EVIDENCE_STRENGTH_FREEZE:
            freeze_brakes.append("weak_evidence_severe")
        elif evidence_strength < self.EVIDENCE_STRENGTH_SLOW:
            active_brakes.append("weak_evidence")

        # Execution noise
        if execution_noise > self.EXECUTION_NOISE_FREEZE:
            freeze_brakes.append("noisy_execution_severe")
        elif execution_noise > self.EXECUTION_NOISE_SLOW:
            active_brakes.append("noisy_execution")

        # Portfolio stress
        if portfolio_stress > self.PORTFOLIO_STRESS_FREEZE:
            freeze_brakes.append("high_portfolio_stress_severe")
        elif portfolio_stress > self.PORTFOLIO_STRESS_SLOW:
            active_brakes.append("high_portfolio_stress")

        # Determine pace
        all_brakes = active_brakes + freeze_brakes

        if freeze_brakes or len(active_brakes) >= self.MULTI_BRAKE_FREEZE_COUNT:
            pace = LearningPace.FROZEN
            multiplier = self.MULTIPLIER_FROZEN
            if len(all_brakes) > 1:
                brake = LearningBrake.MULTIPLE
            elif freeze_brakes:
                brake = self._classify_brake(freeze_brakes[0])
            else:
                brake = LearningBrake.MULTIPLE
        elif active_brakes:
            pace = LearningPace.SLOW
            multiplier = self.MULTIPLIER_SLOW
            brake = self._classify_brake(active_brakes[0]) if len(active_brakes) == 1 else LearningBrake.MULTIPLE
        else:
            pace = LearningPace.NORMAL
            multiplier = self.MULTIPLIER_NORMAL
            brake = LearningBrake.NONE

        reason_parts = []
        if all_brakes:
            reason_parts.append(f"brakes=[{', '.join(all_brakes)}]")
        reason = "; ".join(reason_parts) if reason_parts else "all_clear"

        assessment = PaceAssessment(
            pace=pace,
            brake=brake,
            adaptation_multiplier=multiplier,
            regime_stability=regime_stability,
            evidence_strength=evidence_strength,
            execution_noise=execution_noise,
            portfolio_stress=portfolio_stress,
            active_brakes=all_brakes,
            reason=reason,
        )
        self._last_assessment = assessment

        logger.info(
            "Learning pace: %s (multiplier=%.2f, brakes=%s)",
            pace.value, multiplier, all_brakes,
        )
        return assessment

    def get_last_assessment(self) -> Optional[PaceAssessment]:
        return self._last_assessment

    def get_multiplier(self) -> float:
        """Shortcut: return current adaptation multiplier."""
        if self._last_assessment is None:
            return self.MULTIPLIER_NORMAL
        return self._last_assessment.adaptation_multiplier

    def is_frozen(self) -> bool:
        if self._last_assessment is None:
            return False
        return self._last_assessment.pace == LearningPace.FROZEN

    # ── Private helpers ──────────────────────────────────────────

    @staticmethod
    def _classify_brake(brake_name: str) -> LearningBrake:
        mapping = {
            "unstable_regime": LearningBrake.UNSTABLE_REGIME,
            "unstable_regime_severe": LearningBrake.UNSTABLE_REGIME,
            "weak_evidence": LearningBrake.WEAK_EVIDENCE,
            "weak_evidence_severe": LearningBrake.WEAK_EVIDENCE,
            "noisy_execution": LearningBrake.NOISY_EXECUTION,
            "noisy_execution_severe": LearningBrake.NOISY_EXECUTION,
            "high_portfolio_stress": LearningBrake.HIGH_PORTFOLIO_STRESS,
            "high_portfolio_stress_severe": LearningBrake.HIGH_PORTFOLIO_STRESS,
        }
        return mapping.get(brake_name, LearningBrake.NONE)
