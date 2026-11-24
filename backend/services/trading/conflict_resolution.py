"""
Conflict Resolution Engine — Resolve capital competition between signals
══════════════════════════════════════════════════════════════════════

When multiple setups compete for limited capital, ranks them by:
  1. Execution-adjusted edge
  2. Robustness score
  3. Execution quality
  4. Portfolio diversification value (anti-correlation bonus)

Winners get full allocation, runners-up get reduced, losers rejected.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from backend.services.trading.portfolio_models import (
    ConflictDecision,
    ConflictOutcome,
    ConflictReport,
    SetupOpportunity,
)


class ConflictResolutionEngine:
    """Resolve capital conflicts among competing setups."""

    # Scoring weights for conflict ranking
    W_EDGE = 0.35
    W_ROBUSTNESS = 0.25
    W_EXEC_QUALITY = 0.20
    W_DIVERSIFICATION = 0.20

    # Allocation tiers
    WINNER_CUTOFF = 0.70        # top N% of scores get WINNER
    REDUCED_MULT = 0.50         # REDUCED setups get 50% of their ask

    def __init__(self) -> None:
        pass

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def resolve(
        self,
        opportunities: List[SetupOpportunity],
        available_budget_pct: float,
        diversification_values: Optional[Dict[str, float]] = None,
    ) -> ConflictReport:
        """
        Resolve conflicts for a limited capital budget.

        Parameters
        ----------
        opportunities : competing setups
        available_budget_pct : how much portfolio % is available
        diversification_values : setup_key → 0–1 diversification value
        """
        if not opportunities:
            return ConflictReport()

        div_vals = diversification_values or {}

        # 1. Score each setup
        scored: List[tuple[float, SetupOpportunity]] = []
        for opp in opportunities:
            div_val = div_vals.get(opp.setup_key, 0.5)
            composite = (
                self.W_EDGE * self._norm_edge(opp.adjusted_edge)
                + self.W_ROBUSTNESS * opp.robustness_score
                + self.W_EXEC_QUALITY * opp.execution_quality
                + self.W_DIVERSIFICATION * div_val
            )
            composite = max(0.0, min(1.0, composite))
            scored.append((composite, opp))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 2. Allocate greedily
        decisions: List[ConflictDecision] = []
        remaining = available_budget_pct
        total_requested = sum(o.requested_allocation_pct for _, o in scored)
        total_allocated = 0.0

        # Determine cutoff score for winner tier
        top_score = scored[0][0] if scored else 0.0
        cutoff = top_score * self.WINNER_CUTOFF

        for score, opp in scored:
            ask = opp.requested_allocation_pct
            if ask <= 0:
                ask = 0.05  # default 5%

            if remaining <= 0.001:
                decisions.append(ConflictDecision(
                    setup_key=opp.setup_key,
                    outcome=ConflictOutcome.REJECTED,
                    composite_score=score,
                    original_alloc_pct=ask,
                    final_alloc_pct=0.0,
                    reason="budget_exhausted",
                ))
                continue

            if score >= cutoff:
                # Winner: full allocation up to budget
                granted = min(ask, remaining)
                outcome = ConflictOutcome.WINNER
                reason = f"winner_score={score:.3f}"
            else:
                # Reduced: give reduced amount
                reduced_ask = ask * self.REDUCED_MULT
                granted = min(reduced_ask, remaining)
                if granted < 0.005:
                    granted = 0.0
                    outcome = ConflictOutcome.REJECTED
                    reason = f"reduced_insufficient_score={score:.3f}"
                else:
                    outcome = ConflictOutcome.REDUCED
                    reason = f"reduced_score={score:.3f}"

            remaining -= granted
            total_allocated += granted

            decisions.append(ConflictDecision(
                setup_key=opp.setup_key,
                outcome=outcome,
                composite_score=score,
                original_alloc_pct=ask,
                final_alloc_pct=granted,
                reason=reason,
            ))

        return ConflictReport(
            competing_setups=len(opportunities),
            available_budget_pct=available_budget_pct,
            decisions=decisions,
            total_requested=total_requested,
            total_allocated=total_allocated,
        )

    # ────────────────────────────────────────────────────
    #  INTERNAL
    # ────────────────────────────────────────────────────

    @staticmethod
    def _norm_edge(edge: float) -> float:
        if edge <= 0:
            return 0.0
        return min(1.0, edge / (edge + 10.0))
