"""
Execution-Adjusted Edge Engine — True edge after execution costs
════════════════════════════════════════════════════════════════

Recomputes a setup's expectancy after accounting for:
  • Entry/exit slippage
  • Spread costs
  • Partial fills / missed fills
  • Latency drag

Output: raw edge, adjusted edge, degradation score.
Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from typing import List

from backend.services.trading.execution_models import (
    AdjustedEdgeReport,
    ExecutionRecord,
    FillStatus,
)


class ExecutionAdjustedEdgeEngine:
    """Compare theoretical edge with execution-adjusted reality."""

    # If adjusted edge < this fraction of raw edge, flag as WARN
    WARN_THRESHOLD = 0.50
    # If adjusted edge is negative, flag as edge-killed
    EDGE_KILL_THRESHOLD = 0.0

    def __init__(self) -> None:
        pass

    # ──────────────────────────────────────────────────────
    #  PUBLIC
    # ──────────────────────────────────────────────────────

    def evaluate(
        self,
        setup_key: str,
        records: List[ExecutionRecord],
    ) -> AdjustedEdgeReport:
        """Compute execution-adjusted edge from trade records."""
        if not records:
            return AdjustedEdgeReport(setup_key=setup_key, trade_count=0)

        n = len(records)
        raw_pnls = [r.raw_pnl for r in records]
        realized_pnls = [r.realized_pnl for r in records]
        costs = [r.total_execution_cost for r in records]

        raw_expectancy = sum(raw_pnls) / n
        avg_cost = sum(costs) / n
        adjusted_expectancy = sum(realized_pnls) / n

        # Execution failure rate (missed + rejected)
        failures = sum(
            1 for r in records
            if r.fill_status in (FillStatus.MISSED, FillStatus.REJECTED)
        )
        failure_rate = failures / n

        # Edge degradation: how much of raw edge is consumed by execution
        if abs(raw_expectancy) > 1e-12:
            degradation = 1.0 - (adjusted_expectancy / raw_expectancy)
            degradation = max(0.0, min(1.0, degradation))
        else:
            degradation = 0.0

        edge_positive = adjusted_expectancy > self.EDGE_KILL_THRESHOLD

        return AdjustedEdgeReport(
            setup_key=setup_key,
            raw_expectancy=raw_expectancy,
            avg_execution_cost=avg_cost,
            execution_failure_rate=failure_rate,
            adjusted_expectancy=adjusted_expectancy,
            edge_degradation=degradation,
            edge_positive=edge_positive,
            trade_count=n,
        )

    def edge_still_viable(self, report: AdjustedEdgeReport) -> bool:
        """Quick check: is the adjusted edge still positive?"""
        return report.edge_positive

    def edge_severely_degraded(self, report: AdjustedEdgeReport) -> bool:
        """True if more than 50% of raw edge consumed by execution."""
        return report.edge_degradation > self.WARN_THRESHOLD
