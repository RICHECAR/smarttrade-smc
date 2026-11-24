"""
Fill Reliability Monitor — Track fill quality and detect instability
═══════════════════════════════════════════════════════════════════

Monitors:
  • Partial fill rate
  • Miss / reject rate
  • Latency distribution (avg, p95)

Triggers reliability level: HEALTHY → WARNING → DEGRADED → BLOCKED.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import math
from typing import List

from backend.services.trading.execution_models import (
    ExecutionRecord,
    FillStatus,
    ReliabilityLevel,
    ReliabilityReport,
)


class FillReliabilityMonitor:
    """Monitor fill reliability per setup or per symbol."""

    # Thresholds (rates)
    PARTIAL_WARN = 0.10       # 10 % partial fills → warning
    PARTIAL_DEGRADE = 0.25    # 25 % → degraded
    FAILURE_WARN = 0.05       # 5 % missed/rejected → warning
    FAILURE_DEGRADE = 0.15    # 15 % → degraded
    FAILURE_BLOCK = 0.30      # 30 % → blocked
    LATENCY_WARN_MS = 500.0
    LATENCY_DEGRADE_MS = 2000.0

    def __init__(self) -> None:
        pass

    # ──────────────────────────────────────────────────────
    #  PUBLIC
    # ──────────────────────────────────────────────────────

    def evaluate(
        self,
        key: str,
        records: List[ExecutionRecord],
    ) -> ReliabilityReport:
        """Evaluate fill reliability for a set of execution records."""
        n = len(records)
        if n == 0:
            return ReliabilityReport(key=key)

        full = sum(1 for r in records if r.fill_status == FillStatus.FULL)
        partial = sum(1 for r in records if r.fill_status == FillStatus.PARTIAL)
        missed = sum(1 for r in records if r.fill_status == FillStatus.MISSED)
        rejected = sum(1 for r in records if r.fill_status == FillStatus.REJECTED)

        partial_rate = partial / n
        failure_rate = (missed + rejected) / n

        latencies = [r.order_latency_ms for r in records]
        avg_lat = sum(latencies) / n
        p95_lat = self._percentile(latencies, 0.95)

        # Determine level — worst wins
        level = ReliabilityLevel.HEALTHY

        # Check failure rate
        if failure_rate >= self.FAILURE_BLOCK:
            level = ReliabilityLevel.BLOCKED
        elif failure_rate >= self.FAILURE_DEGRADE:
            level = self._worst(level, ReliabilityLevel.DEGRADED)
        elif failure_rate >= self.FAILURE_WARN:
            level = self._worst(level, ReliabilityLevel.WARNING)

        # Check partial rate
        if partial_rate >= self.PARTIAL_DEGRADE:
            level = self._worst(level, ReliabilityLevel.DEGRADED)
        elif partial_rate >= self.PARTIAL_WARN:
            level = self._worst(level, ReliabilityLevel.WARNING)

        # Check latency
        if p95_lat >= self.LATENCY_DEGRADE_MS:
            level = self._worst(level, ReliabilityLevel.DEGRADED)
        elif p95_lat >= self.LATENCY_WARN_MS:
            level = self._worst(level, ReliabilityLevel.WARNING)

        return ReliabilityReport(
            key=key,
            level=level,
            total_orders=n,
            full_fills=full,
            partial_fills=partial,
            missed_fills=missed,
            rejected_orders=rejected,
            avg_latency_ms=avg_lat,
            partial_rate=partial_rate,
            failure_rate=failure_rate,
            latency_p95=p95_lat,
        )

    # ──────────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────────

    _LEVEL_ORDER = {
        ReliabilityLevel.HEALTHY: 0,
        ReliabilityLevel.WARNING: 1,
        ReliabilityLevel.DEGRADED: 2,
        ReliabilityLevel.BLOCKED: 3,
    }

    def _worst(self, a: ReliabilityLevel, b: ReliabilityLevel) -> ReliabilityLevel:
        if self._LEVEL_ORDER[b] > self._LEVEL_ORDER[a]:
            return b
        return a

    @staticmethod
    def _percentile(values: List[float], pct: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        idx = pct * (len(s) - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return s[lo]
        frac = idx - lo
        return s[lo] * (1 - frac) + s[hi] * frac
