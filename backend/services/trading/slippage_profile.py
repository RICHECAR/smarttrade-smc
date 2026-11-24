"""
Slippage Profile Engine — Multi-dimensional slippage analysis
════════════════════════════════════════════════════════════

Builds slippage profiles per setup across:
  • symbol
  • regime
  • session hour (0–23 UTC)
  • volatility bucket (low / medium / high)

Detects setups that are profitable in theory but execution-weak.
Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from backend.services.trading.execution_models import (
    ExecutionRecord,
    SlippageBucket,
    SlippageProfile,
)


class SlippageProfileEngine:
    """Builds multi-dimensional slippage profiles for setups."""

    # Volatility buckets (relative to price, e.g. ATR / price)
    VOL_LOW = 0.005      # < 0.5 %
    VOL_MED = 0.015      # < 1.5 %

    def __init__(self) -> None:
        self._records: Dict[str, List[ExecutionRecord]] = defaultdict(list)

    # ──────────────────────────────────────────────────────
    #  PUBLIC
    # ──────────────────────────────────────────────────────

    def ingest(self, records: List[ExecutionRecord]) -> None:
        """Bulk-ingest records for profiling."""
        for r in records:
            self._records[r.setup_key].append(r)

    def build_profile(
        self,
        setup_key: str,
        raw_expectancy: float = 0.0,
    ) -> SlippageProfile:
        """Build a full slippage profile for one setup."""
        recs = self._records.get(setup_key, [])
        if not recs:
            return SlippageProfile(setup_key=setup_key)

        # Overall
        overall = self._aggregate("overall", "overall", recs, raw_expectancy)

        # By symbol
        by_sym: Dict[str, SlippageBucket] = {}
        sym_groups = self._group_by(recs, lambda r: r.symbol)
        for key, group in sym_groups.items():
            by_sym[key] = self._aggregate(key, "symbol", group, raw_expectancy)

        # By regime
        by_reg: Dict[str, SlippageBucket] = {}
        reg_groups = self._group_by(recs, lambda r: r.regime)
        for key, group in reg_groups.items():
            by_reg[key] = self._aggregate(key, "regime", group, raw_expectancy)

        # By hour
        by_hour: Dict[str, SlippageBucket] = {}
        hour_groups = self._group_by(recs, lambda r: f"hour_{r.session_hour}")
        for key, group in hour_groups.items():
            by_hour[key] = self._aggregate(key, "hour", group, raw_expectancy)

        # By volatility bucket
        by_vol: Dict[str, SlippageBucket] = {}
        vol_groups = self._group_by(recs, lambda r: self._vol_label(r))
        for key, group in vol_groups.items():
            by_vol[key] = self._aggregate(key, "volatility", group, raw_expectancy)

        kills_edge = (
            raw_expectancy > 0 and overall.avg_total_cost >= raw_expectancy
        )

        return SlippageProfile(
            setup_key=setup_key,
            by_symbol=by_sym,
            by_regime=by_reg,
            by_hour=by_hour,
            by_volatility=by_vol,
            overall=overall,
            execution_kills_edge=kills_edge,
        )

    def get_worst_buckets(
        self,
        profile: SlippageProfile,
        top_n: int = 3,
    ) -> List[SlippageBucket]:
        """Return the N worst-cost buckets across all dimensions."""
        all_buckets: List[SlippageBucket] = []
        for dim in (profile.by_symbol, profile.by_regime, profile.by_hour, profile.by_volatility):
            all_buckets.extend(dim.values())
        all_buckets.sort(key=lambda b: b.avg_total_cost, reverse=True)
        return all_buckets[:top_n]

    # ──────────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────────

    @staticmethod
    def _group_by(recs: List[ExecutionRecord], key_fn) -> Dict[str, List[ExecutionRecord]]:
        groups: Dict[str, List[ExecutionRecord]] = defaultdict(list)
        for r in recs:
            groups[key_fn(r)].append(r)
        return groups

    @staticmethod
    def _aggregate(
        bucket_key: str,
        dimension: str,
        recs: List[ExecutionRecord],
        raw_expectancy: float,
    ) -> SlippageBucket:
        n = len(recs)
        if n == 0:
            return SlippageBucket(bucket_key=bucket_key, dimension=dimension)

        avg_e = sum(abs(r.entry_slippage) for r in recs) / n
        avg_x = sum(abs(r.exit_slippage) for r in recs) / n
        avg_sp = sum(r.spread_paid for r in recs) / n
        avg_tc = sum(r.total_execution_cost for r in recs) / n
        max_slip = max(abs(r.entry_slippage) + abs(r.exit_slippage) for r in recs)

        cost_pct = avg_tc / raw_expectancy if raw_expectancy > 0 else 0.0

        return SlippageBucket(
            bucket_key=bucket_key,
            dimension=dimension,
            trade_count=n,
            avg_entry_slip=avg_e,
            avg_exit_slip=avg_x,
            avg_spread=avg_sp,
            avg_total_cost=avg_tc,
            max_slip=max_slip,
            cost_as_pct_of_edge=cost_pct,
        )

    def _vol_label(self, rec: ExecutionRecord) -> str:
        """Classify record into volatility bucket based on spread/price proxy."""
        price = rec.expected_entry if rec.expected_entry > 0 else 1.0
        vol_proxy = rec.spread_paid / price
        if vol_proxy < self.VOL_LOW:
            return "vol_low"
        elif vol_proxy < self.VOL_MED:
            return "vol_medium"
        else:
            return "vol_high"
