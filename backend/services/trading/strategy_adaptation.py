"""
Strategy Adaptation Engine — Dynamic Parameter Tuning
═════════════════════════════════════════════════════

Adjusts trading parameters in real-time based on:
  - Recent win rate vs. baseline
  - Current drawdown state
  - Detected market regime
  - Setup-specific performance from MetaPerformanceAnalyzer

Parameters tuned:
  - confidence_threshold  (higher = more selective)
  - size_multiplier       (scale up/down from PositionSizingEngine)
  - partial_tp_pct        (how much to take at TP1)
  - trailing_atr_mult     (trailing stop aggressiveness)
  - max_concurrent_trades
  - min_risk_reward
  - cooldown_multiplier

All adjustments are BOUNDED — the engine can never exceed predefined
safety limits regardless of how optimistic the adaptation becomes.

Config via .env:
  ADAPT_BASELINE_WIN_RATE=0.55
  ADAPT_LOOKBACK_TRADES=30
  ADAPT_MAX_SIZE_MULT=1.8
  ADAPT_MIN_SIZE_MULT=0.3
  ADAPT_MAX_CONFIDENCE_THRESH=0.7
  ADAPT_MIN_CONFIDENCE_THRESH=0.25
  ADAPT_MAX_TRAILING_ATR=3.5
  ADAPT_MIN_TRAILING_ATR=1.0
"""

import logging
import os
from typing import Dict, List, Optional

from backend.services.trading.adaptive_models import (
    AdaptationParams,
    DimensionStats,
    MarketRegime,
    RegimeSnapshot,
)
from backend.services.trading.models import PerformanceSnapshot

logger = logging.getLogger("smarttrade.adaptive.strategy")


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


