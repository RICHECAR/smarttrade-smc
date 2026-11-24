"""
Evolution Validation Engine
═══════════════════════════

Validates every proposed adaptation before activation.

Checks:
  • Minimum evidence quality (sample size, CI bounds)
  • Robustness across multiple time windows
  • Execution-adjusted improvement (survives costs)
  • No hidden fragility increase
  • No unacceptable drawdown contribution

Output: APPROVE / HOLD / REJECT with confidence + rejection reasons.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Any, Dict, List, Optional

from backend.services.trading.governance_models import (
    DrawdownCheck,
    EvidenceQuality,
    ExecutionAdjustedCheck,
    FragilityCheck,
    RobustnessCheck,
    ValidationResult,
    ValidationVerdict,
)

logger = logging.getLogger(__name__)


class EvolutionValidationEngine:
    """
    Gate-keeper that validates proposed adaptations before they can be
    promoted to live trading.  Pure function-style: no side-effects,
    fully deterministic given inputs.
    """

    # ── Configurable thresholds ──────────────────────────────────
    MIN_SAMPLE_SIZE: int = 30
    MIN_WIN_RATE_CI_LOWER: float = 0.40
    MIN_EXPECTANCY_STABILITY: float = 0.50
    MIN_PF_STABILITY: float = 0.45
    MIN_WINDOWS_POSITIVE_RATIO: float = 0.60
    MIN_CONSISTENCY_SCORE: float = 0.50
    MAX_REGIME_DEPENDENCY: float = 0.70
    MIN_EXECUTION_ADJUSTED_IMPROVEMENT: float = 0.0
    MAX_EXECUTION_COST_RATIO: float = 0.80
    MAX_FRAGILITY_DELTA: float = 0.10
    MAX_DRAWDOWN_CONTRIBUTION: float = 0.15
    # Confidence weights
    W_EVIDENCE: float = 0.25
    W_ROBUSTNESS: float = 0.25
    W_EXECUTION: float = 0.20
    W_FRAGILITY: float = 0.15
    W_DRAWDOWN: float = 0.15

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            if hasattr(self, key.upper()):
                setattr(self, key.upper(), value)

    # ── Public API ───────────────────────────────────────────────

    def validate(
        self,
        adaptation_id: str,
        *,
        sample_size: int = 0,
        win_rate_ci_lower: float = 0.0,
        expectancy_stability: float = 0.0,
        pf_stability: float = 0.0,
        windows_tested: int = 0,
        windows_positive: int = 0,
        consistency_score: float = 0.0,
        regime_dependency: float = 0.0,
        worst_window_expectancy: float = 0.0,
        raw_improvement: float = 0.0,
        execution_adjusted_improvement: float = 0.0,
        fragility_before: float = 0.0,
        fragility_after: float = 0.0,
        max_dd_before: float = 0.0,
        max_dd_after: float = 0.0,
    ) -> ValidationResult:
        """Run all validation checks and return a verdict."""

        evidence = self._check_evidence(
            sample_size, win_rate_ci_lower, expectancy_stability, pf_stability
        )
        robustness = self._check_robustness(
            windows_tested, windows_positive, consistency_score,
            regime_dependency, worst_window_expectancy,
        )
        execution = self._check_execution(
            raw_improvement, execution_adjusted_improvement
        )
        fragility = self._check_fragility(fragility_before, fragility_after)
        drawdown = self._check_drawdown(max_dd_before, max_dd_after)

        rejection_reasons: List[str] = []
        if not evidence.is_sufficient:
            rejection_reasons.append("insufficient_evidence")
        if not robustness.is_robust:
            rejection_reasons.append("not_robust_across_windows")
        if not execution.survives_execution:
            rejection_reasons.append("improvement_eroded_by_execution")
        if not fragility.is_safe:
            rejection_reasons.append("fragility_increase_unacceptable")
        if not drawdown.is_acceptable:
            rejection_reasons.append("drawdown_contribution_too_high")

        # Composite confidence
        scores = [
            (self.W_EVIDENCE, 1.0 if evidence.is_sufficient else 0.0),
            (self.W_ROBUSTNESS, robustness.consistency_score),
            (self.W_EXECUTION, 1.0 if execution.survives_execution else 0.0),
            (self.W_FRAGILITY, 1.0 - min(fragility.fragility_delta / max(self.MAX_FRAGILITY_DELTA, 1e-9), 1.0)),
            (self.W_DRAWDOWN, 1.0 - min(drawdown.dd_contribution / max(self.MAX_DRAWDOWN_CONTRIBUTION, 1e-9), 1.0)),
        ]
        confidence = sum(w * s for w, s in scores)

        # Verdict
        if len(rejection_reasons) == 0 and confidence >= 0.70:
            verdict = ValidationVerdict.APPROVE
        elif len(rejection_reasons) <= 1 and confidence >= 0.45:
            verdict = ValidationVerdict.HOLD
        else:
            verdict = ValidationVerdict.REJECT

        result = ValidationResult(
            adaptation_id=adaptation_id,
            verdict=verdict,
            confidence=confidence,
            rejection_reasons=rejection_reasons,
            evidence=evidence,
            robustness=robustness,
            execution_adjusted=execution,
            fragility=fragility,
            drawdown=drawdown,
        )
        logger.info(
            "Validation %s → %s (confidence=%.2f, rejections=%s)",
            adaptation_id, verdict.value, confidence, rejection_reasons,
        )
        return result

    # ── Private checks ───────────────────────────────────────────

    def _check_evidence(
        self, sample_size: int, wr_ci_lower: float,
        exp_stab: float, pf_stab: float,
    ) -> EvidenceQuality:
        is_sufficient = (
            sample_size >= self.MIN_SAMPLE_SIZE
            and wr_ci_lower >= self.MIN_WIN_RATE_CI_LOWER
            and exp_stab >= self.MIN_EXPECTANCY_STABILITY
            and pf_stab >= self.MIN_PF_STABILITY
        )
        return EvidenceQuality(
            sample_size=sample_size,
            min_required=self.MIN_SAMPLE_SIZE,
            win_rate_ci_lower=wr_ci_lower,
            expectancy_stability=exp_stab,
            profit_factor_stability=pf_stab,
            is_sufficient=is_sufficient,
        )

    def _check_robustness(
        self, windows_tested: int, windows_positive: int,
        consistency: float, regime_dep: float, worst_exp: float,
    ) -> RobustnessCheck:
        ratio = windows_positive / max(windows_tested, 1)
        is_robust = (
            ratio >= self.MIN_WINDOWS_POSITIVE_RATIO
            and consistency >= self.MIN_CONSISTENCY_SCORE
            and regime_dep <= self.MAX_REGIME_DEPENDENCY
            and worst_exp > 0
        )
        return RobustnessCheck(
            windows_tested=windows_tested,
            windows_positive=windows_positive,
            consistency_score=consistency,
            regime_dependency=regime_dep,
            worst_window_expectancy=worst_exp,
            is_robust=is_robust,
        )

    def _check_execution(
        self, raw_improvement: float, exec_improvement: float,
    ) -> ExecutionAdjustedCheck:
        cost_ratio = 0.0
        if abs(raw_improvement) > 1e-9:
            cost_ratio = 1.0 - (exec_improvement / raw_improvement)
        survives = (
            exec_improvement > self.MIN_EXECUTION_ADJUSTED_IMPROVEMENT
            and cost_ratio <= self.MAX_EXECUTION_COST_RATIO
        )
        return ExecutionAdjustedCheck(
            raw_improvement=raw_improvement,
            execution_adjusted_improvement=exec_improvement,
            execution_cost_ratio=max(cost_ratio, 0.0),
            survives_execution=survives,
        )

    def _check_fragility(
        self, before: float, after: float,
    ) -> FragilityCheck:
        delta = after - before
        return FragilityCheck(
            fragility_before=before,
            fragility_after=after,
            fragility_delta=delta,
            max_acceptable_delta=self.MAX_FRAGILITY_DELTA,
            is_safe=delta <= self.MAX_FRAGILITY_DELTA,
        )

    def _check_drawdown(
        self, dd_before: float, dd_after: float,
    ) -> DrawdownCheck:
        contribution = max(dd_after - dd_before, 0.0)
        return DrawdownCheck(
            max_dd_before=dd_before,
            max_dd_after=dd_after,
            dd_contribution=contribution,
            max_acceptable_dd=self.MAX_DRAWDOWN_CONTRIBUTION,
            is_acceptable=contribution <= self.MAX_DRAWDOWN_CONTRIBUTION,
        )
