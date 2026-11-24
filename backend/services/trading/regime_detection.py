"""
Regime Detection Engine — Market State Classification
═════════════════════════════════════════════════════

Classifies market into one of 6 regimes using only injectable inputs:
  - TRENDING_UP / TRENDING_DOWN  (strong directional bias)
  - RANGING                      (sideways, mean-reverting)
  - HIGH_VOLATILITY              (expanded ATR, chaotic)
  - LOW_VOLATILITY               (compressed, pre-breakout)
  - MOMENTUM_EXPANSION           (breakout + volume surge)

Detection pipeline:
  1. Trend strength via Directional Movement proxy (ADX-like)
  2. Volatility percentile via ATR vs rolling history
  3. Bollinger bandwidth for compression/expansion
  4. Momentum rate-of-change alignment

All inputs are price-based.  No external data needed.

Config via .env:
  REGIME_LOOKBACK=50
  REGIME_ADX_PERIOD=14
  REGIME_VOL_LOOKBACK=100
  REGIME_TREND_THRESHOLD=0.35
  REGIME_RANGE_MAX_TREND=0.20
  REGIME_VOL_HIGH_PERCENTILE=0.80
  REGIME_VOL_LOW_PERCENTILE=0.20
  REGIME_MOMENTUM_EXPANSION_MULT=2.0
"""

import logging
import math
import os
from collections import deque
from typing import Deque, Dict, List, Optional

from backend.services.trading.adaptive_models import MarketRegime, RegimeSnapshot

logger = logging.getLogger("smarttrade.adaptive.regime")


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


