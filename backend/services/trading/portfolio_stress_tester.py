"""
Portfolio Stress Tester — Simulate adverse scenarios
═════════════════════════════════════════════════════

Scenarios:
  1. VOLATILITY_SPIKE — slippage doubles, spread triples
  2. CORRELATED_DRAWDOWN — all positions move against simultaneously
  3. SLIPPAGE_EXPANSION — execution costs increase 3×
  4. LIQUIDITY_DETERIORATION — fill rate drops, partial fills increase

Outputs stress-adjusted allocation guidance per setup.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from typing import Dict, List

from backend.services.trading.portfolio_models import (
    SetupOpportunity,
    StressReport,
    StressResult,
    StressScenario,
)


class PortfolioStressTester:
    """Simulate stress scenarios on portfolio setups."""

    # Stress multipliers per scenario
    SCENARIO_PARAMS: Dict[StressScenario, Dict[str, float]] = {
        StressScenario.VOLATILITY_SPIKE: {
            "slippage_mult": 2.0,
            "spread_mult": 3.0,
            "fill_penalty": 0.10,       # 10% more misses
            "edge_impact": 0.60,        # 60% of edge survives
        },
        StressScenario.CORRELATED_DRAWDOWN: {
            "slippage_mult": 1.5,
            "spread_mult": 2.0,
            "fill_penalty": 0.05,
            "edge_impact": 0.40,        # 40% of diversification-adjusted edge
        },
        StressScenario.SLIPPAGE_EXPANSION: {
            "slippage_mult": 3.0,
            "spread_mult": 2.0,
            "fill_penalty": 0.05,
            "edge_impact": 0.50,
        },
        StressScenario.LIQUIDITY_DETERIORATION: {
            "slippage_mult": 2.0,
            "spread_mult": 2.5,
            "fill_penalty": 0.25,       # 25% more fill issues
            "edge_impact": 0.45,
        },
    }

    def __init__(self) -> None:
        pass

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def stress_test_setup(
        self,
        opp: SetupOpportunity,
        execution_cost: float = 0.0,
    ) -> StressReport:
        """Run all stress scenarios on one setup."""
        results: List[StressResult] = []

        for scenario, params in self.SCENARIO_PARAMS.items():
            result = self._simulate_scenario(opp, scenario, params, execution_cost)
            results.append(result)

        # Worst and composite
        worst = min(results, key=lambda r: r.edge_survival_pct)
        avg_survival = (
            sum(r.edge_survival_pct for r in results) / len(results)
            if results else 1.0
        )

        return StressReport(
            setup_key=opp.setup_key,
            results=results,
            worst_scenario=worst.scenario.value,
            worst_survival=worst.edge_survival_pct,
            composite_resilience=avg_survival,
        )

    def stress_test_portfolio(
        self,
        opportunities: List[SetupOpportunity],
        execution_costs: Dict[str, float] | None = None,
    ) -> Dict[str, StressReport]:
        """Run stress on all setups. Returns {setup_key: StressReport}."""
        costs = execution_costs or {}
        return {
            opp.setup_key: self.stress_test_setup(
                opp, costs.get(opp.setup_key, 0.0)
            )
            for opp in opportunities
        }

    def get_stress_haircuts(
        self,
        reports: Dict[str, StressReport],
        min_resilience: float = 0.30,
    ) -> Dict[str, float]:
        """
        Convert stress reports to haircut multipliers.

        Returns setup_key → mult (0–1). Setups below min_resilience
        get blocked (mult=0).
        """
        haircuts: Dict[str, float] = {}
        for sk, report in reports.items():
            if report.composite_resilience < min_resilience:
                haircuts[sk] = 0.0  # block
            else:
                # Scale linearly: 0.30→0.5x, 1.0→1.0x
                haircuts[sk] = 0.5 + 0.5 * (
                    (report.composite_resilience - min_resilience)
                    / (1.0 - min_resilience)
                )
                haircuts[sk] = max(0.0, min(1.0, haircuts[sk]))
        return haircuts

    # ────────────────────────────────────────────────────
    #  INTERNAL
    # ────────────────────────────────────────────────────

    def _simulate_scenario(
        self,
        opp: SetupOpportunity,
        scenario: StressScenario,
        params: Dict[str, float],
        execution_cost: float,
    ) -> StressResult:
        """Simulate one scenario on one setup."""
        original_edge = opp.adjusted_edge

        # Stressed execution cost
        stressed_cost = execution_cost * params["slippage_mult"]

        # Edge survival factor
        base_survival = params["edge_impact"]

        # Robust setups survive better
        robustness_bonus = opp.robustness_score * 0.20
        quality_bonus = opp.execution_quality * 0.10

        survival = base_survival + robustness_bonus + quality_bonus
        survival = max(0.0, min(1.0, survival))

        stressed_edge = original_edge * survival - stressed_cost

        # Survival percentage
        if abs(original_edge) > 1e-12:
            surv_pct = max(0.0, stressed_edge / original_edge)
        else:
            surv_pct = 0.0

        # Recommended allocation multiplier
        if surv_pct >= 0.80:
            alloc_mult = 1.0
        elif surv_pct >= 0.50:
            alloc_mult = 0.70
        elif surv_pct >= 0.20:
            alloc_mult = 0.40
        elif surv_pct > 0:
            alloc_mult = 0.20
        else:
            alloc_mult = 0.0

        return StressResult(
            setup_key=opp.setup_key,
            scenario=scenario,
            original_edge=original_edge,
            stressed_edge=stressed_edge,
            edge_survival_pct=surv_pct,
            recommended_alloc_mult=alloc_mult,
        )
