"""
A/B Comparison Engine
═════════════════════

Compares baseline vs adapted behavior across multiple dimensions:

  • Expectancy
  • Drawdown
  • Execution quality
  • Recovery behavior
  • Portfolio impact

Determines whether a candidate is truly superior or only
temporarily better due to noise / favourable conditions.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Any, Dict, List, Optional

from backend.services.trading.governance_models import (
    ABComparisonResult,
    ABMetrics,
    ABOutcome,
)

logger = logging.getLogger(__name__)


class ABComparisonEngine:
    """
    Stateless comparison engine.  Given baseline and candidate metrics,
    produces a rigorous verdict on whether the candidate is truly superior.
    """

    # ── Weights for composite score ─────────────────────────────
    W_EXPECTANCY: float = 0.30
    W_DRAWDOWN: float = 0.20
    W_EXECUTION: float = 0.15
    W_RECOVERY: float = 0.15
    W_PORTFOLIO: float = 0.20

    # Minimum superiority margin to declare candidate better
    MIN_COMPOSITE_MARGIN: float = 0.05
    # Minimum trades required on each side for valid comparison
    MIN_TRADES_PER_SIDE: int = 20
    # Confidence calculation: how many dimensions must be better
    MIN_DIMENSIONS_BETTER: int = 3

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            if hasattr(self, key.upper()):
                setattr(self, key.upper(), value)

    # ── Public API ───────────────────────────────────────────────

    def compare(
        self,
        candidate_id: str,
        baseline: ABMetrics,
        candidate: ABMetrics,
    ) -> ABComparisonResult:
        """Run a full A/B comparison and return verdict."""

        comparison_id = self._make_id(candidate_id)

        # Insufficient data check
        if baseline.trade_count < self.MIN_TRADES_PER_SIDE or candidate.trade_count < self.MIN_TRADES_PER_SIDE:
            return ABComparisonResult(
                comparison_id=comparison_id,
                candidate_id=candidate_id,
                baseline=baseline,
                candidate=candidate,
                outcome=ABOutcome.INCONCLUSIVE,
                confidence=0.0,
                reason="insufficient_trade_count",
            )

        # Per-dimension deltas (positive = candidate better)
        exp_delta = self._expectancy_delta(baseline, candidate)
        dd_delta = self._drawdown_delta(baseline, candidate)
        exec_delta = self._execution_delta(baseline, candidate)
        recovery_delta = self._recovery_delta(baseline, candidate)
        portfolio_delta = self._portfolio_delta(baseline, candidate)

        # Composite score
        composite = (
            self.W_EXPECTANCY * exp_delta
            + self.W_DRAWDOWN * dd_delta
            + self.W_EXECUTION * exec_delta
            + self.W_RECOVERY * recovery_delta
            + self.W_PORTFOLIO * portfolio_delta
        )

        # How many dimensions favour candidate
        dims_better = sum(1 for d in [exp_delta, dd_delta, exec_delta, recovery_delta, portfolio_delta] if d > 0)

        # Confidence based on dimensionality and margin
        confidence = min(dims_better / 5.0, 1.0) * min(abs(composite) / max(self.MIN_COMPOSITE_MARGIN, 1e-9), 1.0)
        confidence = max(0.0, min(confidence, 1.0))

        # Determine outcome
        if composite > self.MIN_COMPOSITE_MARGIN and dims_better >= self.MIN_DIMENSIONS_BETTER:
            outcome = ABOutcome.CANDIDATE_SUPERIOR
            is_superior = True
            reason = f"candidate better on {dims_better}/5 dimensions, composite={composite:+.4f}"
        elif composite < -self.MIN_COMPOSITE_MARGIN and dims_better <= 1:
            outcome = ABOutcome.CANDIDATE_HARMFUL
            is_superior = False
            reason = f"candidate worse on {5 - dims_better}/5 dimensions, composite={composite:+.4f}"
        elif composite < -self.MIN_COMPOSITE_MARGIN:
            outcome = ABOutcome.BASELINE_SUPERIOR
            is_superior = False
            reason = f"baseline better, composite={composite:+.4f}"
        else:
            outcome = ABOutcome.INCONCLUSIVE
            is_superior = False
            reason = f"no clear winner, composite={composite:+.4f}, dims_better={dims_better}"

        result = ABComparisonResult(
            comparison_id=comparison_id,
            candidate_id=candidate_id,
            baseline=baseline,
            candidate=candidate,
            outcome=outcome,
            confidence=confidence,
            expectancy_delta=exp_delta,
            drawdown_delta=dd_delta,
            execution_delta=exec_delta,
            recovery_delta=recovery_delta,
            portfolio_delta=portfolio_delta,
            is_truly_superior=is_superior,
            reason=reason,
        )
        logger.info(
            "A/B %s: %s (confidence=%.2f) — %s",
            comparison_id, outcome.value, confidence, reason,
        )
        return result

    # ── Delta computations (positive = candidate better) ─────────

    @staticmethod
    def _expectancy_delta(b: ABMetrics, c: ABMetrics) -> float:
        """Normalised expectancy difference."""
        denom = max(abs(b.expectancy), 1e-9)
        return (c.expectancy - b.expectancy) / denom

    @staticmethod
    def _drawdown_delta(b: ABMetrics, c: ABMetrics) -> float:
        """Lower drawdown = better.  Positive means candidate has less DD."""
        if max(b.max_drawdown, c.max_drawdown) < 1e-9:
            return 0.0
        return (b.max_drawdown - c.max_drawdown) / max(b.max_drawdown, 1e-9)

    @staticmethod
    def _execution_delta(b: ABMetrics, c: ABMetrics) -> float:
        """Higher execution quality = better."""
        return c.execution_quality - b.execution_quality

    @staticmethod
    def _recovery_delta(b: ABMetrics, c: ABMetrics) -> float:
        """Faster recovery = better.  Positive means candidate recovers faster."""
        if max(b.avg_recovery_time, c.avg_recovery_time) < 1e-9:
            return 0.0
        return (b.avg_recovery_time - c.avg_recovery_time) / max(b.avg_recovery_time, 1e-9)

    @staticmethod
    def _portfolio_delta(b: ABMetrics, c: ABMetrics) -> float:
        """Higher portfolio impact (diversification) = better."""
        return c.portfolio_impact - b.portfolio_impact

    @staticmethod
    def _make_id(candidate_id: str) -> str:
        h = hashlib.sha256(f"ab:{candidate_id}:{time.time()}".encode()).hexdigest()[:12]
        return f"ab_{h}"
