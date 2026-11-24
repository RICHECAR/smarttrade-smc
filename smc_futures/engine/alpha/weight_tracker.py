"""
StrategyWeightTracker — Adaptive Performance Weighting
═══════════════════════════════════════════════════════
Maintains rolling-window performance stats per strategy and produces
adaptive weights that feed into the StrategyScorer and Capital Allocator.

Pulls data from PerformanceGovernor's per-(strategy, regime) counters.
Recomputes weights on demand (lazy, sub-millisecond).

Weight formula:
  raw_weight = (pf_component * w_pf) + (wr_component * w_wr) + (rr_component * w_rr)
  weight     = clamp(raw_weight, min_weight, max_weight)

Where:
  pf_component = min(PF / pf_cap, 1.0)
  wr_component = winrate (0..1)
  rr_component = min(avg_rr / rr_cap, 1.0)
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StrategyStats:
    """Rolling performance snapshot for one strategy."""
    strategy_name: str
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    last_updated: float = 0.0

    @property
    def winrate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return self.gross_win if self.gross_win > 0 else 0.0
        return self.gross_win / self.gross_loss


class StrategyWeightTracker:
    """
    Computes adaptive weights per strategy from PerformanceGovernor data.

    Usage:
        tracker = StrategyWeightTracker(governor_ref=governor)
        weights = tracker.get_weights()        # {"DefaultSMC": 1.23, "AsiaWeekend": 0.87, ...}
        stats   = tracker.get_all_stats()       # {name: StrategyStats}
    """

    DEFAULT_STRATEGIES = [
        "DefaultSMC", "AsiaWeekend", "FundingReversion",
        "VWAPReversion", "SweepScalp", "LiqCascade", "OBImbalance",
    ]

    def __init__(
        self,
        governor_ref=None,
        w_pf: float = 0.50,
        w_wr: float = 0.30,
        w_rr: float = 0.20,
        pf_cap: float = 3.0,
        rr_cap: float = 3.0,
        min_weight: float = 0.3,
        max_weight: float = 2.0,
        min_trades_for_weight: int = 5,
        default_weight: float = 1.0,
    ):
        """
        Args:
            governor_ref: Reference to PerformanceGovernor singleton
            w_pf: Weight for profit factor component
            w_wr: Weight for winrate component
            w_rr: Weight for avg R:R component (reserved for future use)
            pf_cap: PF values above this → 1.0 in component
            rr_cap: Avg R:R values above this → 1.0
            min_weight: Floor — never go below this
            max_weight: Ceiling — never exceed this
            min_trades_for_weight: Minimum trades before adapting (else default_weight)
            default_weight: Weight for strategies with insufficient data
        """
        total = w_pf + w_wr + w_rr
        self.w_pf = w_pf / total
        self.w_wr = w_wr / total
        self.w_rr = w_rr / total
        self.pf_cap = pf_cap
        self.rr_cap = rr_cap
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.min_trades = min_trades_for_weight
        self.default_weight = default_weight
        self._governor = governor_ref
        self._cache: Dict[str, float] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 5.0  # seconds

    def get_weights(self) -> Dict[str, float]:
        """
        Return {strategy_name: weight} for all known strategies.
        Cached for _cache_ttl seconds to avoid re-computing on every signal.
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return dict(self._cache)

        stats = self._gather_stats()
        weights: Dict[str, float] = {}

        for name in self.DEFAULT_STRATEGIES:
            s = stats.get(name)
            if s is None or s.trades < self.min_trades:
                weights[name] = self.default_weight
                continue

            pf_comp = min(s.profit_factor / self.pf_cap, 1.0)
            wr_comp = s.winrate
            # rr_comp is 0.5 placeholder (governor doesn't track per-trade RR yet)
            rr_comp = 0.5

            raw = (
                self.w_pf * pf_comp
                + self.w_wr * wr_comp
                + self.w_rr * rr_comp
            )
            # Scale to [0..2] range then clamp
            scaled = raw * 2.0
            weight = max(self.min_weight, min(scaled, self.max_weight))
            weights[name] = round(weight, 3)

        self._cache = weights
        self._cache_ts = now

        logger.debug(
            f"[WEIGHT_TRACKER] Updated weights: "
            + " | ".join(f"{k}={v:.2f}" for k, v in weights.items())
        )
        return dict(weights)

    def get_weight(self, strategy_name: str) -> float:
        """Get weight for a single strategy."""
        weights = self.get_weights()
        return weights.get(strategy_name, self.default_weight)

    def get_all_stats(self) -> Dict[str, StrategyStats]:
        """Return current StrategyStats for all strategies."""
        return self._gather_stats()

    def _gather_stats(self) -> Dict[str, StrategyStats]:
        """
        Pull per-strategy data from PerformanceGovernor.
        Aggregates across all regimes for each strategy.
        """
        if self._governor is None:
            return {}

        stats: Dict[str, StrategyStats] = {}

        gov = self._governor
        with gov._lock:
            # Aggregate across regimes
            for (strategy, regime), trades in gov._strategy_trades.items():
                if strategy not in stats:
                    stats[strategy] = StrategyStats(
                        strategy_name=strategy,
                        last_updated=time.time(),
                    )
                s = stats[strategy]
                s.trades += trades
                s.wins += gov._strategy_wins.get((strategy, regime), 0)
                pnl = gov._strategy_pnl.get((strategy, regime), 0.0)
                s.total_pnl += pnl
                if pnl >= 0:
                    s.gross_win += pnl
                else:
                    s.gross_loss += abs(pnl)

        return stats

    def force_refresh(self) -> None:
        """Invalidate cache, forcing next get_weights() to recompute."""
        self._cache_ts = 0.0
        self._cache = {}
