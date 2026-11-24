"""
Capital Scaling Governor
════════════════════════

Controls how much capital an adapted behavior is allowed to use.

Scaling stages with default fractions:

  SANDBOX      → 0 %   (no real capital)
  MICRO        → 2 %
  SMALL        → 5 %
  MEDIUM       → 15 %
  FULL         → 100 %

Evidence + stability requirements must be met before scaling up.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from backend.services.trading.governance_models import (
    CapitalAllocation,
    CapitalStage,
    ScalingDecision,
)

logger = logging.getLogger(__name__)

# Ordered scaling path
_SCALING_ORDER: List[CapitalStage] = [
    CapitalStage.SANDBOX,
    CapitalStage.MICRO,
    CapitalStage.SMALL,
    CapitalStage.MEDIUM,
    CapitalStage.FULL,
]

# Default capital fractions per stage
_DEFAULT_FRACTIONS: Dict[CapitalStage, float] = {
    CapitalStage.SANDBOX: 0.00,
    CapitalStage.MICRO: 0.02,
    CapitalStage.SMALL: 0.05,
    CapitalStage.MEDIUM: 0.15,
    CapitalStage.FULL: 1.00,
}

# Evidence thresholds per stage
_DEFAULT_EVIDENCE: Dict[CapitalStage, Dict[str, float]] = {
    CapitalStage.MICRO: {
        "min_trades": 10, "min_win_rate": 0.40,
        "min_expectancy": 0.0, "min_sharpe": 0.0,
        "max_drawdown": 0.25, "max_fragility": 0.70,
    },
    CapitalStage.SMALL: {
        "min_trades": 25, "min_win_rate": 0.45,
        "min_expectancy": 0.3, "min_sharpe": 0.2,
        "max_drawdown": 0.18, "max_fragility": 0.55,
    },
    CapitalStage.MEDIUM: {
        "min_trades": 50, "min_win_rate": 0.48,
        "min_expectancy": 0.8, "min_sharpe": 0.4,
        "max_drawdown": 0.14, "max_fragility": 0.45,
    },
    CapitalStage.FULL: {
        "min_trades": 100, "min_win_rate": 0.50,
        "min_expectancy": 1.2, "min_sharpe": 0.6,
        "max_drawdown": 0.10, "max_fragility": 0.35,
    },
}


class CapitalScalingGovernor:
    """
    Manages capital allocation per candidate, progressing through
    scaling stages only when evidence thresholds are satisfied.
    """

    def __init__(
        self,
        fractions: Optional[Dict[CapitalStage, float]] = None,
        evidence_thresholds: Optional[Dict[CapitalStage, Dict[str, float]]] = None,
    ) -> None:
        self._fractions = fractions or dict(_DEFAULT_FRACTIONS)
        self._evidence = evidence_thresholds or dict(_DEFAULT_EVIDENCE)
        self._allocations: Dict[str, CapitalAllocation] = {}

    # ── Public API ───────────────────────────────────────────────

    def register(self, candidate_id: str) -> CapitalAllocation:
        """Register a new candidate at SANDBOX with 0% capital."""
        alloc = CapitalAllocation(
            candidate_id=candidate_id,
            stage=CapitalStage.SANDBOX,
            capital_fraction=self._fractions[CapitalStage.SANDBOX],
            max_position_size=0.0,
            stage_history=[{"stage": CapitalStage.SANDBOX.value, "ts": time.time()}],
        )
        self._allocations[candidate_id] = alloc
        return alloc

    def evaluate_scaling(
        self,
        candidate_id: str,
        *,
        trades: int = 0,
        win_rate: float = 0.0,
        expectancy: float = 0.0,
        sharpe: float = 0.0,
        drawdown: float = 0.0,
        fragility: float = 0.0,
        total_capital: float = 0.0,
    ) -> ScalingDecision:
        """
        Evaluate whether a candidate should scale up, stay, or scale down.
        """
        alloc = self._allocations.get(candidate_id)
        if alloc is None:
            alloc = self.register(candidate_id)

        current = alloc.stage
        next_stage = self._next_stage(current)

        # Check for scale-up
        if next_stage is not None:
            reqs = self._evidence.get(next_stage, {})
            evidence_met = self._check_evidence(reqs, trades, win_rate, expectancy, sharpe, drawdown, fragility)
            # Stability: at least been in current stage for some trades
            stability_met = trades >= reqs.get("min_trades", 0) * 0.5

            if evidence_met and stability_met:
                return self._scale_to(alloc, next_stage, total_capital, evidence_met, stability_met,
                                      f"promoted from {current.value} to {next_stage.value}")

        # Check for scale-down (if current metrics degrade)
        if current not in (CapitalStage.SANDBOX, CapitalStage.MICRO):
            current_reqs = self._evidence.get(current, {})
            if current_reqs and not self._check_evidence(
                current_reqs, trades, win_rate, expectancy, sharpe, drawdown, fragility,
            ):
                prev_stage = self._prev_stage(current)
                if prev_stage is not None:
                    return self._scale_to(alloc, prev_stage, total_capital, False, False,
                                          f"degraded from {current.value} to {prev_stage.value}")

        # No change
        return ScalingDecision(
            candidate_id=candidate_id,
            previous_stage=current,
            new_stage=current,
            previous_fraction=alloc.capital_fraction,
            new_fraction=alloc.capital_fraction,
            reason="no_change",
            evidence_met=True,
            stability_met=True,
        )

    def force_sandbox(self, candidate_id: str) -> ScalingDecision:
        """Emergency: force candidate to SANDBOX (0% capital)."""
        alloc = self._allocations.get(candidate_id)
        if alloc is None:
            alloc = self.register(candidate_id)
        return self._scale_to(alloc, CapitalStage.SANDBOX, 0.0, False, False, "forced_sandbox")

    def get_allocation(self, candidate_id: str) -> Optional[CapitalAllocation]:
        return self._allocations.get(candidate_id)

    def get_all_allocations(self) -> List[CapitalAllocation]:
        return list(self._allocations.values())

    def get_total_utilisation(self) -> float:
        """Sum of all active capital fractions (should not exceed 1.0)."""
        return sum(a.capital_fraction for a in self._allocations.values())

    # ── Private helpers ──────────────────────────────────────────

    def _scale_to(
        self, alloc: CapitalAllocation, stage: CapitalStage,
        total_capital: float, evidence: bool, stability: bool, reason: str,
    ) -> ScalingDecision:
        prev_stage = alloc.stage
        prev_fraction = alloc.capital_fraction
        new_fraction = self._fractions.get(stage, 0.0)

        alloc.stage = stage
        alloc.capital_fraction = new_fraction
        alloc.max_position_size = total_capital * new_fraction
        alloc.last_evaluation = time.time()
        alloc.stage_history.append({"stage": stage.value, "ts": time.time()})

        decision = ScalingDecision(
            candidate_id=alloc.candidate_id,
            previous_stage=prev_stage,
            new_stage=stage,
            previous_fraction=prev_fraction,
            new_fraction=new_fraction,
            reason=reason,
            evidence_met=evidence,
            stability_met=stability,
        )
        logger.info(
            "Capital scaling %s: %s → %s (%.1f%% → %.1f%%)",
            alloc.candidate_id, prev_stage.value, stage.value,
            prev_fraction * 100, new_fraction * 100,
        )
        return decision

    @staticmethod
    def _check_evidence(
        reqs: Dict[str, float], trades: int, win_rate: float,
        expectancy: float, sharpe: float, drawdown: float, fragility: float,
    ) -> bool:
        return (
            trades >= reqs.get("min_trades", 0)
            and win_rate >= reqs.get("min_win_rate", 0.0)
            and expectancy >= reqs.get("min_expectancy", 0.0)
            and sharpe >= reqs.get("min_sharpe", 0.0)
            and drawdown <= reqs.get("max_drawdown", 1.0)
            and fragility <= reqs.get("max_fragility", 1.0)
        )

    @staticmethod
    def _next_stage(current: CapitalStage) -> Optional[CapitalStage]:
        try:
            idx = _SCALING_ORDER.index(current)
            if idx + 1 < len(_SCALING_ORDER):
                return _SCALING_ORDER[idx + 1]
        except ValueError:
            pass
        return None

    @staticmethod
    def _prev_stage(current: CapitalStage) -> Optional[CapitalStage]:
        try:
            idx = _SCALING_ORDER.index(current)
            if idx - 1 >= 0:
                return _SCALING_ORDER[idx - 1]
        except ValueError:
            pass
        return None
