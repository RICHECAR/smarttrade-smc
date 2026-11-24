"""
RegimeFilter — Market Regime Detection & Strategy Gating
═════════════════════════════════════════════════════════
Detects 4 market regimes:
  TREND      – Strong directional movement (ADX-proxy high + clear HH/HL or LH/LL)
  RANGE      – Low directional strength, contained price action
  HIGH_VOL   – ATR ratio far above mean (liquidation cascades, news events)
  LOW_VOL    – ATR ratio well below mean (dead market, Asia lull)

Strategy gating:
  - AsiaWeekendStrategy (mean reversion) is BLOCKED in TREND regime
  - DefaultSMCStrategy  (breakout/trend) is DEGRADED in RANGE regime (confidence -20%)
  - Both strategies PAUSED in HIGH_VOL regime
  - Both strategies confidence-reduced in LOW_VOL regime (poor R:R)
"""

import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"


class RegimeFilter:
    """Detects market regime from candle data and gates strategy execution.
    
    Hysteresis: a regime change must persist for `hysteresis_cycles` consecutive
    detections before the filter adopts it.  This prevents whipsawing between
    TREND and RANGE on noisy candles.
    
    Cooldown: after a regime transition, no further transition is allowed for
    `cooldown_cycles` cycles.
    """

    def __init__(self, config: Dict):
        self.enabled: bool = config.get("REGIME_FILTER_ENABLED", True)
        # Directional strength: count of consecutive HH/HL (bull) or LH/LL (bear)
        self.trend_threshold: int = config.get("REGIME_TREND_THRESHOLD", 4)
        # ATR ratio thresholds
        self.high_vol_ratio: float = config.get("REGIME_HIGH_VOL_ATR_RATIO", 2.0)
        self.low_vol_ratio: float = config.get("REGIME_LOW_VOL_ATR_RATIO", 0.5)
        # Lookback for directional strength calculation
        self.lookback: int = config.get("REGIME_LOOKBACK", 20)
        # Range compression: range_size / ATR < threshold → compression → breakout imminent
        self.compression_threshold: float = config.get("REGIME_COMPRESSION_RATIO", 1.0)

        # Hysteresis & Cooldown
        self.hysteresis_cycles: int = config.get("REGIME_HYSTERESIS_CYCLES", 3)
        self.cooldown_cycles: int = config.get("REGIME_COOLDOWN_CYCLES", 5)

        self._last_regime: Optional[MarketRegime] = None
        self._pending_regime: Optional[MarketRegime] = None
        self._pending_count: int = 0
        self._cooldown_remaining: int = 0

        logger.info(
            f"[REGIME] Init | enabled={self.enabled}"
            f" | trend_thresh={self.trend_threshold}"
            f" | high_vol_ratio={self.high_vol_ratio}"
            f" | low_vol_ratio={self.low_vol_ratio}"
            f" | hysteresis={self.hysteresis_cycles}"
            f" | cooldown={self.cooldown_cycles}"
        )

    def detect(
        self,
        candles: List,
        current_atr: float,
        avg_atr: float,
    ) -> MarketRegime:
        """
        Detect current market regime from recent candles + ATR data.
        Uses hysteresis: a regime change must be confirmed for
        `hysteresis_cycles` consecutive calls before being adopted.
        
        Exception: HIGH_VOL is adopted immediately (safety override).
        """
        if not self.enabled or avg_atr <= 0:
            return MarketRegime.RANGE  # safe default

        # 1. Raw detection (same logic as before)
        raw_regime = self._raw_detect(candles, current_atr, avg_atr)

        # 2. First call — initialize
        if self._last_regime is None:
            self._last_regime = raw_regime
            return raw_regime

        # 3. HIGH_VOL is instant (safety override, no hysteresis)
        if raw_regime == MarketRegime.HIGH_VOL and self._last_regime != MarketRegime.HIGH_VOL:
            self._transition(raw_regime)
            return self._last_regime

        # 4. Cooldown: ignore transitions during cooldown
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            # Still track pending for when cooldown expires
            if raw_regime != self._last_regime:
                if raw_regime == self._pending_regime:
                    self._pending_count += 1
                else:
                    self._pending_regime = raw_regime
                    self._pending_count = 1
            else:
                self._pending_regime = None
                self._pending_count = 0
            return self._last_regime

        # 5. Hysteresis logic
        if raw_regime != self._last_regime:
            if raw_regime == self._pending_regime:
                self._pending_count += 1
            else:
                self._pending_regime = raw_regime
                self._pending_count = 1

            if self._pending_count >= self.hysteresis_cycles:
                self._transition(raw_regime)
        else:
            # Raw matches current — reset pending
            self._pending_regime = None
            self._pending_count = 0

        return self._last_regime

    def _transition(self, new_regime: MarketRegime) -> None:
        """Execute a regime transition, reset pending, start cooldown."""
        logger.info(
            f"[REGIME] Change: {self._last_regime} → {new_regime}"
        )
        self._last_regime = new_regime
        self._pending_regime = None
        self._pending_count = 0
        self._cooldown_remaining = self.cooldown_cycles

    def _raw_detect(
        self,
        candles: List,
        current_atr: float,
        avg_atr: float,
    ) -> MarketRegime:
        """Pure stateless regime detection (no hysteresis)."""
        atr_ratio = current_atr / avg_atr

        if atr_ratio >= self.high_vol_ratio:
            return MarketRegime.HIGH_VOL
        elif atr_ratio <= self.low_vol_ratio:
            return MarketRegime.LOW_VOL
        else:
            dir_strength = self._directional_strength(candles)
            compression = self._range_compression(candles, current_atr)
            if dir_strength >= self.trend_threshold or (compression and dir_strength >= self.trend_threshold - 1):
                return MarketRegime.TREND
            else:
                return MarketRegime.RANGE

    def gate_strategy(
        self,
        regime: MarketRegime,
        strategy_name: str,
    ) -> Tuple[bool, float, str]:
        """
        Check if a strategy should execute under the current regime.

        Returns:
            (allowed, confidence_multiplier, reason)
            - allowed: False means skip this signal entirely
            - confidence_multiplier: 1.0 = no change, <1.0 = reduce confidence
        """
        if not self.enabled:
            return True, 1.0, "regime_filter_disabled"

        is_asia = "asia" in strategy_name.lower() or "weekend" in strategy_name.lower()

        if regime == MarketRegime.HIGH_VOL:
            return False, 0.0, "HIGH_VOL: paused — extreme volatility"

        if regime == MarketRegime.TREND and is_asia:
            return False, 0.0, "TREND: mean-reversion blocked in trending market"

        if regime == MarketRegime.RANGE and not is_asia:
            return True, 0.8, "RANGE: trend strategy confidence reduced"

        if regime == MarketRegime.LOW_VOL:
            return True, 0.7, "LOW_VOL: reduced confidence — poor R:R"

        return True, 1.0, f"{regime.value}: OK"

    def _directional_strength(self, candles: List) -> int:
        """
        ADX-proxy: count consecutive higher-highs + higher-lows (bullish)
        or lower-highs + lower-lows (bearish).

        Returns max of bullish or bearish consecutive count.
        Supports both dict candles and object candles (.high / .low attributes).
        """
        if not candles or len(candles) < 3:
            return 0

        recent = candles[-self.lookback:] if len(candles) >= self.lookback else candles

        def _h(c):
            return float(c.get("high", c.get("h", 0))) if isinstance(c, dict) else float(getattr(c, "high", 0))

        def _l(c):
            return float(c.get("low", c.get("l", 0))) if isinstance(c, dict) else float(getattr(c, "low", 0))

        bull_count = 0
        bear_count = 0
        max_bull = 0
        max_bear = 0

        for i in range(1, len(recent)):
            prev_h = _h(recent[i - 1])
            prev_l = _l(recent[i - 1])
            curr_h = _h(recent[i])
            curr_l = _l(recent[i])

            if curr_h > prev_h and curr_l > prev_l:
                bull_count += 1
                bear_count = 0
            elif curr_h < prev_h and curr_l < prev_l:
                bear_count += 1
                bull_count = 0
            else:
                bull_count = 0
                bear_count = 0

            max_bull = max(max_bull, bull_count)
            max_bear = max(max_bear, bear_count)

        return max(max_bull, max_bear)

    def _range_compression(self, candles: List, current_atr: float) -> bool:
        """
        Detect range compression: recent candle range / ATR < threshold.
        Compression signals breakout is imminent → supports TREND detection.
        """
        if not candles or current_atr <= 0 or len(candles) < 5:
            return False

        def _h(c):
            return float(c.get("high", c.get("h", 0))) if isinstance(c, dict) else float(getattr(c, "high", 0))

        def _l(c):
            return float(c.get("low", c.get("l", 0))) if isinstance(c, dict) else float(getattr(c, "low", 0))

        # Use last 5 candles to measure range compression
        recent = candles[-5:]
        highest = max(_h(c) for c in recent)
        lowest = min(_l(c) for c in recent)
        range_size = highest - lowest

        if range_size <= 0:
            return False

        range_ratio = range_size / current_atr
        return range_ratio < self.compression_threshold
