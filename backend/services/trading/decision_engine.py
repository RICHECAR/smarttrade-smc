"""
Decision Engine — Entry Signal Generation
═══════════════════════════════════════════

Consumes price data, positions, PnL, and volatility to produce TradeSignals.
Uses momentum, volatility expansion, and market structure (HH/HL/LH/LL).

Plug-and-play: takes any PriceSource-compatible provider.
Does NOT execute — only decides.

Config via .env:
  DECISION_MOMENTUM_SHORT=14
  DECISION_MOMENTUM_LONG=50
  DECISION_VOL_LOOKBACK=20
  DECISION_VOL_EXPANSION_MULT=1.5
  DECISION_MIN_CONFIDENCE=0.4
  DECISION_MIN_RR=1.5
  DECISION_COOLDOWN_SEC=60
"""

import logging
import math
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from backend.services.trading.models import (
    SignalDirection,
    SignalStrength,
    TradeSignal,
    VolatilityRegime,
)

logger = logging.getLogger("smarttrade.trading.decision")


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

def _env(key: str, default: str) -> str:
    return os.getenv(key, default)

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


# ═══════════════════════════════════════════════════════════════
#  DECISION ENGINE
# ═══════════════════════════════════════════════════════════════

class DecisionEngine:
    """
    Generates entry signals from price data.

    Pipeline per symbol:
      1. Ingest price → update rolling windows
      2. Compute momentum (short + long)
      3. Detect volatility regime (ATR-based)
      4. Identify market structure (HH/HL = bullish, LH/LL = bearish)
      5. Score confidence (0–1) from factor alignment
      6. Emit TradeSignal if confidence ≥ threshold and cooldown elapsed
    """

    def __init__(self) -> None:
        # Config
        self._mom_short = _envi("DECISION_MOMENTUM_SHORT", 14)
        self._mom_long = _envi("DECISION_MOMENTUM_LONG", 50)
        self._vol_lookback = _envi("DECISION_VOL_LOOKBACK", 20)
        self._vol_expansion_mult = _envf("DECISION_VOL_EXPANSION_MULT", 1.5)
        self._min_confidence = _envf("DECISION_MIN_CONFIDENCE", 0.4)
        self._min_rr = _envf("DECISION_MIN_RR", 1.5)
        self._cooldown_sec = _envf("DECISION_COOLDOWN_SEC", 60.0)
        self._atr_period = _envi("DECISION_ATR_PERIOD", 14)
        self._sl_atr_mult = _envf("DECISION_SL_ATR_MULT", 1.5)
        self._tp1_atr_mult = _envf("DECISION_TP1_ATR_MULT", 2.5)
        self._tp2_atr_mult = _envf("DECISION_TP2_ATR_MULT", 4.0)

        # Per-symbol state
        self._prices: Dict[str, Deque[float]] = {}        # close prices
        self._highs: Dict[str, Deque[float]] = {}
        self._lows: Dict[str, Deque[float]] = {}
        self._last_signal_time: Dict[str, float] = {}
        self._swing_highs: Dict[str, List[float]] = {}    # last N swing highs
        self._swing_lows: Dict[str, List[float]] = {}     # last N swing lows

        max_lookback = max(self._mom_long, self._vol_lookback, self._atr_period) + 10
        self._max_history = max_lookback

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def ingest_candle(
        self,
        symbol: str,
        open_: float,
        high: float,
        low: float,
        close: float,
    ) -> None:
        """Feed a closed candle. Call this on every new candle for each symbol."""
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=self._max_history)
            self._highs[symbol] = deque(maxlen=self._max_history)
            self._lows[symbol] = deque(maxlen=self._max_history)
            self._swing_highs[symbol] = []
            self._swing_lows[symbol] = []

        self._prices[symbol].append(close)
        self._highs[symbol].append(high)
        self._lows[symbol].append(low)
        self._update_swing_points(symbol)

    def evaluate(self, symbol: str, current_price: float) -> Optional[TradeSignal]:
        """
        Evaluate whether to generate a signal for *symbol* at *current_price*.
        Returns TradeSignal or None.
        """
        prices = self._prices.get(symbol)
        if not prices or len(prices) < self._mom_long:
            return None

        # Cooldown check
        now = time.time()
        last = self._last_signal_time.get(symbol, 0)
        if now - last < self._cooldown_sec:
            return None

        # ── Factor 1: Momentum ──
        mom_short = self._momentum(prices, self._mom_short)
        mom_long = self._momentum(prices, self._mom_long)

        # ── Factor 2: Volatility regime ──
        vol_regime = self._volatility_regime(symbol)

        # ── Factor 3: Market structure ──
        structure_bias = self._structure_bias(symbol)

        # ── Composite scoring ──
        direction, confidence, reasons = self._score(
            mom_short, mom_long, vol_regime, structure_bias
        )

        if direction == SignalDirection.FLAT:
            return None
        if confidence < self._min_confidence:
            return None

        # ── Compute SL/TP from ATR ──
        atr = self._compute_atr(symbol)
        if atr <= 0:
            return None

        if direction == SignalDirection.LONG:
            sl = current_price - self._sl_atr_mult * atr
            tp1 = current_price + self._tp1_atr_mult * atr
            tp2 = current_price + self._tp2_atr_mult * atr
        else:
            sl = current_price + self._sl_atr_mult * atr
            tp1 = current_price - self._tp1_atr_mult * atr
            tp2 = current_price - self._tp2_atr_mult * atr

        risk = abs(current_price - sl)
        reward = abs(tp1 - current_price)
        rr = reward / risk if risk > 0 else 0

        if rr < self._min_rr:
            return None

        # Determine strength
        if confidence >= 0.75:
            strength = SignalStrength.STRONG
        elif confidence >= 0.55:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        # Block signals during extreme volatility
        if vol_regime == VolatilityRegime.EXTREME:
            reasons.append("BLOCKED: extreme volatility")
            return None

        signal = TradeSignal(
            symbol=symbol,
            direction=direction,
            strength=strength,
            confidence=confidence,
            entry_price=current_price,
            stop_loss=round(sl, 2),
            take_profit_1=round(tp1, 2),
            take_profit_2=round(tp2, 2),
            risk_reward=rr,
            reasons=reasons,
        )

        self._last_signal_time[symbol] = now
        logger.info(
            "[DECISION] %s %s | conf=%.2f rr=%.1f | %s",
            symbol, direction.value, confidence, rr, "; ".join(reasons),
        )
        return signal

    def get_volatility_regime(self, symbol: str) -> VolatilityRegime:
        """Public accessor for current volatility regime."""
        return self._volatility_regime(symbol)

    # ──────────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────────

    @staticmethod
    def _momentum(prices: Deque[float], period: int) -> float:
        """Rate of change over *period* bars, normalised to [-1, 1]."""
        if len(prices) < period + 1:
            return 0.0
        old = prices[-period - 1]
        if old == 0:
            return 0.0
        roc = (prices[-1] - old) / old
        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, roc * 10))

    def _compute_atr(self, symbol: str) -> float:
        """Average True Range over self._atr_period bars."""
        highs = self._highs.get(symbol)
        lows = self._lows.get(symbol)
        prices = self._prices.get(symbol)
        if not highs or len(highs) < self._atr_period + 1:
            return 0.0

        trs: List[float] = []
        h = list(highs)
        lo = list(lows)
        c = list(prices)
        for i in range(-self._atr_period, 0):
            tr = max(
                h[i] - lo[i],
                abs(h[i] - c[i - 1]),
                abs(lo[i] - c[i - 1]),
            )
            trs.append(tr)
        return sum(trs) / len(trs) if trs else 0.0

    def _volatility_regime(self, symbol: str) -> VolatilityRegime:
        """Classify current volatility vs historical."""
        prices = self._prices.get(symbol)
        if not prices or len(prices) < self._vol_lookback + 5:
            return VolatilityRegime.NORMAL

        recent = list(prices)[-self._vol_lookback:]
        returns = [(recent[i] - recent[i - 1]) / recent[i - 1]
                    for i in range(1, len(recent)) if recent[i - 1] != 0]
        if not returns:
            return VolatilityRegime.NORMAL

        current_vol = self._std(returns)

        # Compare to longer-term baseline
        all_returns = []
        p = list(prices)
        for i in range(1, len(p)):
            if p[i - 1] != 0:
                all_returns.append((p[i] - p[i - 1]) / p[i - 1])
        if not all_returns:
            return VolatilityRegime.NORMAL

        baseline_vol = self._std(all_returns)
        if baseline_vol == 0:
            return VolatilityRegime.NORMAL

        ratio = current_vol / baseline_vol

        if ratio > 3.0:
            return VolatilityRegime.EXTREME
        if ratio > self._vol_expansion_mult:
            return VolatilityRegime.HIGH
        if ratio < 0.5:
            return VolatilityRegime.LOW
        return VolatilityRegime.NORMAL

    def _update_swing_points(self, symbol: str) -> None:
        """Detect swing highs/lows from the last 5 bars."""
        highs = self._highs.get(symbol)
        lows = self._lows.get(symbol)
        if not highs or len(highs) < 5:
            return

        h = list(highs)
        lo = list(lows)
        mid = -3  # check the bar 2 bars ago

        # Swing high: h[mid] > h[mid-1] and h[mid] > h[mid-2] and h[mid] > h[mid+1] and h[mid] > h[mid+2]
        if (h[mid] > h[mid - 1] and h[mid] > h[mid - 2]
                and h[mid] > h[mid + 1] and h[mid] > h[mid + 2]):
            swings = self._swing_highs[symbol]
            if not swings or swings[-1] != h[mid]:
                swings.append(h[mid])
                if len(swings) > 10:
                    swings.pop(0)

        # Swing low
        if (lo[mid] < lo[mid - 1] and lo[mid] < lo[mid - 2]
                and lo[mid] < lo[mid + 1] and lo[mid] < lo[mid + 2]):
            swings = self._swing_lows[symbol]
            if not swings or swings[-1] != lo[mid]:
                swings.append(lo[mid])
                if len(swings) > 10:
                    swings.pop(0)

    def _structure_bias(self, symbol: str) -> float:
        """
        Market structure score in [-1, 1].
        +1 = strong bullish (HH + HL)
        -1 = strong bearish (LH + LL)
         0 = neutral / insufficient data
        """
        sh = self._swing_highs.get(symbol, [])
        sl = self._swing_lows.get(symbol, [])
        if len(sh) < 2 or len(sl) < 2:
            return 0.0

        score = 0.0
        # Higher Highs → bullish
        if sh[-1] > sh[-2]:
            score += 0.5
        elif sh[-1] < sh[-2]:
            score -= 0.5

        # Higher Lows → bullish
        if sl[-1] > sl[-2]:
            score += 0.5
        elif sl[-1] < sl[-2]:
            score -= 0.5

        return score

    def _score(
        self,
        mom_short: float,
        mom_long: float,
        vol_regime: VolatilityRegime,
        structure: float,
    ) -> tuple:
        """
        Combine factors into (direction, confidence, reasons).
        """
        reasons: List[str] = []
        total = 0.0

        # Momentum alignment: both same sign = stronger signal
        mom_avg = (mom_short * 0.4 + mom_long * 0.6)
        if mom_short > 0 and mom_long > 0:
            total += abs(mom_avg)
            reasons.append(f"momentum_bullish({mom_short:.2f}/{mom_long:.2f})")
        elif mom_short < 0 and mom_long < 0:
            total += abs(mom_avg)
            reasons.append(f"momentum_bearish({mom_short:.2f}/{mom_long:.2f})")
        else:
            # Divergent momentum reduces confidence
            total += abs(mom_avg) * 0.3
            reasons.append(f"momentum_mixed({mom_short:.2f}/{mom_long:.2f})")

        # Volatility bonus/penalty
        vol_map = {
            VolatilityRegime.LOW: 0.05,
            VolatilityRegime.NORMAL: 0.15,
            VolatilityRegime.HIGH: 0.10,
            VolatilityRegime.EXTREME: -0.2,
        }
        vol_adj = vol_map.get(vol_regime, 0.0)
        total += vol_adj
        reasons.append(f"vol_regime={vol_regime.value}")

        # Structure alignment
        total += abs(structure) * 0.3
        if structure > 0:
            reasons.append("structure_bullish")
        elif structure < 0:
            reasons.append("structure_bearish")
        else:
            reasons.append("structure_neutral")

        # Direction decision
        if mom_avg > 0 and structure >= 0:
            direction = SignalDirection.LONG
        elif mom_avg < 0 and structure <= 0:
            direction = SignalDirection.SHORT
        elif abs(mom_avg) > 0.5:
            # Strong momentum overrides neutral structure
            direction = SignalDirection.LONG if mom_avg > 0 else SignalDirection.SHORT
            total *= 0.8  # Reduce confidence without structure
            reasons.append("momentum_override")
        else:
            direction = SignalDirection.FLAT

        confidence = max(0.0, min(1.0, total))
        return direction, confidence, reasons

    @staticmethod
    def _std(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        return math.sqrt(variance)
