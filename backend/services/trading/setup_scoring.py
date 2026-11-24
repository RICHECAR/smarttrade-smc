"""
Setup Scoring Engine — Pre-Execution Signal Quality Assessment
═════════════════════════════════════════════════════════════

Scores each signal 0–1 BEFORE sizing/execution.  A low score can veto
the trade entirely, preventing low-edge setups from consuming capital.

Scoring dimensions:
  1. **Signal quality** — raw confidence, risk-reward, strength
  2. **Regime fit** — does this setup type historically perform in the
     current regime?
  3. **Setup history** — win rate of this specific setup (direction +
     strength) over the last N trades
  4. **Time-of-day** — performance during the current UTC hour
  5. **Streak context** — adjust score based on win/loss streaks

Each dimension outputs 0–1, then a weighted composite is returned.

Config via .env:
  SCORING_MIN_SCORE=0.35
  SCORING_VETO_BELOW=0.20
  SCORING_WEIGHT_SIGNAL=0.25
  SCORING_WEIGHT_REGIME=0.25
  SCORING_WEIGHT_HISTORY=0.25
  SCORING_WEIGHT_TOD=0.10
  SCORING_WEIGHT_STREAK=0.15
  SCORING_MIN_HISTORY_TRADES=5
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from backend.services.trading.adaptive_models import (
    MarketRegime,
    RegimeSnapshot,
    SetupScore,
)
from backend.services.trading.models import (
    PerformanceSnapshot,
    SignalDirection,
    TradeSignal,
)

logger = logging.getLogger("smarttrade.adaptive.scoring")


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


# ───────────────────────────────────────────────────────
#  INTERNAL: outcome record for history tracking
# ───────────────────────────────────────────────────────

class _OutcomeRecord:
    __slots__ = ("direction", "strength", "regime", "hour", "pnl", "win", "ts")

    def __init__(
        self,
        direction: str,
        strength: str,
        regime: str,
        hour: int,
        pnl: float,
        win: bool,
        ts: float,
    ):
        self.direction = direction
        self.strength = strength
        self.regime = regime
        self.hour = hour
        self.pnl = pnl
        self.win = win
        self.ts = ts


class SetupScoringEngine:
    """
    Evaluates trade signal quality before sizing.

    Usage:
        scorer = SetupScoringEngine()
        score = scorer.score(signal, regime_snap, perf_snap)
        if score.veto:
            skip trade
    """

    def __init__(self, max_history: int = 500) -> None:
        # Weights
        self._w_signal = _envf("SCORING_WEIGHT_SIGNAL", 0.25)
        self._w_regime = _envf("SCORING_WEIGHT_REGIME", 0.25)
        self._w_history = _envf("SCORING_WEIGHT_HISTORY", 0.25)
        self._w_tod = _envf("SCORING_WEIGHT_TOD", 0.10)
        self._w_streak = _envf("SCORING_WEIGHT_STREAK", 0.15)

        self._min_score = _envf("SCORING_MIN_SCORE", 0.35)
        self._veto_below = _envf("SCORING_VETO_BELOW", 0.20)
        self._min_history = _envi("SCORING_MIN_HISTORY_TRADES", 5)

        self._history: List[_OutcomeRecord] = []
        self._max_history = max_history

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def score(
        self,
        signal: TradeSignal,
        regime: Optional[RegimeSnapshot] = None,
        performance: Optional[PerformanceSnapshot] = None,
    ) -> SetupScore:
        """
        Score a signal before execution.

        Returns SetupScore with composite 0–1 score, priority (1–10),
        and optional veto flag.
        """
        components: Dict[str, float] = {}

        # 1. Signal quality
        sq = self._score_signal_quality(signal)
        components["signal_quality"] = sq

        # 2. Regime fit
        rf = self._score_regime_fit(signal, regime)
        components["regime_fit"] = rf

        # 3. Setup history (direction + strength track record)
        sh = self._score_setup_history(signal, regime)
        components["setup_history"] = sh

        # 4. Time-of-day
        tod = self._score_time_of_day()
        components["time_of_day"] = tod

        # 5. Streak context
        st = self._score_streak(performance)
        components["streak"] = st

        # Weighted composite
        composite = (
            sq * self._w_signal
            + rf * self._w_regime
            + sh * self._w_history
            + tod * self._w_tod
            + st * self._w_streak
        )
        composite = max(0.0, min(1.0, composite))

        # Priority (1 = best, 10 = worst)
        priority = max(1, min(10, int((1.0 - composite) * 10) + 1))

        # Veto logic
        veto = False
        veto_reason = ""
        if composite < self._veto_below:
            veto = True
            veto_reason = f"score {composite:.3f} below veto threshold {self._veto_below}"
        elif composite < self._min_score:
            veto = True
            veto_reason = f"score {composite:.3f} below min threshold {self._min_score}"

        result = SetupScore(
            score=composite,
            priority=priority,
            veto=veto,
            veto_reason=veto_reason,
            components=components,
        )

        logger.info(
            "[SCORING] %s %s | score=%.3f prio=%d veto=%s | %s",
            signal.symbol,
            signal.direction.value,
            composite,
            priority,
            veto,
            " ".join(f"{k}={v:.2f}" for k, v in components.items()),
        )
        return result

    def record_outcome(
        self,
        direction: str,
        strength: str,
        regime: str,
        pnl: float,
    ) -> None:
        """Record a trade outcome for future scoring."""
        now = time.time()
        hour = datetime.now(timezone.utc).hour

        self._history.append(
            _OutcomeRecord(
                direction=direction,
                strength=strength,
                regime=regime,
                hour=hour,
                pnl=pnl,
                win=pnl > 0,
                ts=now,
            )
        )
        # Cap history length
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    # ──────────────────────────────────────────────────
    #  SCORING COMPONENTS
    # ──────────────────────────────────────────────────

    def _score_signal_quality(self, signal: TradeSignal) -> float:
        """Score raw signal properties (confidence, RR, strength)."""
        # Confidence contributes 50%, RR contributes 30%, strength contributes 20%
        conf_score = signal.confidence  # already 0–1

        rr_score = min(1.0, signal.risk_reward / 4.0)  # 4:1 → 1.0

        strength_map = {"strong": 1.0, "moderate": 0.65, "weak": 0.3}
        str_score = strength_map.get(signal.strength.value, 0.3)

        return conf_score * 0.5 + rr_score * 0.3 + str_score * 0.2

    def _score_regime_fit(
        self, signal: TradeSignal, regime: Optional[RegimeSnapshot]
    ) -> float:
        """
        Score how well the signal fits the current regime.
        Trending regime + trending signal = high score.
        Ranging regime + trending signal = low score.
        """
        if not regime or regime.regime == MarketRegime.UNKNOWN:
            return 0.5  # neutral, no penalty, no boost

        r = regime.regime

        # Build a heuristic compatibility matrix
        if signal.direction in (SignalDirection.LONG, SignalDirection.SHORT):
            # Directional signals
            if r == MarketRegime.TRENDING_UP and signal.direction == SignalDirection.LONG:
                return 0.95
            if r == MarketRegime.TRENDING_DOWN and signal.direction == SignalDirection.SHORT:
                return 0.95
            if r == MarketRegime.TRENDING_UP and signal.direction == SignalDirection.SHORT:
                return 0.25  # counter-trend
            if r == MarketRegime.TRENDING_DOWN and signal.direction == SignalDirection.LONG:
                return 0.25  # counter-trend
            if r == MarketRegime.MOMENTUM_EXPANSION:
                # Momentum expansion: align with momentum
                if regime.momentum_score > 0 and signal.direction == SignalDirection.LONG:
                    return 0.9
                if regime.momentum_score < 0 and signal.direction == SignalDirection.SHORT:
                    return 0.9
                return 0.3  # wrong side
            if r == MarketRegime.RANGING:
                return 0.4  # directional signals not great in ranges
            if r == MarketRegime.HIGH_VOLATILITY:
                return 0.35  # risky in high vol
            if r == MarketRegime.LOW_VOLATILITY:
                return 0.6   # potential breakout

        return 0.5

    def _score_setup_history(
        self, signal: TradeSignal, regime: Optional[RegimeSnapshot]
    ) -> float:
        """
        Win rate of this setup type (direction + strength) in recent history.
        Falls back to 0.5 if insufficient data.
        """
        if not self._history:
            return 0.5

        # Filter for matching setups
        direction = signal.direction.value
        strength = signal.strength.value
        matching = [
            h for h in self._history
            if h.direction == direction and h.strength == strength
        ]

        if len(matching) < self._min_history:
            return 0.5  # not enough data

        wins = sum(1 for h in matching if h.win)
        wr = wins / len(matching)

        # Optionally narrow to same regime
        if regime and regime.regime != MarketRegime.UNKNOWN:
            regime_matching = [
                h for h in matching if h.regime == regime.regime.value
            ]
            if len(regime_matching) >= 3:
                regime_wins = sum(1 for h in regime_matching if h.win)
                regime_wr = regime_wins / len(regime_matching)
                # Blend: 60% regime-specific, 40% overall
                wr = regime_wr * 0.6 + wr * 0.4

        return wr

    def _score_time_of_day(self) -> float:
        """
        Performance in the current UTC hour bucket.
        Returns 0.5 if insufficient data.
        """
        hour = datetime.now(timezone.utc).hour

        matching = [h for h in self._history if h.hour == hour]
        if len(matching) < 3:
            return 0.5

        wins = sum(1 for h in matching if h.win)
        return wins / len(matching)

    def _score_streak(self, performance: Optional[PerformanceSnapshot]) -> float:
        """
        Adjust based on win/loss streaks.
        Winning streak → slightly boost (edge confirmation).
        Losing streak → lower score (something may be off).
        """
        if not performance:
            return 0.5

        if performance.consecutive_wins >= 3:
            return min(1.0, 0.5 + performance.consecutive_wins * 0.08)
        if performance.consecutive_losses >= 2:
            return max(0.1, 0.5 - performance.consecutive_losses * 0.12)

        return 0.5
