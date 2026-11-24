"""
Monte Carlo Simulator — Robustness Testing
═══════════════════════════════════════════
Permute trades, vary slippage/fees, shift timing to estimate
confidence intervals on strategy metrics.

Methods:
  1. Trade permutation (shuffle trade order → drawdown distribution)
  2. Parameter perturbation (vary slippage/fees ±50%)
  3. Skip-N analysis (remove N random trades → stability)
  4. Confidence intervals on key metrics (95th percentile worst case)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

from smc_futures.backtest.engine import RealisticTrade


# ───────────────────────────────────────────
#  Monte Carlo Config
# ───────────────────────────────────────────

@dataclass
class MonteCarloConfig:
    """Monte Carlo simulation parameters."""
    n_permutations: int = 2000         # Trade order permutations
    n_param_variations: int = 500      # Slippage/fee parameter variations
    n_skip_trials: int = 500           # Skip-N resamplings
    skip_pct: float = 0.10             # Remove 10% of trades
    slippage_vary_range: float = 0.50  # ±50% variation in slippage
    fee_vary_range: float = 0.30       # ±30% variation in fees
    confidence_level: float = 0.95     # 95% confidence
    seed: int = 42


# ───────────────────────────────────────────
#  Monte Carlo Result
# ───────────────────────────────────────────

@dataclass
class MonteCarloResult:
    """Aggregated Monte Carlo analysis."""
    n_base_trades: int = 0

    # Permutation analysis (drawdown distribution)
    perm_max_dd_mean: float = 0.0
    perm_max_dd_median: float = 0.0
    perm_max_dd_95th: float = 0.0      # 95th percentile worst DD
    perm_max_dd_99th: float = 0.0

    perm_final_pnl_mean: float = 0.0
    perm_final_pnl_5th: float = 0.0    # 5th percentile (worst case)
    perm_final_pnl_median: float = 0.0

    # Profit factor distribution
    perm_pf_mean: float = 0.0
    perm_pf_5th: float = 0.0
    perm_pf_median: float = 0.0

    # Parameter variation analysis
    param_pnl_mean: float = 0.0
    param_pnl_5th: float = 0.0
    param_pnl_median: float = 0.0
    param_pf_mean: float = 0.0
    param_pf_5th: float = 0.0
    param_unprofitable_pct: float = 0.0  # % of variations unprofitable

    # Skip-N analysis (stability)
    skip_pnl_mean: float = 0.0
    skip_pnl_5th: float = 0.0
    skip_pf_5th: float = 0.0
    skip_unprofitable_pct: float = 0.0

    # Overall robustness
    is_robust: bool = False
    robustness_score: float = 0.0      # 0-1 composite score
    warnings: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════
#  MONTE CARLO SIMULATOR
# ═══════════════════════════════════════════

class MonteCarloSimulator:
    """
    Run Monte Carlo simulations on a set of completed trades.

    Does NOT re-run the backtest engine. Instead, it permutes/varies
    the TRADE RESULTS to estimate distributional properties.
    """

    def __init__(self, config: MonteCarloConfig):
        self.config = config
        self._rng = random.Random(config.seed)

    def analyze(
        self,
        trades: List[RealisticTrade],
        initial_capital: float = 10_000.0,
    ) -> MonteCarloResult:
        """
        Full Monte Carlo analysis on completed trades.

        Args:
            trades: List of completed RealisticTrade from backtest
            initial_capital: Starting capital for equity curve simulation
        """
        result = MonteCarloResult(n_base_trades=len(trades))

        if len(trades) < 5:
            result.warnings.append(f"Too few trades ({len(trades)}) for Monte Carlo")
            return result

        pnls = [t.net_pnl for t in trades]

        # ── 1. Trade permutation analysis ────────
        perm_results = self._permutation_analysis(pnls, initial_capital)
        result.perm_max_dd_mean = perm_results["dd_mean"]
        result.perm_max_dd_median = perm_results["dd_median"]
        result.perm_max_dd_95th = perm_results["dd_95th"]
        result.perm_max_dd_99th = perm_results["dd_99th"]
        result.perm_final_pnl_mean = perm_results["pnl_mean"]
        result.perm_final_pnl_5th = perm_results["pnl_5th"]
        result.perm_final_pnl_median = perm_results["pnl_median"]
        result.perm_pf_mean = perm_results["pf_mean"]
        result.perm_pf_5th = perm_results["pf_5th"]
        result.perm_pf_median = perm_results["pf_median"]

        # ── 2. Parameter perturbation ────────────
        param_results = self._parameter_variation(trades, initial_capital)
        result.param_pnl_mean = param_results["pnl_mean"]
        result.param_pnl_5th = param_results["pnl_5th"]
        result.param_pnl_median = param_results["pnl_median"]
        result.param_pf_mean = param_results["pf_mean"]
        result.param_pf_5th = param_results["pf_5th"]
        result.param_unprofitable_pct = param_results["unprofitable_pct"]

        # ── 3. Skip-N analysis ───────────────────
        skip_results = self._skip_analysis(pnls, initial_capital)
        result.skip_pnl_mean = skip_results["pnl_mean"]
        result.skip_pnl_5th = skip_results["pnl_5th"]
        result.skip_pf_5th = skip_results["pf_5th"]
        result.skip_unprofitable_pct = skip_results["unprofitable_pct"]

        # ── 4. Robustness scoring ────────────────
        result.robustness_score, result.warnings = self._compute_robustness(result)
        result.is_robust = result.robustness_score >= 0.6

        return result

    # ── 1. Permutation analysis ──────────────

    def _permutation_analysis(
        self, pnls: List[float], initial_capital: float,
    ) -> dict:
        """Shuffle trade order N times, compute equity stats."""
        max_dds = []
        final_pnls = []
        pfs = []

        for _ in range(self.config.n_permutations):
            shuffled = pnls.copy()
            self._rng.shuffle(shuffled)

            equity = initial_capital
            peak = equity
            max_dd = 0.0
            gross_profit = 0.0
            gross_loss = 0.0

            for pnl in shuffled:
                equity += pnl
                peak = max(peak, equity)
                dd_pct = (peak - equity) / peak * 100 if peak > 0 else 0
                max_dd = max(max_dd, dd_pct)
                if pnl > 0:
                    gross_profit += pnl
                elif pnl < 0:
                    gross_loss += abs(pnl)

            max_dds.append(max_dd)
            final_pnls.append(equity - initial_capital)
            pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)
            pfs.append(pf)

        return {
            "dd_mean": _mean(max_dds),
            "dd_median": _percentile(max_dds, 50),
            "dd_95th": _percentile(max_dds, 95),
            "dd_99th": _percentile(max_dds, 99),
            "pnl_mean": _mean(final_pnls),
            "pnl_5th": _percentile(final_pnls, 5),
            "pnl_median": _percentile(final_pnls, 50),
            "pf_mean": _mean([p for p in pfs if p < 100]),  # Filter infinity
            "pf_5th": _percentile([p for p in pfs if p < 100], 5),
            "pf_median": _percentile([p for p in pfs if p < 100], 50),
        }

    # ── 2. Parameter variation ───────────────

    def _parameter_variation(
        self, trades: List[RealisticTrade], initial_capital: float,
    ) -> dict:
        """Vary slippage and fees to test parameter sensitivity."""
        final_pnls = []
        pfs = []

        for _ in range(self.config.n_param_variations):
            slip_mult = 1.0 + self._rng.uniform(
                -self.config.slippage_vary_range,
                self.config.slippage_vary_range,
            )
            fee_mult = 1.0 + self._rng.uniform(
                -self.config.fee_vary_range,
                self.config.fee_vary_range,
            )

            total_pnl = 0.0
            gross_profit = 0.0
            gross_loss = 0.0

            for t in trades:
                # Recompute PnL with varied parameters
                original_slip_cost = abs(t.actual_entry - t.intended_entry) * t.qty
                original_exit_slip = abs(t.actual_exit - t.intended_exit) * t.qty if t.intended_exit else 0

                extra_slip = original_slip_cost * (slip_mult - 1)
                extra_fee = t.total_fees * (fee_mult - 1)

                adjusted_pnl = t.net_pnl - extra_slip - extra_fee
                total_pnl += adjusted_pnl

                if adjusted_pnl > 0:
                    gross_profit += adjusted_pnl
                elif adjusted_pnl < 0:
                    gross_loss += abs(adjusted_pnl)

            pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)
            final_pnls.append(total_pnl)
            pfs.append(pf if pf < 100 else 100)

        unprofitable = sum(1 for p in final_pnls if p <= 0) / len(final_pnls) * 100 if final_pnls else 100

        return {
            "pnl_mean": _mean(final_pnls),
            "pnl_5th": _percentile(final_pnls, 5),
            "pnl_median": _percentile(final_pnls, 50),
            "pf_mean": _mean(pfs),
            "pf_5th": _percentile(pfs, 5),
            "unprofitable_pct": unprofitable,
        }

    # ── 3. Skip-N analysis ───────────────────

    def _skip_analysis(
        self, pnls: List[float], initial_capital: float,
    ) -> dict:
        """Remove random subset of trades to test reliance on outliers."""
        n_skip = max(1, int(len(pnls) * self.config.skip_pct))
        final_pnls = []
        pfs = []

        for _ in range(self.config.n_skip_trials):
            indices = list(range(len(pnls)))
            skip_indices = set(self._rng.sample(indices, min(n_skip, len(indices))))
            remaining = [pnls[i] for i in range(len(pnls)) if i not in skip_indices]

            total = sum(remaining)
            gp = sum(p for p in remaining if p > 0)
            gl = abs(sum(p for p in remaining if p < 0))
            pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)

            final_pnls.append(total)
            pfs.append(pf if pf < 100 else 100)

        unprofitable = sum(1 for p in final_pnls if p <= 0) / len(final_pnls) * 100 if final_pnls else 100

        return {
            "pnl_mean": _mean(final_pnls),
            "pnl_5th": _percentile(final_pnls, 5),
            "pf_5th": _percentile([p for p in pfs if p < 100], 5) if pfs else 0,
            "unprofitable_pct": unprofitable,
        }

    # ── 4. Robustness scoring ────────────────

    @staticmethod
    def _compute_robustness(result: MonteCarloResult) -> tuple[float, list[str]]:
        """Compute composite robustness score (0-1) with warnings."""
        score = 1.0
        warnings = []

        # 95th pctile drawdown check
        if result.perm_max_dd_95th > 30:
            penalty = min((result.perm_max_dd_95th - 30) / 30, 0.3)
            score -= penalty
            warnings.append(f"95th DD = {result.perm_max_dd_95th:.1f}% (risk of >30% drawdown)")

        # 5th percentile PnL check (worst-case profitability)
        if result.perm_final_pnl_5th <= 0:
            score -= 0.25
            warnings.append(f"5th pctile PnL = ${result.perm_final_pnl_5th:.0f} (can be negative)")

        # Parameter sensitivity
        if result.param_unprofitable_pct > 30:
            penalty = min(result.param_unprofitable_pct / 100, 0.3)
            score -= penalty
            warnings.append(
                f"Unprofitable in {result.param_unprofitable_pct:.0f}% of param variations"
            )

        # 5th percentile PF
        if result.perm_pf_5th < 1.0:
            score -= 0.15
            warnings.append(f"5th pctile PF = {result.perm_pf_5th:.2f} (below breakeven)")

        # Skip-N stability
        if result.skip_unprofitable_pct > 20:
            score -= 0.15
            warnings.append(
                f"Unprofitable in {result.skip_unprofitable_pct:.0f}% of skip-N trials (outlier dependent)"
            )

        return max(0.0, min(1.0, score)), warnings


# ───────────────────────────────────────────
#  Statistics helpers (no numpy dependency)
# ───────────────────────────────────────────

def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = (pct / 100) * (len(s) - 1)
    low = int(math.floor(idx))
    high = int(math.ceil(idx))
    if low == high:
        return s[low]
    frac = idx - low
    return s[low] * (1 - frac) + s[high] * frac
