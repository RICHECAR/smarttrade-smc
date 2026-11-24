"""
Edge Fragility Analyzer — Detect Brittle Setups
═══════════════════════════════════════════════

Penalizes setups whose edge is fragile:

  1. Variance penalty        (w=0.30) — high PnL variance → fragile
  2. Drawdown instability    (w=0.20) — DD contribution swings
  3. Win concentration       (w=0.25) — profit dependent on few big wins
  4. Regime inconsistency    (w=0.25) — edge only in narrow regimes

Output: FragilityReport with score 0–1 (0=robust, 1=extremely fragile)

Config via .env:
  FRAG_MAX_CV=2.0           (CV above this → maximum variance penalty)
  FRAG_WIN_CONC_TOP_PCT=0.2 (top 20% of wins — if they hold >60% of profit → concentrated)
"""

import logging
import math
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from backend.services.trading.robustness_models import FragilityReport

logger = logging.getLogger("smarttrade.robustness.fragility")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class EdgeFragilityAnalyzer:
    """
    Analyzes how fragile a setup's edge is.

    Usage:
        analyzer = EdgeFragilityAnalyzer()
        analyzer.record(key_str, pnl=42.0, regime="trending_up")
        report = analyzer.evaluate(key_str)
    """

    W_VARIANCE = 0.30
    W_DD_INSTAB = 0.20
    W_WIN_CONC = 0.25
    W_REGIME_INC = 0.25

    def __init__(self) -> None:
        self._max_cv = _envf("FRAG_MAX_CV", 2.0)
        self._top_pct = _envf("FRAG_WIN_CONC_TOP_PCT", 0.20)
        self._max_buf = 200

        self._pnls: Dict[str, Deque[float]] = {}
        self._regimes: Dict[str, Deque[str]] = {}
        self._dd_contribs: Dict[str, Deque[float]] = {}

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def record(
        self,
        key: str,
        pnl: float,
        regime: str = "unknown",
        drawdown_contribution: float = 0.0,
    ) -> None:
        self._pnls.setdefault(key, deque(maxlen=self._max_buf)).append(pnl)
        self._regimes.setdefault(key, deque(maxlen=self._max_buf)).append(regime)
        self._dd_contribs.setdefault(key, deque(maxlen=self._max_buf)).append(
            drawdown_contribution
        )

    def evaluate(self, key: str) -> FragilityReport:
        pnls = list(self._pnls.get(key, []))
        regimes = list(self._regimes.get(key, []))
        dd_c = list(self._dd_contribs.get(key, []))

        if len(pnls) < 5:
            return FragilityReport(fragility_score=0.5, components={"data": 0.0})

        components: Dict[str, float] = {}

        # 1. Variance penalty
        var_pen = self._variance_penalty(pnls)
        components["variance"] = var_pen

        # 2. Drawdown instability
        dd_inst = self._dd_instability(dd_c)
        components["dd_instability"] = dd_inst

        # 3. Win concentration
        win_conc = self._win_concentration(pnls)
        components["win_concentration"] = win_conc

        # 4. Regime inconsistency
        reg_inc = self._regime_inconsistency(pnls, regimes)
        components["regime_inconsistency"] = reg_inc

        score = (
            self.W_VARIANCE * var_pen
            + self.W_DD_INSTAB * dd_inst
            + self.W_WIN_CONC * win_conc
            + self.W_REGIME_INC * reg_inc
        )
        score = round(max(0.0, min(1.0, score)), 4)

        return FragilityReport(
            fragility_score=score,
            variance_penalty=round(var_pen, 4),
            drawdown_instability=round(dd_inst, 4),
            win_concentration=round(win_conc, 4),
            regime_inconsistency=round(reg_inc, 4),
            components=components,
        )

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _variance_penalty(self, pnls: List[float]) -> float:
        """CV of PnL → 0=low variance, 1=high variance."""
        n = len(pnls)
        if n < 2:
            return 0.5
        mean = sum(pnls) / n
        if abs(mean) < 1e-9:
            return 0.5
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        cv = math.sqrt(var) / abs(mean)
        return min(1.0, cv / self._max_cv)

    def _dd_instability(self, dd_contribs: List[float]) -> float:
        """Instability of drawdown contribution over time."""
        if len(dd_contribs) < 5:
            return 0.3
        mean_dd = sum(dd_contribs) / len(dd_contribs)
        if mean_dd < 1e-9:
            return 0.0  # no drawdown → stable
        var = sum((d - mean_dd) ** 2 for d in dd_contribs) / len(dd_contribs)
        cv = math.sqrt(var) / mean_dd if mean_dd > 0 else 0
        return min(1.0, cv / 1.5)

    def _win_concentration(self, pnls: List[float]) -> float:
        """
        Measures if profit is concentrated in a few large wins.
        Returns 0=distributed, 1=concentrated in top N%.
        """
        wins = sorted([p for p in pnls if p > 0], reverse=True)
        if len(wins) < 3:
            return 0.5  # inconclusive

        total_profit = sum(wins)
        if total_profit <= 0:
            return 0.5

        top_n = max(1, int(len(wins) * self._top_pct))
        top_profit = sum(wins[:top_n])
        concentration = top_profit / total_profit

        # If top 20% of wins hold >60% of profit → fragile
        # Scale: 0.2→0, 0.6→0.5, 1.0→1.0
        return max(0.0, min(1.0, (concentration - 0.2) / 0.8))

    def _regime_inconsistency(self, pnls: List[float], regimes: List[str]) -> float:
        """
        How inconsistent is the edge across regimes?
        0=consistent everywhere, 1=only works in one regime.
        """
        if len(pnls) < 10 or len(set(regimes)) <= 1:
            return 0.0

        regime_exp: Dict[str, List[float]] = {}
        for pnl, reg in zip(pnls, regimes):
            regime_exp.setdefault(reg, []).append(pnl)

        # Need at least 2 regimes with enough data
        valid_regimes = {
            r: trades for r, trades in regime_exp.items() if len(trades) >= 3
        }
        if len(valid_regimes) < 2:
            return 0.0

        means = [sum(t) / len(t) for t in valid_regimes.values()]
        positive = sum(1 for m in means if m > 0)
        negative = sum(1 for m in means if m <= 0)

        # If some regimes positive and some negative → inconsistent
        if positive > 0 and negative > 0:
            return negative / (positive + negative)

        # All same sign → check variance
        mean_of_means = sum(means) / len(means)
        if abs(mean_of_means) < 1e-9:
            return 0.5
        var = sum((m - mean_of_means) ** 2 for m in means) / len(means)
        cv = math.sqrt(var) / abs(mean_of_means)
        return min(1.0, cv / 2.0)
