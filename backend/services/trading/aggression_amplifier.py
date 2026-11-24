"""
Aggression Amplifier
════════════════════

Dynamically increases trading aggressiveness ONLY when conditions align:

  1. Opportunity score ≥ threshold     (high asymmetry detected)
  2. Structural conditions favorable   (no breaks, low stress)
  3. Execution quality strong          (good fills, low latency)
  4. Portfolio stress low              (drawdown, correlation OK)

When ALL 4 conditions are met → full amplification.
When 3/4 → moderate amplification.
When 2/4 → minimal amplification.
When <2 → no amplification (baseline).

Adjusts:
  • Position size multiplier   (1.0 – 2.0x)
  • Scaling speed              (1.0 – 1.5x)
  • Take-profit delay factor   (1.0 – 2.0x longer hold)
  • Trailing stop looseness    (1.0 – 1.5x wider)

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.opportunity_dominance_models import (
    AggressionDecision,
)


class AggressionAmplifier:
    """Amplify aggression when true edge conditions emerge."""

    # Condition thresholds
    OPP_SCORE_THRESHOLD = 0.60
    STRUCTURAL_STRESS_MAX = 0.30    # composite stress must be below
    EXECUTION_QUALITY_MIN = 0.70    # fill quality must exceed
    PORTFOLIO_STRESS_MAX = 0.40     # portfolio stress must be below

    # Amplification profiles per conditions-met count
    _PROFILES = {
        4: {  # All conditions met → maximum aggression
            "size_mult": 1.80,
            "scaling_speed": 1.50,
            "tp_delay": 1.80,
            "trailing_loose": 1.40,
        },
        3: {  # 3 conditions → moderate aggression
            "size_mult": 1.40,
            "scaling_speed": 1.25,
            "tp_delay": 1.40,
            "trailing_loose": 1.20,
        },
        2: {  # 2 conditions → minimal amplification
            "size_mult": 1.15,
            "scaling_speed": 1.10,
            "tp_delay": 1.15,
            "trailing_loose": 1.10,
        },
    }

    _BASELINE = {
        "size_mult": 1.00,
        "scaling_speed": 1.00,
        "tp_delay": 1.00,
        "trailing_loose": 1.00,
    }

    def __init__(self) -> None:
        self._last_decision: AggressionDecision | None = None

    def evaluate(
        self,
        *,
        opportunity_score: float = 0.0,
        composite_stress: float = 0.0,
        execution_quality: float = 1.0,
        portfolio_stress: float = 0.0,
        expected_asymmetry: float = 1.0,
    ) -> AggressionDecision:
        """
        Determine aggression amplification level.

        Higher opportunity_score + lower stress = more aggression.
        """
        conditions = []
        details = []

        # 1. Opportunity score
        cond1 = opportunity_score >= self.OPP_SCORE_THRESHOLD
        conditions.append(cond1)
        details.append(f"opp={'OK' if cond1 else 'LOW'}({opportunity_score:.2f})")

        # 2. Structural conditions
        cond2 = composite_stress <= self.STRUCTURAL_STRESS_MAX
        conditions.append(cond2)
        details.append(f"struct={'OK' if cond2 else 'HIGH'}({composite_stress:.2f})")

        # 3. Execution quality
        cond3 = execution_quality >= self.EXECUTION_QUALITY_MIN
        conditions.append(cond3)
        details.append(f"exec={'OK' if cond3 else 'LOW'}({execution_quality:.2f})")

        # 4. Portfolio stress
        cond4 = portfolio_stress <= self.PORTFOLIO_STRESS_MAX
        conditions.append(cond4)
        details.append(f"port={'OK' if cond4 else 'HIGH'}({portfolio_stress:.2f})")

        met = sum(conditions)
        profile = self._PROFILES.get(met, self._BASELINE)

        # Scale amplification by asymmetry quality
        asym_bonus = min(1.0, max(0.0, (expected_asymmetry - 1.0) / 4.0))

        size_mult = profile["size_mult"] + asym_bonus * 0.20
        scaling_speed = profile["scaling_speed"] + asym_bonus * 0.10
        tp_delay = profile["tp_delay"] + asym_bonus * 0.20
        trailing_loose = profile["trailing_loose"] + asym_bonus * 0.10

        # Cap maximums
        size_mult = min(2.00, size_mult)
        scaling_speed = min(1.60, scaling_speed)
        tp_delay = min(2.00, tp_delay)
        trailing_loose = min(1.50, trailing_loose)

        # Overall aggression multiplier
        aggression = (size_mult + scaling_speed + tp_delay + trailing_loose) / 4.0
        # Risk-adjusted intensity: 0–1
        intensity = min(1.0, (aggression - 1.0) / 0.75)

        decision = AggressionDecision(
            aggression_multiplier=round(aggression, 4),
            size_multiplier=round(size_mult, 4),
            scaling_speed=round(scaling_speed, 4),
            tp_delay_factor=round(tp_delay, 4),
            trailing_looseness=round(trailing_loose, 4),
            risk_adjusted_intensity=round(max(0.0, intensity), 4),
            conditions_met=met,
            detail=" | ".join(details),
        )
        self._last_decision = decision
        return decision

    def get_last_decision(self) -> AggressionDecision | None:
        return self._last_decision
