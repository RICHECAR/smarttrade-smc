"""
Setup Survival Scorer — Composite Scoring for Setup Viability
═════════════════════════════════════════════════════════════

Scores each setup from 0.0 → 1.0 using a weighted composite of:

  1. Win-rate component      (w=0.25)
  2. Profit-factor component (w=0.25)
  3. Expectancy component    (w=0.20)
  4. Drawdown-contribution   (w=0.15, inverted — lower is better)
  5. Regime-fit bonus        (w=0.15)

Output is a SurvivalVerdict with:
  - score: 0.0 – 1.0
  - recommendation: BOOST / KEEP / REDUCE / BLOCK
  - components: dict of per-component score

Thresholds (env-configurable):
  SURVIVAL_BOOST_THRESHOLD=0.75
  SURVIVAL_KEEP_THRESHOLD=0.50
  SURVIVAL_REDUCE_THRESHOLD=0.30
  (below 0.30 → BLOCK)
"""

import logging
import os
from typing import Dict, Optional

from backend.services.trading.darwinian_models import (
    SetupProfile,
    SetupState,
    SurvivalRecommendation,
    SurvivalVerdict,
)

logger = logging.getLogger("smarttrade.darwinian.survival")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class SetupSurvivalScorer:
    """
    Pure scorer — takes a SetupProfile + optional regime info, returns a SurvivalVerdict.
    """

    # Component weights (sum = 1.0)
    W_WIN_RATE = 0.25
    W_PROFIT_FACTOR = 0.25
    W_EXPECTANCY = 0.20
    W_DRAWDOWN = 0.15
    W_REGIME_FIT = 0.15

    def __init__(self) -> None:
        self._boost_thr = _envf("SURVIVAL_BOOST_THRESHOLD", 0.75)
        self._keep_thr = _envf("SURVIVAL_KEEP_THRESHOLD", 0.50)
        self._reduce_thr = _envf("SURVIVAL_REDUCE_THRESHOLD", 0.30)

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def score(
        self,
        profile: SetupProfile,
        regime_fit: float = 0.5,
    ) -> SurvivalVerdict:
        """
        Compute survival verdict for a setup.

        Args:
            profile: Current SetupProfile with rolling stats.
            regime_fit: 0.0–1.0 compatibility with current regime
                        (supplied by RegimeSetupMatrix).
        """
        components: Dict[str, float] = {}

        # 1. Win-rate: 0→0, 0.5→0.5, 1.0→1.0 (linear)
        components["win_rate"] = max(0.0, min(1.0, profile.win_rate))

        # 2. Profit factor: 0→0, 1.0→0.4, 2.0→0.8, ≥2.5→1.0 (clamped linear)
        pf_norm = max(0.0, min(1.0, profile.profit_factor / 2.5))
        components["profit_factor"] = pf_norm

        # 3. Expectancy: normalised around typical range [-100, +100]
        # Sigmoid-like mapping: -100→~0.05, 0→0.5, +100→~0.95
        exp_norm = 1.0 / (1.0 + _safe_exp(-profile.expectancy / 30.0))
        components["expectancy"] = exp_norm

        # 4. Drawdown contribution (inverted): 0→1.0, 1.0→0.0
        components["drawdown"] = max(0.0, 1.0 - profile.drawdown_contribution)

        # 5. Regime fit (passthrough; caller-supplied or default 0.5)
        components["regime_fit"] = max(0.0, min(1.0, regime_fit))

        # Weighted composite
        score = (
            self.W_WIN_RATE * components["win_rate"]
            + self.W_PROFIT_FACTOR * components["profit_factor"]
            + self.W_EXPECTANCY * components["expectancy"]
            + self.W_DRAWDOWN * components["drawdown"]
            + self.W_REGIME_FIT * components["regime_fit"]
        )
        score = round(max(0.0, min(1.0, score)), 4)

        # Override: blocked setups get hard floor
        if profile.state == SetupState.BLOCKED:
            score = min(score, 0.10)

        # Recommendation
        recommendation = self._classify(score, profile)

        verdict = SurvivalVerdict(
            score=score,
            recommendation=recommendation,
            components=components,
        )
        logger.debug(
            "[SURVIVAL] %s → %.3f (%s) %s",
            str(profile.key), score, recommendation.value, components,
        )
        return verdict

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _classify(self, score: float, profile: SetupProfile) -> SurvivalRecommendation:
        """Map score to recommendation, respecting lifecycle state."""
        if profile.state == SetupState.BLOCKED:
            return SurvivalRecommendation.BLOCK

        if score >= self._boost_thr:
            return SurvivalRecommendation.BOOST
        elif score >= self._keep_thr:
            return SurvivalRecommendation.KEEP
        elif score >= self._reduce_thr:
            return SurvivalRecommendation.REDUCE
        else:
            return SurvivalRecommendation.BLOCK


def _safe_exp(x: float) -> float:
    """math.exp with overflow protection."""
    import math
    try:
        return math.exp(max(-500.0, min(500.0, x)))
    except OverflowError:
        return 1e200 if x > 0 else 0.0
