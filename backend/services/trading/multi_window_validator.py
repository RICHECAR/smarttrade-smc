"""
Multi-Window Validator — Cross-Timeframe Edge Consistency
════════════════════════════════════════════════════════

Evaluates each setup across three windows:

  short  (default 10 trades)
  medium (default 30 trades)
  long   (default 60 trades)

Then classifies the edge trend:

  PERSISTENT       — positive edge in all three windows
  DECAYING         — long-term positive but short-term dropping
  UNSTABLE         — high variance between windows
  REGIME_DEPENDENT — edge present only with specific regime context
  INSUFFICIENT     — not enough data for any window

Config via .env:
  MW_SHORT_WINDOW=10
  MW_MEDIUM_WINDOW=30
  MW_LONG_WINDOW=60
  MW_CONSISTENCY_THRESHOLD=0.30   (max allowed CV across windows)
  MW_DECAY_THRESHOLD=0.60         (short/long ratio below this = decaying)
"""

import logging
import math
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from backend.services.trading.robustness_models import (
    EdgeTrend,
    MultiWindowVerdict,
    WindowStats,
)

logger = logging.getLogger("smarttrade.robustness.multi_window")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class MultiWindowValidator:
    """
    Evaluates setup edge consistency across multiple time windows.

    Usage:
        validator = MultiWindowValidator()
        validator.record(key_str, pnl=42.0, regime="trending_up")
        verdict = validator.evaluate(key_str)
    """

    def __init__(self) -> None:
        self._short = _envi("MW_SHORT_WINDOW", 10)
        self._medium = _envi("MW_MEDIUM_WINDOW", 30)
        self._long = _envi("MW_LONG_WINDOW", 60)
        self._consistency_thr = _envf("MW_CONSISTENCY_THRESHOLD", 0.30)
        self._decay_thr = _envf("MW_DECAY_THRESHOLD", 0.60)
        self._max_buf = max(self._long * 2, 200)

        # Per-setup buffers
        self._pnls: Dict[str, Deque[float]] = {}
        self._regimes: Dict[str, Deque[str]] = {}

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def record(self, key: str, pnl: float, regime: str = "unknown") -> None:
        pnl_buf = self._pnls.setdefault(key, deque(maxlen=self._max_buf))
        reg_buf = self._regimes.setdefault(key, deque(maxlen=self._max_buf))
        pnl_buf.append(pnl)
        reg_buf.append(regime)

    def evaluate(self, key: str) -> MultiWindowVerdict:
        all_pnls = list(self._pnls.get(key, []))
        all_regimes = list(self._regimes.get(key, []))

        if len(all_pnls) < self._short:
            return MultiWindowVerdict(edge_trend=EdgeTrend.INSUFFICIENT)

        windows: List[WindowStats] = []
        for name, size in [("short", self._short), ("medium", self._medium), ("long", self._long)]:
            if len(all_pnls) >= size:
                w = self._compute_window(name, all_pnls[-size:], size)
                windows.append(w)

        if len(windows) < 2:
            return MultiWindowVerdict(
                edge_trend=EdgeTrend.INSUFFICIENT,
                windows=windows,
            )

        # Cross-window consistency
        wrs = [w.win_rate for w in windows]
        consistency = self._consistency(wrs)

        # Trend score: compare short vs long
        short_wr = windows[0].win_rate
        long_wr = windows[-1].win_rate
        if long_wr > 0:
            ratio = short_wr / long_wr
        else:
            ratio = 0.0 if short_wr <= 0 else 2.0
        trend_score = min(1.0, max(0.0, ratio / 2.0))

        # Regime dependency
        regime_dep = self._regime_dependency(all_pnls, all_regimes)

        # Classify
        edge_trend = self._classify(windows, consistency, ratio, regime_dep)

        return MultiWindowVerdict(
            edge_trend=edge_trend,
            windows=windows,
            trend_score=round(trend_score, 4),
            consistency=round(consistency, 4),
            regime_dependency=round(regime_dep, 4),
        )

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _compute_window(self, name: str, pnls: List[float], size: int) -> WindowStats:
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n if n > 0 else 0.0
        gp = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p <= 0))
        pf = gp / gl if gl > 0 else (gp if gp > 0 else 0.0)
        mean_pnl = sum(pnls) / n if n > 0 else 0.0
        # Simple Sharpe approximation
        if n > 1:
            var = sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1)
            std = math.sqrt(var)
            sharpe = mean_pnl / std if std > 0 else 0.0
        else:
            sharpe = 0.0

        return WindowStats(
            window_name=name,
            window_size=size,
            trade_count=n,
            win_rate=wr,
            profit_factor=pf,
            expectancy=mean_pnl,
            sharpe_approx=sharpe,
        )

    def _consistency(self, values: List[float]) -> float:
        """0–1 consistency from CV of values."""
        if len(values) < 2:
            return 0.5
        mean_v = sum(values) / len(values)
        if abs(mean_v) < 1e-9:
            return 0.5
        var = sum((v - mean_v) ** 2 for v in values) / len(values)
        cv = math.sqrt(var) / abs(mean_v)
        return max(0.0, min(1.0, 1.0 - min(cv, 2.0) / 2.0))

    def _regime_dependency(self, pnls: List[float], regimes: List[str]) -> float:
        """
        Measure how much edge depends on regime.
        0=regime-independent, 1=fully regime-dependent.
        """
        if len(pnls) < 10 or len(set(regimes)) <= 1:
            return 0.0

        # Per-regime expectancy
        regime_exp: Dict[str, List[float]] = {}
        for pnl, reg in zip(pnls, regimes):
            regime_exp.setdefault(reg, []).append(pnl)

        means = []
        for trades in regime_exp.values():
            if len(trades) >= 3:
                means.append(sum(trades) / len(trades))

        if len(means) < 2:
            return 0.0

        global_mean = sum(pnls) / len(pnls)
        # Variance of per-regime means relative to global mean
        regime_var = sum((m - global_mean) ** 2 for m in means) / len(means)
        total_var = sum((p - global_mean) ** 2 for p in pnls) / len(pnls)

        if total_var < 1e-9:
            return 0.0

        # Ratio of between-regime variance to total variance
        dep = min(1.0, regime_var / total_var)
        return dep

    def _classify(
        self,
        windows: List[WindowStats],
        consistency: float,
        short_long_ratio: float,
        regime_dep: float,
    ) -> EdgeTrend:
        # All windows profitable?
        all_positive = all(w.expectancy > 0 for w in windows)

        if consistency < (1.0 - self._consistency_thr):
            # Low consistency
            if regime_dep > 0.5:
                return EdgeTrend.REGIME_DEPENDENT
            return EdgeTrend.UNSTABLE

        if all_positive and short_long_ratio >= self._decay_thr:
            return EdgeTrend.PERSISTENT

        if short_long_ratio < self._decay_thr:
            return EdgeTrend.DECAYING

        if regime_dep > 0.5:
            return EdgeTrend.REGIME_DEPENDENT

        return EdgeTrend.UNSTABLE