class RegimeDetectionEngine:
    """
    Stateful regime detector.  Feed candles with ``ingest()``, then call
    ``detect()`` to get the current regime classification.
    """

    def __init__(self) -> None:
        self._lookback = _envi("REGIME_LOOKBACK", 50)
        self._adx_period = _envi("REGIME_ADX_PERIOD", 14)
        self._vol_lookback = _envi("REGIME_VOL_LOOKBACK", 100)
        self._trend_thresh = _envf("REGIME_TREND_THRESHOLD", 0.35)
        self._range_max_trend = _envf("REGIME_RANGE_MAX_TREND", 0.20)
        self._vol_high_pct = _envf("REGIME_VOL_HIGH_PERCENTILE", 0.80)
        self._vol_low_pct = _envf("REGIME_VOL_LOW_PERCENTILE", 0.20)
        self._mom_expansion_mult = _envf("REGIME_MOMENTUM_EXPANSION_MULT", 2.0)

        max_len = max(self._lookback, self._vol_lookback, self._adx_period * 3) + 10
        self._max_len = max_len

        # Per-symbol rolling data
        self._closes: Dict[str, Deque[float]] = {}
        self._highs: Dict[str, Deque[float]] = {}
        self._lows: Dict[str, Deque[float]] = {}
        self._atrs: Dict[str, Deque[float]] = {}
        self._regimes: Dict[str, MarketRegime] = {}
        self._regime_bar_count: Dict[str, int] = {}

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def ingest(
        self, symbol: str, open_: float, high: float, low: float, close: float
    ) -> None:
        """Feed a completed candle."""
        if symbol not in self._closes:
            self._closes[symbol] = deque(maxlen=self._max_len)
            self._highs[symbol] = deque(maxlen=self._max_len)
            self._lows[symbol] = deque(maxlen=self._max_len)
            self._atrs[symbol] = deque(maxlen=self._max_len)

        self._closes[symbol].append(close)
        self._highs[symbol].append(high)
        self._lows[symbol].append(low)

        # Compute true range for this bar
        closes = self._closes[symbol]
        if len(closes) >= 2:
            prev_c = closes[-2]
            tr = max(high - low, abs(high - prev_c), abs(low - prev_c))
        else:
            tr = high - low
        self._atrs[symbol].append(tr)

    def detect(self, symbol: str) -> RegimeSnapshot:
        """
        Classify the current market regime for *symbol*.
        Requires at least ``_lookback`` candles of history.
        """
        closes = self._closes.get(symbol)
        if not closes or len(closes) < self._lookback:
            return RegimeSnapshot(symbol=symbol, regime=MarketRegime.UNKNOWN)

        trend_strength, trend_direction = self._compute_trend(symbol)
        vol_percentile = self._compute_vol_percentile(symbol)
        bandwidth = self._compute_bandwidth(symbol)
        momentum = self._compute_momentum(symbol)

        # ── Classification logic ──
        regime = self._classify(
            trend_strength, trend_direction, vol_percentile, bandwidth, momentum
        )

        # Track regime duration
        prev_regime = self._regimes.get(symbol, MarketRegime.UNKNOWN)
        if regime == prev_regime:
            self._regime_bar_count[symbol] = self._regime_bar_count.get(symbol, 0) + 1
        else:
            self._regime_bar_count[symbol] = 1
        self._regimes[symbol] = regime

        # Confidence: how clearly classified
        confidence = self._compute_confidence(
            trend_strength, vol_percentile, bandwidth, regime
        )

        snap = RegimeSnapshot(
            symbol=symbol,
            regime=regime,
            trend_strength=trend_strength,
            volatility_percentile=vol_percentile,
            momentum_score=momentum,
            bandwidth=bandwidth,
            regime_age_bars=self._regime_bar_count.get(symbol, 1),
            confidence=confidence,
        )

        logger.debug(
            "[REGIME] %s → %s (trend=%.2f vol_pct=%.2f bw=%.4f mom=%.2f conf=%.2f)",
            symbol, regime.value, trend_strength, vol_percentile,
            bandwidth, momentum, confidence,
        )
        return snap

    def get_regime(self, symbol: str) -> MarketRegime:
        """Quick accessor – returns last detected regime."""
        return self._regimes.get(symbol, MarketRegime.UNKNOWN)

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    def _classify(
        self,
        trend_strength: float,
        trend_direction: float,
        vol_percentile: float,
        bandwidth: float,
        momentum: float,
    ) -> MarketRegime:
        """Core classification rules."""

        # Momentum expansion: strong momentum + expanding vol
        if (
            abs(momentum) > 0.6
            and vol_percentile > self._vol_high_pct
            and trend_strength > self._trend_thresh
        ):
            return MarketRegime.MOMENTUM_EXPANSION

        # High volatility: vol percentile above threshold regardless of trend
        if vol_percentile > self._vol_high_pct and trend_strength < self._trend_thresh:
            return MarketRegime.HIGH_VOLATILITY

        # Low volatility: compressed, potential breakout zone
        if vol_percentile < self._vol_low_pct and bandwidth < 0.02:
            return MarketRegime.LOW_VOLATILITY

        # Trending: strong directional bias
        if trend_strength >= self._trend_thresh:
            if trend_direction > 0:
                return MarketRegime.TRENDING_UP
            else:
                return MarketRegime.TRENDING_DOWN

        # Ranging: weak trend
        if trend_strength <= self._range_max_trend:
            return MarketRegime.RANGING

        # Default: ranging if nothing else fits
        return MarketRegime.RANGING

    def _compute_trend(self, symbol: str) -> tuple:
        """
        ADX-like trend strength (0–1) and direction sign.
        Uses smoothed directional movement.
        """
        highs = list(self._highs[symbol])
        lows = list(self._lows[symbol])
        closes = list(self._closes[symbol])
        period = min(self._adx_period, len(closes) - 1)
        if period < 5:
            return 0.0, 0.0

        plus_dm_sum = 0.0
        minus_dm_sum = 0.0
        tr_sum = 0.0

        for i in range(-period, 0):
            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]
            plus_dm = max(high_diff, 0.0) if high_diff > low_diff else 0.0
            minus_dm = max(low_diff, 0.0) if low_diff > high_diff else 0.0

            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )

            plus_dm_sum += plus_dm
            minus_dm_sum += minus_dm
            tr_sum += tr

        if tr_sum == 0:
            return 0.0, 0.0

        plus_di = plus_dm_sum / tr_sum
        minus_di = minus_dm_sum / tr_sum
        di_sum = plus_di + minus_di

        if di_sum == 0:
            return 0.0, 0.0

        # DX normalised to 0–1
        dx = abs(plus_di - minus_di) / di_sum
        direction = 1.0 if plus_di > minus_di else -1.0

        return min(1.0, dx), direction

    def _compute_vol_percentile(self, symbol: str) -> float:
        """
        Current ATR as a percentile of its own history.
        Returns 0.0–1.0.
        """
        atrs = list(self._atrs[symbol])
        if len(atrs) < 10:
            return 0.5

        current_atr = sum(atrs[-self._adx_period:]) / min(self._adx_period, len(atrs))
        sorted_atrs = sorted(atrs)
        # Rank
        rank = sum(1 for a in sorted_atrs if a <= current_atr)
        return rank / len(sorted_atrs)

    def _compute_bandwidth(self, symbol: str) -> float:
        """
        Bollinger Bandwidth = (upper - lower) / middle.
        Measures price compression/expansion.
        """
        closes = list(self._closes[symbol])
        period = min(20, len(closes))
        if period < 5:
            return 0.0

        window = closes[-period:]
        mean = sum(window) / len(window)
        if mean == 0:
            return 0.0

        variance = sum((c - mean) ** 2 for c in window) / len(window)
        std = math.sqrt(variance)
        upper = mean + 2 * std
        lower = mean - 2 * std

        return (upper - lower) / mean

    def _compute_momentum(self, symbol: str) -> float:
        """
        Momentum score from -1 to +1.
        Short-term ROC normalised and clamped.
        """
        closes = list(self._closes[symbol])
        if len(closes) < 15:
            return 0.0

        roc_10 = (closes[-1] - closes[-11]) / closes[-11] if closes[-11] != 0 else 0
        roc_5 = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] != 0 else 0

        # Blend short + medium momentum
        score = (roc_5 * 10 + roc_10 * 5) / 2
        return max(-1.0, min(1.0, score))

    def _compute_confidence(
        self,
        trend_strength: float,
        vol_percentile: float,
        bandwidth: float,
        regime: MarketRegime,
    ) -> float:
        """How certain we are about the classification (0–1)."""
        if regime == MarketRegime.UNKNOWN:
            return 0.0

        # Strong trend → high confidence for trending regimes
        if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            return min(1.0, trend_strength * 1.5)

        # Extreme vol percentile → high confidence for vol regimes
        if regime == MarketRegime.HIGH_VOLATILITY:
            return min(1.0, (vol_percentile - 0.5) * 2)

        if regime == MarketRegime.LOW_VOLATILITY:
            return min(1.0, (0.5 - vol_percentile) * 2)

        if regime == MarketRegime.MOMENTUM_EXPANSION:
            return min(1.0, trend_strength + (vol_percentile - 0.5))

        if regime == MarketRegime.RANGING:
            return min(1.0, (1.0 - trend_strength) * 1.3)

        return 0.5
