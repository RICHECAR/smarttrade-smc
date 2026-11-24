"""
Preemptive Throttle Engine
══════════════════════════

Reduces system aggression BEFORE damage compounds, based on
structural break evidence rather than only realized drawdown.

Throttle levels:
  NONE     — 100% capacity, no restrictions
  LIGHT    — 80% capacity, mild tightening
  MODERATE — 50% capacity, reduced exposure + size
  HEAVY    — 25% capacity, minimal new positions
  HALT     — 0% new positions, let existing ones play out

Inputs consumed:
  • BreakReport.composite_stress         (from StructuralBreakDetector)
  • TransitionReport.max_fragility       (from RegimeTransitionMonitor)
  • DoctrineVerdict.validity_score       (from DoctrineValidityEngine)
  • PropagationResult.propagation_score  (from ShockPropagationAnalyzer)

Asymmetric:
  • Fast tightening (1 cycle to go HEAVY/HALT)
  • Slow relaxation (need N consecutive cycles of low stress)

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.structural_break_models import (
    ThrottleLevel,
    ThrottleState,
)


_LEVEL_RANK = {
    ThrottleLevel.NONE: 0,
    ThrottleLevel.LIGHT: 1,
    ThrottleLevel.MODERATE: 2,
    ThrottleLevel.HEAVY: 3,
    ThrottleLevel.HALT: 4,
}

_LEVEL_PARAMS: dict[ThrottleLevel, tuple[float, float, int]] = {
    # (exposure_mult, size_mult, max_concurrent_override)
    ThrottleLevel.NONE:     (1.00, 1.00, 0),
    ThrottleLevel.LIGHT:    (0.80, 0.85, 0),
    ThrottleLevel.MODERATE: (0.50, 0.60, 5),
    ThrottleLevel.HEAVY:    (0.25, 0.35, 2),
    ThrottleLevel.HALT:     (0.00, 0.00, 0),
}


class PreemptiveThrottleEngine:
    """Reduce aggression based on structural break evidence."""

    RELAX_HOLD_CYCLES = 3   # consecutive low-stress cycles before relaxing

    def __init__(self) -> None:
        self._current_level = ThrottleLevel.NONE
        self._relax_counter = 0
        self._last_state: ThrottleState | None = None

    def evaluate(
        self,
        *,
        composite_stress: float = 0.0,       # 0–1 from BreakReport
        max_fragility: float = 0.0,           # 0–1 from TransitionReport
        doctrine_validity: float = 1.0,       # 0–1 from DoctrineVerdict
        propagation_score: float = 0.0,       # 0–1 from PropagationResult
        blocked_setups: list[str] | None = None,
    ) -> ThrottleState:
        """
        Compute throttle level from structural break evidence.

        Returns ThrottleState with multipliers and constraints.
        """
        # Composite danger score (0–1)
        danger = (
            0.30 * composite_stress
            + 0.25 * max_fragility
            + 0.25 * (1.0 - doctrine_validity)
            + 0.20 * propagation_score
        )
        danger = max(0.0, min(1.0, danger))

        # Map danger to target level
        target = self._danger_to_level(danger)

        # Asymmetric transitions
        target_rank = _LEVEL_RANK[target]
        current_rank = _LEVEL_RANK[self._current_level]

        if target_rank > current_rank:
            # Fast tightening — immediate
            self._current_level = target
            self._relax_counter = 0
        elif target_rank < current_rank:
            # Slow relaxation — require consecutive cycles
            self._relax_counter += 1
            if self._relax_counter >= self.RELAX_HOLD_CYCLES:
                # Step down one level only
                for level in ThrottleLevel:
                    if _LEVEL_RANK[level] == current_rank - 1:
                        self._current_level = level
                        break
                self._relax_counter = 0
        else:
            self._relax_counter = 0

        params = _LEVEL_PARAMS[self._current_level]
        reasons = []
        if composite_stress > 0.3:
            reasons.append(f"break_stress={composite_stress:.2f}")
        if max_fragility > 0.3:
            reasons.append(f"fragility={max_fragility:.2f}")
        if doctrine_validity < 0.5:
            reasons.append(f"doctrine={doctrine_validity:.2f}")
        if propagation_score > 0.3:
            reasons.append(f"propagation={propagation_score:.2f}")

        state = ThrottleState(
            level=self._current_level,
            exposure_multiplier=params[0],
            size_multiplier=params[1],
            max_concurrent_override=params[2],
            blocked_setups=blocked_setups or [],
            reason="; ".join(reasons) if reasons else "nominal",
        )
        self._last_state = state
        return state

    def get_level(self) -> ThrottleLevel:
        return self._current_level

    def get_last_state(self) -> ThrottleState | None:
        return self._last_state

    def force_level(self, level: ThrottleLevel) -> None:
        """Manual override for emergency."""
        self._current_level = level
        self._relax_counter = 0

    @staticmethod
    def _danger_to_level(danger: float) -> ThrottleLevel:
        if danger >= 0.80:
            return ThrottleLevel.HALT
        if danger >= 0.60:
            return ThrottleLevel.HEAVY
        if danger >= 0.40:
            return ThrottleLevel.MODERATE
        if danger >= 0.20:
            return ThrottleLevel.LIGHT
        return ThrottleLevel.NONE
