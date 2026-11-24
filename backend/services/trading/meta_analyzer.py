"""
Meta-Performance Analyzer — Multi-Dimensional Edge Discovery
═══════════════════════════════════════════════════════════

Slices trade journal data along multiple dimensions to identify
where the system has edge and where it bleeds.

Dimensions:
  - **Setup type** — direction × strength
  - **Market regime** — trending_up / ranging / high_vol …
  - **Symbol** — per-instrument stats
  - **Hour / session** — UTC hour buckets
  - **Execution style** — exit reason clusters (TP1, TP2, SL, trailing)

Consumers:
  - StrategyAdaptationEngine (adjusts params based on edge map)
  - AdaptivePolicy (blocks/boosts specific conditions)
  - Dashboard (human review)

No external dependencies — consumes JournalEntry objects from TradeJournalService.

Config via .env:
  META_MIN_SAMPLE_SIZE=5
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.services.trading.adaptive_models import DimensionStats, JournalEntry

logger = logging.getLogger("smarttrade.adaptive.meta")


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class MetaPerformanceAnalyzer:
    """
    Analyzes closed journal entries across multiple dimensions.

    Usage:
        analyzer = MetaPerformanceAnalyzer()
        by_regime = analyzer.analyze_by_regime(journal_entries)
        strongest = analyzer.strongest_conditions(journal_entries)
        weakest = analyzer.weakest_conditions(journal_entries)
    """

    def __init__(self) -> None:
        self._min_sample = _envi("META_MIN_SAMPLE_SIZE", 5)

    # ──────────────────────────────────────────────────
    #  PUBLIC ANALYZERS
    # ──────────────────────────────────────────────────

    def analyze_by_regime(
        self, entries: List[JournalEntry]
    ) -> List[DimensionStats]:
        """Performance breakdown by market regime at entry."""
        return self._group_and_compute(entries, "regime", lambda e: e.regime)

    def analyze_by_symbol(
        self, entries: List[JournalEntry]
    ) -> List[DimensionStats]:
        """Performance breakdown by trading symbol."""
        return self._group_and_compute(entries, "symbol", lambda e: e.symbol)

    def analyze_by_setup_type(
        self, entries: List[JournalEntry]
    ) -> List[DimensionStats]:
        """Performance breakdown by direction + strength combination."""
        return self._group_and_compute(
            entries,
            "setup_type",
            lambda e: f"{e.direction}_{e.signal_strength}",
        )

    def analyze_by_hour(
        self, entries: List[JournalEntry]
    ) -> List[DimensionStats]:
        """Performance breakdown by UTC hour of entry."""
        def hour_key(e: JournalEntry) -> str:
            if e.timestamp > 0:
                dt = datetime.fromtimestamp(e.timestamp, tz=timezone.utc)
                return f"{dt.hour:02d}:00"
            return "unknown"

        return self._group_and_compute(entries, "hour", hour_key)

    def analyze_by_session(
        self, entries: List[JournalEntry]
    ) -> List[DimensionStats]:
        """Performance by trading session (Asia / London / NY / Off-hours)."""
        def session_key(e: JournalEntry) -> str:
            if e.timestamp <= 0:
                return "unknown"
            h = datetime.fromtimestamp(e.timestamp, tz=timezone.utc).hour
            if 0 <= h < 8:
                return "asia"
            elif 8 <= h < 13:
                return "london"
            elif 13 <= h < 21:
                return "new_york"
            else:
                return "off_hours"

        return self._group_and_compute(entries, "session", session_key)

    def analyze_by_exit_reason(
        self, entries: List[JournalEntry]
    ) -> List[DimensionStats]:
        """Performance breakdown by exit reason cluster."""
        def exit_key(e: JournalEntry) -> str:
            reason = (e.exit_reason or "").lower()
            if "tp1" in reason:
                return "TP1"
            if "tp2" in reason:
                return "TP2"
            if "sl" in reason or "stop" in reason:
                return "SL"
            if "trail" in reason:
                return "trailing"
            if "kill" in reason or "emergency" in reason:
                return "emergency"
            if "block" in reason:
                return "blocked"
            return reason[:20] if reason else "unknown"

        return self._group_and_compute(entries, "exit_reason", exit_key)

    def analyze_by_policy_action(
        self, entries: List[JournalEntry]
    ) -> List[DimensionStats]:
        """Performance breakdown by adaptive policy action at entry."""
        return self._group_and_compute(
            entries, "policy_action", lambda e: e.policy_action or "unknown"
        )

    # ──────────────────────────────────────────────────
    #  EDGE DISCOVERY
    # ──────────────────────────────────────────────────

    def strongest_conditions(
        self,
        entries: List[JournalEntry],
        top_n: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Return the top-N conditions with the highest edge
        (profit factor × sample size weight).
        """
        all_stats = self._all_dimensions(entries)
        # Score = profit_factor × log(trades + 1) to reward edge + sample size
        import math
        scored = []
        for s in all_stats:
            if not s.sample_size_ok:
                continue
            edge_score = s.profit_factor * math.log(s.total_trades + 1)
            scored.append({"stats": s.to_dict(), "edge_score": round(edge_score, 3)})

        scored.sort(key=lambda x: x["edge_score"], reverse=True)
        return scored[:top_n]

    def weakest_conditions(
        self,
        entries: List[JournalEntry],
        top_n: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Return the top-N conditions with the worst performance
        (lowest win rate with sufficient sample).
        """
        all_stats = self._all_dimensions(entries)
        weak = [s for s in all_stats if s.sample_size_ok]
        weak.sort(key=lambda s: s.win_rate)
        return [{"stats": s.to_dict()} for s in weak[:top_n]]

    def full_report(self, entries: List[JournalEntry]) -> Dict[str, Any]:
        """Complete multi-dimensional analysis report."""
        return {
            "by_regime": [s.to_dict() for s in self.analyze_by_regime(entries)],
            "by_symbol": [s.to_dict() for s in self.analyze_by_symbol(entries)],
            "by_setup_type": [s.to_dict() for s in self.analyze_by_setup_type(entries)],
            "by_hour": [s.to_dict() for s in self.analyze_by_hour(entries)],
            "by_session": [s.to_dict() for s in self.analyze_by_session(entries)],
            "by_exit_reason": [s.to_dict() for s in self.analyze_by_exit_reason(entries)],
            "by_policy": [s.to_dict() for s in self.analyze_by_policy_action(entries)],
            "strongest": self.strongest_conditions(entries),
            "weakest": self.weakest_conditions(entries),
            "total_entries_analyzed": len(entries),
        }

    # ──────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────

    def _all_dimensions(self, entries: List[JournalEntry]) -> List[DimensionStats]:
        """Collect stats from all dimensions into a flat list."""
        results: List[DimensionStats] = []
        results.extend(self.analyze_by_regime(entries))
        results.extend(self.analyze_by_symbol(entries))
        results.extend(self.analyze_by_setup_type(entries))
        results.extend(self.analyze_by_hour(entries))
        results.extend(self.analyze_by_session(entries))
        results.extend(self.analyze_by_exit_reason(entries))
        return results

    def _group_and_compute(
        self,
        entries: List[JournalEntry],
        dimension: str,
        key_fn,
    ) -> List[DimensionStats]:
        """Group entries by key_fn, compute DimensionStats for each group."""
        # Only consider closed entries with PnL data
        closed = [e for e in entries if e.closed]

        groups: Dict[str, List[JournalEntry]] = defaultdict(list)
        for e in closed:
            k = key_fn(e)
            groups[k].append(e)

        results: List[DimensionStats] = []
        for value, group in sorted(groups.items()):
            stats = self._compute_stats(dimension, value, group)
            results.append(stats)

        return results

    def _compute_stats(
        self, dimension: str, value: str, entries: List[JournalEntry]
    ) -> DimensionStats:
        """Compute performance stats for a group of journal entries."""
        total = len(entries)
        wins = [e for e in entries if e.pnl > 0]
        losses = [e for e in entries if e.pnl <= 0]
        win_count = len(wins)
        loss_count = len(losses)

        win_rate = win_count / total if total > 0 else 0.0
        net_pnl = sum(e.pnl for e in entries)
        avg_pnl = net_pnl / total if total > 0 else 0.0

        gross_profit = sum(e.pnl for e in wins)
        gross_loss = abs(sum(e.pnl for e in losses))
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0
            else (gross_profit if gross_profit > 0 else 0.0)
        )

        avg_hold = (
            sum(e.hold_seconds for e in entries) / total if total > 0 else 0.0
        )
        best = max((e.pnl for e in entries), default=0.0)
        worst = min((e.pnl for e in entries), default=0.0)

        return DimensionStats(
            dimension=dimension,
            value=value,
            total_trades=total,
            wins=win_count,
            losses=loss_count,
            win_rate=win_rate,
            net_pnl=net_pnl,
            avg_pnl=avg_pnl,
            profit_factor=profit_factor,
            avg_hold_seconds=avg_hold,
            best_trade=best,
            worst_trade=worst,
            sample_size_ok=total >= self._min_sample,
        )
