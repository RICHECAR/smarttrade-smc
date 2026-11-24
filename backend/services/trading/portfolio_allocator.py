"""
Portfolio Allocator — Capital allocation across setups and symbols
════════════════════════════════════════════════════════════════

Allocates capital by prioritizing:
  1. Strongest execution-adjusted edge
  2. Highest robustness score
  3. Best regime fit

Respects a global risk budget and applies haircuts from
CorrelationRiskEngine and PortfolioDrawdownGovernor.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from backend.services.trading.portfolio_models import (
    PortfolioAllocation,
    PortfolioSnapshot,
    SetupOpportunity,
)


class PortfolioAllocator:
    """Allocate portfolio capital across competing setups."""

    # Composite scoring weights
    W_EDGE = 0.40
    W_ROBUSTNESS = 0.25
    W_REGIME_FIT = 0.20
    W_EXEC_QUALITY = 0.15

    # Risk budget
    MAX_SINGLE_ALLOC = 0.20             # max 20% in one setup
    MAX_SINGLE_SYMBOL = 0.30            # max 30% in one symbol
    MAX_TOTAL_UTILIZATION = 0.90        # keep 10% cash buffer
    MIN_ALLOC = 0.01                    # below this → skip

    def __init__(
        self,
        max_single_alloc: float = 0.20,
        max_single_symbol: float = 0.30,
        max_utilization: float = 0.90,
    ) -> None:
        self.MAX_SINGLE_ALLOC = max_single_alloc
        self.MAX_SINGLE_SYMBOL = max_single_symbol
        self.MAX_TOTAL_UTILIZATION = max_utilization

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def allocate(
        self,
        opportunities: List[SetupOpportunity],
        total_capital: float,
        drawdown_multiplier: float = 1.0,
        correlation_haircuts: Optional[Dict[str, float]] = None,
        stress_haircuts: Optional[Dict[str, float]] = None,
    ) -> PortfolioSnapshot:
        """
        Allocate capital to setups.

        Parameters
        ----------
        opportunities : candidate setups with their scores
        total_capital : total portfolio value (USD)
        drawdown_multiplier : 0–1 from DrawdownGovernor
        correlation_haircuts : setup_key → mult (0–1) from CorrelationRiskEngine
        stress_haircuts : setup_key → mult (0–1) from StressTester
        """
        if not opportunities or total_capital <= 0:
            return PortfolioSnapshot(total_capital=total_capital,
                                     free_capital=total_capital)

        corr_cuts = correlation_haircuts or {}
        stress_cuts = stress_haircuts or {}

        # 1. Score and rank
        scored = self._score_and_rank(opportunities)

        # 2. Allocate greedily by rank
        allocations: List[PortfolioAllocation] = []
        symbol_usage: Dict[str, float] = {}
        total_alloc_pct = 0.0
        budget = self.MAX_TOTAL_UTILIZATION * drawdown_multiplier

        for rank, (score, opp) in enumerate(scored, 1):
            if total_alloc_pct >= budget:
                break

            # Base allocation from requested
            raw_pct = opp.requested_allocation_pct
            if raw_pct <= 0:
                # Derive from composite score
                raw_pct = score * 0.15  # 15% for perfect score

            adjustments: Dict[str, float] = {"raw": raw_pct}

            # Cap per-setup
            alloc = min(raw_pct, self.MAX_SINGLE_ALLOC)
            adjustments["single_cap"] = alloc

            # Apply drawdown multiplier
            alloc *= drawdown_multiplier
            adjustments["drawdown_mult"] = drawdown_multiplier

            # Apply correlation haircut
            corr_mult = corr_cuts.get(opp.setup_key, 1.0)
            alloc *= corr_mult
            adjustments["correlation_mult"] = corr_mult

            # Apply stress haircut
            stress_mult = stress_cuts.get(opp.setup_key, 1.0)
            alloc *= stress_mult
            adjustments["stress_mult"] = stress_mult

            # Cap per-symbol
            sym_used = symbol_usage.get(opp.symbol, 0.0)
            sym_room = max(0.0, self.MAX_SINGLE_SYMBOL - sym_used)
            alloc = min(alloc, sym_room)
            adjustments["symbol_room"] = sym_room

            # Cap to remaining budget
            budget_room = budget - total_alloc_pct
            alloc = min(alloc, budget_room)

            # Skip tiny allocations
            if alloc < self.MIN_ALLOC:
                continue

            total_alloc_pct += alloc
            symbol_usage[opp.symbol] = sym_used + alloc

            allocations.append(PortfolioAllocation(
                setup_key=opp.setup_key,
                symbol=opp.symbol,
                allocated_pct=alloc,
                allocated_capital=alloc * total_capital,
                priority_rank=rank,
                adjustments=adjustments,
                reason=f"score={score:.3f}",
            ))

        # Correlation total haircut
        total_corr_haircut = 0.0
        for a in allocations:
            cut = a.adjustments.get("correlation_mult", 1.0)
            if cut < 1.0:
                total_corr_haircut += a.adjustments.get("raw", 0) * (1.0 - cut)

        return PortfolioSnapshot(
            total_capital=total_capital,
            allocated_capital=total_alloc_pct * total_capital,
            free_capital=(1.0 - total_alloc_pct) * total_capital,
            utilization=total_alloc_pct,
            allocations=allocations,
            correlation_haircut=total_corr_haircut,
        )

    # ────────────────────────────────────────────────────
    #  INTERNAL
    # ────────────────────────────────────────────────────

    def _score_and_rank(
        self, opps: List[SetupOpportunity],
    ) -> List[tuple[float, SetupOpportunity]]:
        """Score each opportunity and return sorted (desc) list."""
        scored: List[tuple[float, SetupOpportunity]] = []
        for opp in opps:
            composite = (
                self.W_EDGE * self._normalize_edge(opp.adjusted_edge)
                + self.W_ROBUSTNESS * opp.robustness_score
                + self.W_REGIME_FIT * opp.regime_fit
                + self.W_EXEC_QUALITY * opp.execution_quality
            )
            composite = max(0.0, min(1.0, composite))
            scored.append((composite, opp))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    @staticmethod
    def _normalize_edge(edge: float) -> float:
        """Normalize adjusted edge to 0–1 range. Assumes edge in USD."""
        if edge <= 0:
            return 0.0
        # Soft sigmoid-like normalization
        return min(1.0, edge / (edge + 10.0))