class StrategyAdaptationEngine:
    """
    Produces ``AdaptationParams`` that dynamically override default
    thresholds for the current market conditions and performance state.
    """

    def __init__(self) -> None:
        self._baseline_wr = _envf("ADAPT_BASELINE_WIN_RATE", 0.55)
        self._lookback_trades = _envi("ADAPT_LOOKBACK_TRADES", 30)

        # Safety bounds
        self._max_size_mult = _envf("ADAPT_MAX_SIZE_MULT", 1.8)
        self._min_size_mult = _envf("ADAPT_MIN_SIZE_MULT", 0.3)
        self._max_conf_thresh = _envf("ADAPT_MAX_CONFIDENCE_THRESH", 0.7)
        self._min_conf_thresh = _envf("ADAPT_MIN_CONFIDENCE_THRESH", 0.25)
        self._max_trail_atr = _envf("ADAPT_MAX_TRAILING_ATR", 3.5)
        self._min_trail_atr = _envf("ADAPT_MIN_TRAILING_ATR", 1.0)

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def adapt(
        self,
        performance: PerformanceSnapshot,
        regime: Optional[RegimeSnapshot] = None,
        regime_stats: Optional[DimensionStats] = None,
        setup_stats: Optional[DimensionStats] = None,
    ) -> AdaptationParams:
        """
        Compute adapted parameters from current conditions.

        Parameters
        ----------
        performance : PerformanceSnapshot
            Overall recent performance (win rate, drawdown, streaks).
        regime : RegimeSnapshot, optional
            Current market regime.
        regime_stats : DimensionStats, optional
            Historical performance IN this specific regime.
        setup_stats : DimensionStats, optional
            Historical performance for this specific setup type.

        Returns
        -------
        AdaptationParams with tuned thresholds and multipliers.
        """
        params = AdaptationParams()
        reasons: List[str] = []

        # === 1. Win-rate adaptation ===
        wr_delta = self._win_rate_adaptation(performance, params, reasons)

        # === 2. Drawdown adaptation ===
        self._drawdown_adaptation(performance, params, reasons)

        # === 3. Regime adaptation ===
        self._regime_adaptation(regime, regime_stats, params, reasons)

        # === 4. Setup-specific adaptation ===
        self._setup_adaptation(setup_stats, params, reasons)

        # === 5. Streak adaptation ===
        self._streak_adaptation(performance, params, reasons)

        # === Enforce safety bounds ===
        params.size_multiplier = max(
            self._min_size_mult, min(self._max_size_mult, params.size_multiplier)
        )
        params.confidence_threshold = max(
            self._min_conf_thresh, min(self._max_conf_thresh, params.confidence_threshold)
        )
        params.trailing_atr_mult = max(
            self._min_trail_atr, min(self._max_trail_atr, params.trailing_atr_mult)
        )
        params.partial_tp_pct = max(25.0, min(75.0, params.partial_tp_pct))
        params.max_concurrent_trades = max(1, min(5, params.max_concurrent_trades))
        params.min_risk_reward = max(1.0, min(3.0, params.min_risk_reward))
        params.cooldown_multiplier = max(0.5, min(3.0, params.cooldown_multiplier))

        params.reason = "; ".join(reasons) if reasons else "default"

        logger.info(
            "[ADAPT] size=%.2f conf_thresh=%.3f tp_pct=%.0f trail=%.2f | %s",
            params.size_multiplier,
            params.confidence_threshold,
            params.partial_tp_pct,
            params.trailing_atr_mult,
            params.reason,
        )
        return params

    # ──────────────────────────────────────────────────
    #  ADAPTATION COMPONENTS
    # ──────────────────────────────────────────────────

    def _win_rate_adaptation(
        self,
        perf: PerformanceSnapshot,
        params: AdaptationParams,
        reasons: List[str],
    ) -> float:
        """Adjust based on recent win rate vs. baseline."""
        if perf.total_trades < 5:
            return 0.0

        wr = perf.win_rate
        delta = wr - self._baseline_wr

        if delta > 0.15:
            # Significantly outperforming → get more aggressive
            params.size_multiplier *= 1.3
            params.confidence_threshold -= 0.05
            params.max_concurrent_trades = 4
            reasons.append(f"wr={wr:.0%} >> baseline → boost")
        elif delta > 0.05:
            # Slightly above → mild boost
            params.size_multiplier *= 1.1
            reasons.append(f"wr={wr:.0%} > baseline → mild boost")
        elif delta < -0.15:
            # Significantly underperforming → get defensive
            params.size_multiplier *= 0.5
            params.confidence_threshold += 0.1
            params.max_concurrent_trades = 2
            params.cooldown_multiplier = 2.0
            reasons.append(f"wr={wr:.0%} << baseline → defensive")
        elif delta < -0.05:
            # Slightly below → reduce
            params.size_multiplier *= 0.8
            params.confidence_threshold += 0.05
            reasons.append(f"wr={wr:.0%} < baseline → cautious")

        return delta

    def _drawdown_adaptation(
        self,
        perf: PerformanceSnapshot,
        params: AdaptationParams,
        reasons: List[str],
    ) -> None:
        """Scale back during drawdown."""
        dd = perf.current_drawdown_pct

        if dd >= 5.0:
            # Deep drawdown → minimal activity
            params.size_multiplier *= 0.3
            params.confidence_threshold += 0.15
            params.max_concurrent_trades = 1
            params.min_risk_reward = 2.5
            params.cooldown_multiplier = 3.0
            reasons.append(f"dd={dd:.1f}% → minimal")
        elif dd >= 3.0:
            # Moderate drawdown → reduce exposure
            params.size_multiplier *= 0.6
            params.confidence_threshold += 0.08
            params.max_concurrent_trades = 2
            params.min_risk_reward = 2.0
            reasons.append(f"dd={dd:.1f}% → reduced")
        elif dd >= 1.5:
            # Mild drawdown → slightly cautious
            params.size_multiplier *= 0.85
            reasons.append(f"dd={dd:.1f}% → mildly cautious")

    def _regime_adaptation(
        self,
        regime: Optional[RegimeSnapshot],
        regime_stats: Optional[DimensionStats],
        params: AdaptationParams,
        reasons: List[str],
    ) -> None:
        """Adjust parameters for the current market regime."""
        if not regime or regime.regime == MarketRegime.UNKNOWN:
            return

        r = regime.regime

        # High vol: tighter stops, smaller size, wider TP targets
        if r == MarketRegime.HIGH_VOLATILITY:
            params.size_multiplier *= 0.7
            params.trailing_atr_mult = 2.5
            params.partial_tp_pct = 60.0  # take more off early
            reasons.append("high_vol → smaller + wider trail")

        # Low vol: tighter trailing, patient TP targets
        elif r == MarketRegime.LOW_VOLATILITY:
            params.trailing_atr_mult = 1.5
            params.partial_tp_pct = 40.0  # let more ride
            reasons.append("low_vol → tight trail + patient TP")

        # Momentum expansion: ride the move
        elif r == MarketRegime.MOMENTUM_EXPANSION:
            params.size_multiplier *= 1.2
            params.partial_tp_pct = 35.0  # let it run
            params.trailing_atr_mult = 2.0
            if regime.confidence > 0.7:
                params.size_multiplier *= 1.15
            reasons.append("momentum → ride + boost")

        # Ranging: be selective, quick TP
        elif r == MarketRegime.RANGING:
            params.confidence_threshold += 0.05
            params.partial_tp_pct = 65.0  # take profits quickly
            params.trailing_atr_mult = 1.2
            params.min_risk_reward = 2.0
            reasons.append("ranging → selective + quick TP")

        # Trending: align to trend, moderate trail
        elif r in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            if regime.trend_strength > 0.6:
                params.size_multiplier *= 1.15
                params.partial_tp_pct = 40.0
            reasons.append(f"{r.value} → trend-aligned")

        # Overlay regime-specific historical performance
        if regime_stats and regime_stats.sample_size_ok:
            if regime_stats.win_rate < 0.35:
                params.size_multiplier *= 0.5
                params.confidence_threshold += 0.1
                reasons.append(
                    f"regime wr={regime_stats.win_rate:.0%} poor → cut"
                )
            elif regime_stats.win_rate > 0.70:
                params.size_multiplier *= 1.2
                reasons.append(
                    f"regime wr={regime_stats.win_rate:.0%} strong → boost"
                )

    def _setup_adaptation(
        self,
        setup_stats: Optional[DimensionStats],
        params: AdaptationParams,
        reasons: List[str],
    ) -> None:
        """Adjust based on this specific setup's track record."""
        if not setup_stats or not setup_stats.sample_size_ok:
            return

        wr = setup_stats.win_rate
        pf = setup_stats.profit_factor

        # Strong setup historically → boost
        if wr > 0.65 and pf > 2.0:
            params.size_multiplier *= 1.25
            params.confidence_threshold -= 0.05
            reasons.append(f"setup wr={wr:.0%} pf={pf:.1f} → boost")
        # Decent setup
        elif wr > 0.55 and pf > 1.5:
            params.size_multiplier *= 1.1
            reasons.append(f"setup wr={wr:.0%} → mild boost")
        # Weak setup → cut or skip
        elif wr < 0.40 or pf < 0.8:
            params.size_multiplier *= 0.4
            params.confidence_threshold += 0.1
            reasons.append(f"setup wr={wr:.0%} pf={pf:.1f} → cut")
        elif wr < 0.50:
            params.size_multiplier *= 0.7
            reasons.append(f"setup wr={wr:.0%} → reduce")

    def _streak_adaptation(
        self,
        perf: PerformanceSnapshot,
        params: AdaptationParams,
        reasons: List[str],
    ) -> None:
        """Fine-tune based on current win/loss streaks."""
        if perf.consecutive_wins >= 5:
            # Long win streak: slightly tighten (avoid overconfidence)
            params.size_multiplier *= 0.95
            params.confidence_threshold += 0.03
            reasons.append(f"win_streak={perf.consecutive_wins} → slight tighten")
        elif perf.consecutive_wins >= 3:
            # Moderate streak: maintain edge
            params.size_multiplier *= 1.05
            reasons.append(f"win_streak={perf.consecutive_wins} → maintain")

        if perf.consecutive_losses >= 4:
            # Bad streak: go defensive
            params.size_multiplier *= 0.5
            params.confidence_threshold += 0.1
            params.cooldown_multiplier = 2.5
            reasons.append(f"loss_streak={perf.consecutive_losses} → defensive")
        elif perf.consecutive_losses >= 2:
            params.size_multiplier *= 0.8
            params.cooldown_multiplier = 1.5
            reasons.append(f"loss_streak={perf.consecutive_losses} → cautious")
