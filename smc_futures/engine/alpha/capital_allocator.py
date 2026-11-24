"""
StrategyCapitalAllocator — Per-Strategy Dynamic Capital Budgeting
═════════════════════════════════════════════════════════════════
Allocates a percentage of total risk capital to each strategy based on:
  1. Base allocation weights (configurable)
  2. Adaptive performance adjustments (from StrategyWeightTracker)
  3. Drawdown protection (reduce allocation for underperformers)

This module works ALONGSIDE the existing CapitalAllocator (regime-based).
The flow is:  regime_allocator → strategy_allocator → final position size.

Example:
  regime says 1.0x of base risk → $1000 available
  strategy_allocator says "DefaultSMC gets 25%" → $250 budget for this signal
  position_sizer converts $250 → 0.003 BTC at current price
"""

import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── Default allocation weights (sum to 1.0) ──────────────────────
_DEFAULT_ALLOCATIONS: Dict[str, float] = {
    "DefaultSMC":       0.25,
    "AsiaWeekend":      0.10,
    "FundingReversion": 0.10,
    "VWAPReversion":    0.15,
    "SweepScalp":       0.10,
    "LiqCascade":       0.15,
    "OBImbalance":      0.15,
}


class StrategyCapitalAllocator:
    """
    Computes per-strategy capital fractions.

    Usage:
        alloc = StrategyCapitalAllocator(weight_tracker=tracker)
        frac  = alloc.get_fraction("DefaultSMC")   # 0.25
        cap   = alloc.get_capital("DefaultSMC", available_usd=1000)  # 250.0
    """

    def __init__(
        self,
        weight_tracker=None,
        base_allocations: Optional[Dict[str, float]] = None,
        min_pf_for_full: float = 1.0,
        penalty_below_pf: float = 0.5,
        min_allocation: float = 0.05,
        max_allocation: float = 0.40,
    ):
        """
        Args:
            weight_tracker: StrategyWeightTracker instance for adaptive scaling
            base_allocations: {strategy: fraction}, must sum ≤ 1.0
            min_pf_for_full: PF below this triggers penalty
            penalty_below_pf: Multiplier applied when PF < min_pf
            min_allocation: Floor fraction for any active strategy
            max_allocation: Ceiling fraction for any single strategy
        """
        self._tracker = weight_tracker
        self._base = dict(base_allocations or _DEFAULT_ALLOCATIONS)
        self.min_pf_full = min_pf_for_full
        self.penalty_factor = penalty_below_pf
        self.min_alloc = min_allocation
        self.max_alloc = max_allocation

        # Normalize base allocations to sum = 1.0
        total = sum(self._base.values())
        if total > 0 and abs(total - 1.0) > 0.01:
            self._base = {k: v / total for k, v in self._base.items()}

        self._cache: Dict[str, float] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 10.0  # recompute every 10s

    def get_fraction(self, strategy_name: str) -> float:
        """
        Get the current capital fraction for a strategy (0..1).
        Combines base allocation with adaptive performance adjustments.
        """
        fracs = self._compute_all()
        return fracs.get(strategy_name, self.min_alloc)

    def get_capital(self, strategy_name: str, available_usd: float) -> float:
        """Get dollar capital budget for a strategy."""
        return available_usd * self.get_fraction(strategy_name)

    def get_all_fractions(self) -> Dict[str, float]:
        """Return all strategy fractions {name: fraction}."""
        return dict(self._compute_all())

    def _compute_all(self) -> Dict[str, float]:
        """Recompute adaptive allocations (cached)."""
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        # Start from base allocations
        result: Dict[str, float] = dict(self._base)

        # Apply adaptive adjustments if tracker available
        if self._tracker is not None:
            stats = self._tracker.get_all_stats()
            weights = self._tracker.get_weights()

            for name in list(result.keys()):
                st = stats.get(name)

                # Not enough trades → keep base allocation
                if st is None or st.trades < self._tracker.min_trades:
                    continue

                # PF penalty: underperformers get reduced allocation
                if st.profit_factor < self.min_pf_full:
                    result[name] *= self.penalty_factor
                    logger.info(
                        f"[CAPITAL_ALLOC] {name} PF={st.profit_factor:.2f}"
                        f" < {self.min_pf_full} → penalty {self.penalty_factor}x"
                    )

                # Adaptive weight scaling: high-performers get more
                w = weights.get(name, 1.0)
                if w > 1.0:
                    # Scale up proportionally but bounded
                    boost = 1.0 + (w - 1.0) * 0.5  # dampen: 1.5x weight → 1.25x alloc
                    result[name] *= boost
                elif w < 0.8:
                    # Scale down for low-weight strategies
                    result[name] *= max(w, 0.5)

        # Clamp individual allocations
        for name in result:
            result[name] = max(self.min_alloc, min(result[name], self.max_alloc))

        # Re-normalize to sum ≤ 1.0
        total = sum(result.values())
        if total > 1.0:
            result = {k: v / total for k, v in result.items()}

        self._cache = result
        self._cache_ts = now

        logger.debug(
            f"[CAPITAL_ALLOC] Fractions: "
            + " | ".join(f"{k}={v:.1%}" for k, v in result.items())
        )
        return self._cache

    def force_refresh(self) -> None:
        """Invalidate cache."""
        self._cache_ts = 0.0
        self._cache = {}
