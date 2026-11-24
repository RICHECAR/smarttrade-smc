"""
Capacity Stress Analyzer — Size-scaling impact analysis
═══════════════════════════════════════════════════════

Measures how PnL, slippage, and fill quality degrade as position
size increases. Outputs a recommended allocation ceiling and a
capacity score.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from backend.services.trading.execution_models import (
    CapacityBucket,
    CapacityReport,
    ExecutionRecord,
    FillStatus,
)


class CapacityStressAnalyzer:
    """Analyze execution degradation as size scales up."""

    # Default size bucket boundaries (absolute USD notional)
    DEFAULT_BUCKETS = [
        ("xs", 0.0, 100.0),
        ("s", 100.0, 500.0),
        ("m", 500.0, 2000.0),
        ("l", 2000.0, 10000.0),
        ("xl", 10000.0, float("inf")),
    ]

    # Edge-retention threshold: if retained < this, degradation onset
    DEGRADATION_THRESHOLD = 0.70

    def __init__(
        self,
        size_buckets: List[tuple] | None = None,
    ) -> None:
        self._buckets = size_buckets or self.DEFAULT_BUCKETS

    # ──────────────────────────────────────────────────────
    #  PUBLIC
    # ──────────────────────────────────────────────────────

    def analyze(
        self,
        setup_key: str,
        records: List[ExecutionRecord],
    ) -> CapacityReport:
        """Build a capacity report from execution records."""
        if not records:
            return CapacityReport(setup_key=setup_key)

        # Group records into size buckets
        grouped: Dict[str, List[ExecutionRecord]] = defaultdict(list)
        for rec in records:
            notional = rec.intended_size * max(rec.expected_entry, 1e-12)
            for label, lo, hi in self._buckets:
                if lo <= notional < hi:
                    grouped[label].append(rec)
                    break

        # Compute per-bucket stats
        bucket_results: List[CapacityBucket] = []
        base_pnl: float | None = None

        for label, lo, hi in self._buckets:
            recs = grouped.get(label, [])
            if not recs:
                bucket_results.append(CapacityBucket(
                    size_label=label, size_min=lo,
                    size_max=hi if hi != float("inf") else 999999.0,
                ))
                continue

            avg_pnl = sum(r.realized_pnl for r in recs) / len(recs)
            avg_slip = sum(r.total_execution_cost for r in recs) / len(recs)
            avg_fill = sum(
                r.filled_size / max(r.intended_size, 1e-12) for r in recs
            ) / len(recs)

            if base_pnl is None and len(recs) >= 3:
                base_pnl = avg_pnl

            retained = 1.0
            if base_pnl and abs(base_pnl) > 1e-12:
                retained = avg_pnl / base_pnl
                retained = max(0.0, min(2.0, retained))

            bucket_results.append(CapacityBucket(
                size_label=label,
                size_min=lo,
                size_max=hi if hi != float("inf") else 999999.0,
                trade_count=len(recs),
                avg_pnl=avg_pnl,
                avg_slippage=avg_slip,
                avg_fill_ratio=avg_fill,
                edge_retained_pct=retained,
            ))

        # Find degradation onset and recommended max
        degradation_onset = ""
        recommended_max = float("inf")

        for cb in bucket_results:
            if cb.trade_count < 3:
                continue
            if cb.edge_retained_pct < self.DEGRADATION_THRESHOLD:
                if not degradation_onset:
                    degradation_onset = cb.size_label
                    recommended_max = cb.size_min
                    break

        # Capacity score: average edge_retained across populated buckets
        populated = [b for b in bucket_results if b.trade_count >= 3]
        capacity_score = 1.0
        if populated:
            capacity_score = sum(b.edge_retained_pct for b in populated) / len(populated)
            capacity_score = max(0.0, min(1.0, capacity_score))

        return CapacityReport(
            setup_key=setup_key,
            buckets=bucket_results,
            recommended_max_size=recommended_max if recommended_max != float("inf") else 0.0,
            capacity_score=capacity_score,
            degradation_onset=degradation_onset,
        )
