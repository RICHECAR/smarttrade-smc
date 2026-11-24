"""
Statistical Confidence Engine — Confidence Scoring for Setup Edge
═══════════════════════════════════════════════════════════════

Computes statistical confidence that a setup has a real edge,
using multiple signals:

  1. Sample-size penalty   (w=0.20) — more trades → more confidence
  2. Win-rate CI width     (w=0.25) — Wilson interval; narrow is better
  3. Expectancy stability  (w=0.20) — CV of rolling expectancy
  4. Profit-factor stab.   (w=0.15) — CV of rolling PF
  5. Rolling-window stab.  (w=0.20) — consistency across recent sub-windows

Output: ConfidenceVerdict with score 0–1 and recommendation.

Thresholds (env-configurable):
  CONF_MIN_SAMPLE=15
  CONF_PROMOTE_SCORE=0.65
  CONF_HOLD_SCORE=0.40
  CONF_DELAY_SCORE=0.25
  CONF_Z=1.96  (95 % CI)
"""

import logging
import math
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from backend.services.trading.robustness_models import (
    ConfidenceRecommendation,
    ConfidenceVerdict,
)

logger = logging.getLogger("smarttrade.robustness.confidence")


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


class StatisticalConfidenceEngine:
    """
    Scores each setup 0–1 for statistical confidence.

    Usage:
        engine = StatisticalConfidenceEngine()
        engine.record(key_str, pnl=42.0)   # feed outcomes
        verdict = engine.evaluate(key_str, win_rate=0.55, total_trades=30)
    """

    def __init__(self) -> None:
        self._min_sample = _envi("CONF_MIN_SAMPLE", 15)
        self._z = _envf("CONF_Z", 1.96)
        self._promote = _envf("CONF_PROMOTE_SCORE", 0.65)
        self._hold = _envf("CONF_HOLD_SCORE", 0.40)
        self._delay = _envf("CONF_DELAY_SCORE", 0.25)
        self._rolling_window = _envi("CONF_ROLLING_WINDOW", 50)
        self._sub_window = _envi("CONF_SUB_WINDOW", 10)

        # Per-setup rolling PnL streams
        self._pnls: Dict[str, Deque[float]] = {}

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def record(self, key: str, pnl: float) -> None:
        """Append a trade PnL to the rolling buffer."""
        buf = self._pnls.setdefault(key, deque(maxlen=self._rolling_window))
        buf.append(pnl)

    def evaluate(
        self,
        key: str,
        win_rate: float,
        total_trades: int,
        profit_factor: float = 1.0,
    ) -> ConfidenceVerdict:
        """
        Evaluate confidence for a setup.

        Args:
            key:            Setup key string
            win_rate:       Current rolling win-rate (from SetupProfile)
            total_trades:   Total trades seen
            profit_factor:  Current rolling PF
        """
        pnls = list(self._pnls.get(key, []))
        components: Dict[str, float] = {}

        # 1. Sample-size component — sigmoid ramp
        sample_score = self._sample_score(total_trades)
        components["sample_size"] = sample_score

        # 2. Win-rate CI — reward narrow CI *above breakeven*
        ci_lo, ci_hi = self._wilson_ci(win_rate, total_trades)
        ci_width = ci_hi - ci_lo
        narrowness = max(0.0, 1.0 - ci_width)
        # Penalise if CI upper bound stays below 0.50 (no realistic edge)
        edge_quality = max(0.0, min(1.0, ci_lo / 0.50)) if ci_lo > 0 else 0.0
        ci_score = narrowness * 0.4 + edge_quality * 0.6
        components["wr_ci"] = ci_score

        # 3. Expectancy stability — only valuable if expectancy is positive
        exp_stab_raw = self._sub_window_stability(pnls, metric="mean")
        exp_positive = 1.0 if self._mean(pnls) > 0 else 0.2
        exp_stab = exp_stab_raw * exp_positive
        components["expectancy_stability"] = exp_stab

        # 4. Profit-factor stability — only valuable if PF > 1
        pf_stab_raw = self._pf_stability(pnls)
        pf_quality = min(1.0, profit_factor / 2.0) if profit_factor > 1.0 else 0.2
        pf_stab = pf_stab_raw * pf_quality
        components["pf_stability"] = pf_stab

        # 5. Rolling window win-rate consistency
        win_stab_raw = self._sub_window_stability(pnls, metric="win_rate")
        wr_quality = min(1.0, win_rate / 0.50) if win_rate > 0.35 else 0.15
        win_stab = win_stab_raw * wr_quality
        components["window_stability"] = win_stab

        # Weighted composite
        score = (
            0.20 * sample_score
            + 0.25 * ci_score
            + 0.20 * exp_stab
            + 0.15 * pf_stab
            + 0.20 * win_stab
        )
        score = round(max(0.0, min(1.0, score)), 4)

        recommendation = self._classify(score, total_trades)

        return ConfidenceVerdict(
            confidence_score=score,
            recommendation=recommendation,
            sample_size=total_trades,
            wr_ci_lower=round(ci_lo, 4),
            wr_ci_upper=round(ci_hi, 4),
            expectancy_stability=round(exp_stab, 4),
            pf_stability=round(pf_stab, 4),
            window_stability=round(win_stab, 4),
            components=components,
        )

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _sample_score(self, n: int) -> float:
        """Sigmoid-like ramp: 0 at n=0, ~0.5 at min_sample, ~0.95 at 3×min."""
        if n <= 0:
            return 0.0
        k = 4.0 / max(1, self._min_sample)
        return 1.0 / (1.0 + math.exp(-k * (n - self._min_sample)))

    def _wilson_ci(self, p: float, n: int) -> tuple:
        """Wilson score interval for binomial proportion."""
        if n == 0:
            return (0.0, 1.0)
        z = self._z
        z2 = z * z
        denom = 1.0 + z2 / n
        centre = (p + z2 / (2 * n)) / denom
        spread = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n) / denom
        return (max(0.0, centre - spread), min(1.0, centre + spread))

    def _sub_window_stability(self, pnls: List[float], metric: str = "mean") -> float:
        """
        Split PnLs into sub-windows and measure consistency.
        Returns 0–1 (1 = perfectly consistent).
        """
        if len(pnls) < self._sub_window * 2:
            return 0.3  # prior: low confidence when sparse

        sub_vals: List[float] = []
        for i in range(0, len(pnls) - self._sub_window + 1, self._sub_window):
            window = pnls[i : i + self._sub_window]
            if metric == "mean":
                sub_vals.append(sum(window) / len(window))
            elif metric == "win_rate":
                sub_vals.append(sum(1 for x in window if x > 0) / len(window))

        if len(sub_vals) < 2:
            return 0.3

        mean_v = sum(sub_vals) / len(sub_vals)
        if abs(mean_v) < 1e-9:
            return 0.5  # zero mean → neutral

        variance = sum((v - mean_v) ** 2 for v in sub_vals) / len(sub_vals)
        cv = math.sqrt(variance) / abs(mean_v) if abs(mean_v) > 1e-9 else 0.0
        # CV → stability: low CV = high stability
        return max(0.0, min(1.0, 1.0 - min(cv, 2.0) / 2.0))

    def _pf_stability(self, pnls: List[float]) -> float:
        """Compute stability of profit factor across sub-windows."""
        if len(pnls) < self._sub_window * 2:
            return 0.3

        pf_vals: List[float] = []
        for i in range(0, len(pnls) - self._sub_window + 1, self._sub_window):
            window = pnls[i : i + self._sub_window]
            gross_p = sum(x for x in window if x > 0)
            gross_l = abs(sum(x for x in window if x <= 0))
            pf = gross_p / gross_l if gross_l > 0 else (gross_p if gross_p > 0 else 0.0)
            pf_vals.append(pf)

        if len(pf_vals) < 2:
            return 0.3

        mean_pf = sum(pf_vals) / len(pf_vals)
        if mean_pf < 1e-9:
            return 0.1  # no profit → low stability

        variance = sum((v - mean_pf) ** 2 for v in pf_vals) / len(pf_vals)
        cv = math.sqrt(variance) / mean_pf if mean_pf > 1e-9 else 0.0
        return max(0.0, min(1.0, 1.0 - min(cv, 2.0) / 2.0))

    def _classify(self, score: float, n: int) -> ConfidenceRecommendation:
        if n < self._min_sample:
            return ConfidenceRecommendation.DELAY
        if score >= self._promote:
            return ConfidenceRecommendation.PROMOTE
        if score >= self._hold:
            return ConfidenceRecommendation.HOLD
        if score >= self._delay:
            return ConfidenceRecommendation.DELAY
        return ConfidenceRecommendation.BLOCK

    @staticmethod
    def _mean(values: List[float]) -> float:
        return sum(values) / len(values) if values else 0.0
