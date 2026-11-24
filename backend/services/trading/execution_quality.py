"""
Execution Quality Analyzer — Per-trade and per-setup execution scoring
══════════════════════════════════════════════════════════════════════

Measures entry slippage, exit slippage, spread, fill ratio, and latency.
Produces a quality score 0–1 per trade and an aggregate per setup.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List

from backend.services.trading.execution_models import (
    ExecutionRecord,
    ExecutionQualityScore,
    FillStatus,
)


class ExecutionQualityAnalyzer:
    """Scores execution quality per trade and per setup."""

    # ── thresholds for scoring ───────────────────────────
    SLIP_EXCELLENT = 0.0001      # 0.01 % — (relative to price)
    SLIP_BAD = 0.005             # 0.5 %
    SPREAD_EXCELLENT = 0.0002
    SPREAD_BAD = 0.003
    LATENCY_EXCELLENT_MS = 50.0
    LATENCY_BAD_MS = 2000.0
    # Component weights
    W_ENTRY = 0.25
    W_EXIT = 0.20
    W_SPREAD = 0.15
    W_FILL = 0.25
    W_LATENCY = 0.15

    def __init__(self) -> None:
        self._records: Dict[str, List[ExecutionRecord]] = defaultdict(list)

    # ──────────────────────────────────────────────────────
    #  PUBLIC — record + score
    # ──────────────────────────────────────────────────────

    def record(self, rec: ExecutionRecord) -> ExecutionRecord:
        """Compute derived fields on a raw record, store it, return it."""
        rec = self._compute_derived(rec)
        self._records[rec.setup_key].append(rec)
        return rec

    def score_trade(self, rec: ExecutionRecord) -> ExecutionQualityScore:
        """Score a single trade's execution quality."""
        mid = (rec.expected_entry + rec.expected_exit) / 2 if rec.expected_entry else 1.0
        mid = max(mid, 1e-12)

        entry_s = self._slip_score(abs(rec.entry_slippage) / mid)
        exit_s = self._slip_score(abs(rec.exit_slippage) / mid)
        spread_s = self._spread_score(rec.spread_paid / mid)
        fill_s = self._fill_score(rec)
        lat_s = self._latency_score(rec.order_latency_ms)

        composite = (
            self.W_ENTRY * entry_s
            + self.W_EXIT * exit_s
            + self.W_SPREAD * spread_s
            + self.W_FILL * fill_s
            + self.W_LATENCY * lat_s
        )

        return ExecutionQualityScore(
            setup_key=rec.setup_key,
            quality_score=round(composite, 4),
            entry_slip_score=round(entry_s, 4),
            exit_slip_score=round(exit_s, 4),
            spread_score=round(spread_s, 4),
            fill_score=round(fill_s, 4),
            latency_score=round(lat_s, 4),
            trade_count=1,
            components={
                "entry_slippage_rel": round(abs(rec.entry_slippage) / mid, 6),
                "exit_slippage_rel": round(abs(rec.exit_slippage) / mid, 6),
                "spread_rel": round(rec.spread_paid / mid, 6),
                "fill_ratio": round(rec.filled_size / max(rec.intended_size, 1e-12), 4),
                "latency_ms": round(rec.order_latency_ms, 1),
            },
        )

    def score_setup(self, setup_key: str) -> ExecutionQualityScore:
        """Aggregate execution quality across all trades for a setup."""
        records = self._records.get(setup_key, [])
        if not records:
            return ExecutionQualityScore(setup_key=setup_key, trade_count=0)

        scores = [self.score_trade(r) for r in records]
        n = len(scores)
        return ExecutionQualityScore(
            setup_key=setup_key,
            quality_score=round(sum(s.quality_score for s in scores) / n, 4),
            entry_slip_score=round(sum(s.entry_slip_score for s in scores) / n, 4),
            exit_slip_score=round(sum(s.exit_slip_score for s in scores) / n, 4),
            spread_score=round(sum(s.spread_score for s in scores) / n, 4),
            fill_score=round(sum(s.fill_score for s in scores) / n, 4),
            latency_score=round(sum(s.latency_score for s in scores) / n, 4),
            trade_count=n,
            components={
                "worst_quality": round(min(s.quality_score for s in scores), 4),
                "best_quality": round(max(s.quality_score for s in scores), 4),
                "std_quality": round(self._std([s.quality_score for s in scores]), 4),
            },
        )

    def get_records(self, setup_key: str) -> List[ExecutionRecord]:
        return list(self._records.get(setup_key, []))

    def all_records(self) -> List[ExecutionRecord]:
        out: List[ExecutionRecord] = []
        for recs in self._records.values():
            out.extend(recs)
        return out

    # ──────────────────────────────────────────────────────
    #  INTERNAL — scoring helpers
    # ──────────────────────────────────────────────────────

    def _compute_derived(self, rec: ExecutionRecord) -> ExecutionRecord:
        """Fill in computed fields."""
        rec.entry_slippage = rec.realized_entry - rec.expected_entry
        rec.exit_slippage = rec.expected_exit - rec.realized_exit
        rec.total_execution_cost = abs(rec.entry_slippage) + abs(rec.exit_slippage) + rec.spread_paid
        if rec.intended_size > 0 and rec.expected_entry > 0:
            direction = 1.0  # assume long for raw pnl sign
            rec.raw_pnl = (rec.expected_exit - rec.expected_entry) * rec.intended_size
            rec.realized_pnl = (rec.realized_exit - rec.realized_entry) * rec.filled_size
        return rec

    @staticmethod
    def _linear_score(value: float, excellent: float, bad: float) -> float:
        """Linear interpolation: excellent→1.0, bad→0.0, clamped."""
        if bad <= excellent:
            return 1.0
        score = 1.0 - (value - excellent) / (bad - excellent)
        return max(0.0, min(1.0, score))

    def _slip_score(self, rel_slip: float) -> float:
        return self._linear_score(rel_slip, self.SLIP_EXCELLENT, self.SLIP_BAD)

    def _spread_score(self, rel_spread: float) -> float:
        return self._linear_score(rel_spread, self.SPREAD_EXCELLENT, self.SPREAD_BAD)

    def _latency_score(self, ms: float) -> float:
        return self._linear_score(ms, self.LATENCY_EXCELLENT_MS, self.LATENCY_BAD_MS)

    @staticmethod
    def _fill_score(rec: ExecutionRecord) -> float:
        if rec.fill_status == FillStatus.FULL:
            return 1.0
        if rec.fill_status == FillStatus.PARTIAL:
            ratio = rec.filled_size / max(rec.intended_size, 1e-12)
            return max(0.2, ratio)
        if rec.fill_status == FillStatus.MISSED:
            return 0.0
        # REJECTED
        return 0.0

    @staticmethod
    def _std(vals: List[float]) -> float:
        if len(vals) < 2:
            return 0.0
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        return math.sqrt(var)
