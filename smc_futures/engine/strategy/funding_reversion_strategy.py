"""
Funding Rate Reversion Strategy
════════════════════════════════
Generates mean-reversion signals when funding rate reaches extreme levels,
indicating over-crowded positioning that tends to correct.

  funding very positive  → market over-long  → SHORT signal
  funding very negative  → market over-short → LONG signal

Confirmation via RSI to avoid fighting strong momentum.
"""

import logging
from typing import Dict, List, Optional

from smc_futures.engine.strategy.signal_builder import (
    SignalDirection,
    SignalResult,
    SMCSignal,
)

logger = logging.getLogger(__name__)


class FundingReversionStrategy:
    """
    Generates signals based on extreme funding rate + RSI confirmation.
    Designed for crypto perpetual futures (Bitget, Binance, etc.).
    """

    def __init__(
        self,
        funding_threshold: float = 0.0004,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        rsi_period: int = 14,
        min_rr: float = 1.5,
        sl_atr_mult: float = 1.2,
        tp_atr_mult: float = 2.0,
        risk_factor: float = 0.6,
    ):
        self.funding_threshold = funding_threshold
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.rsi_period = rsi_period
        self.min_rr = min_rr
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        self.risk_factor = risk_factor

    def build_signal(
        self,
        symbol: str,
        funding_rate: float,
        current_price: float,
        candles: List[Dict],
        atr: float,
        timestamp: int = 0,
    ) -> SignalResult:
        """
        Evaluate whether extreme funding + RSI divergence warrants entry.

        Args:
            symbol: Trading pair
            funding_rate: Current funding rate (e.g. 0.0005 = 0.05%)
            current_price: Last close
            candles: Recent candle dicts with 'close' key (for RSI)
            atr: Current ATR value
            timestamp: Candle timestamp (ms)

        Returns:
            SignalResult with signal if conditions met
        """
        result = SignalResult(symbol=symbol)

        if atr <= 0:
            result.reasons.append("NO_ATR_DATA")
            return result

        # ── Funding rate check ────────────────────
        if abs(funding_rate) < self.funding_threshold:
            result.reasons.append(
                f"FUNDING_NOT_EXTREME({funding_rate:.6f}_vs_thresh_{self.funding_threshold})"
            )
            return result

        # ── RSI computation ───────────────────────
        rsi = self._compute_rsi(candles)

        # ── Direction logic ───────────────────────
        direction = None

        if funding_rate > self.funding_threshold and rsi > self.rsi_overbought:
            direction = SignalDirection.SHORT
        elif funding_rate < -self.funding_threshold and rsi < self.rsi_oversold:
            direction = SignalDirection.LONG

        if direction is None:
            result.reasons.append(
                f"NO_CONFIRMATION(funding={funding_rate:.6f},rsi={rsi:.1f})"
            )
            return result

        # ── Build levels ──────────────────────────
        sl_dist = atr * self.sl_atr_mult
        tp_dist = atr * self.tp_atr_mult

        if direction == SignalDirection.LONG:
            entry = current_price
            sl = entry - sl_dist
            tp = entry + tp_dist
        else:
            entry = current_price
            sl = entry + sl_dist
            tp = entry - tp_dist

        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < self.min_rr:
            result.reasons.append(f"LOW_RR({rr:.2f}<{self.min_rr})")
            return result

        # ── Confidence ────────────────────────────
        funding_strength = min(abs(funding_rate) / self.funding_threshold, 3.0) / 3.0
        rsi_strength = (
            (rsi - self.rsi_overbought) / 30.0
            if direction == SignalDirection.SHORT
            else (self.rsi_oversold - rsi) / 30.0
        )
        confidence = 0.4 + 0.3 * funding_strength + 0.3 * max(0, min(1, rsi_strength))

        signal = SMCSignal(
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="FUNDING_REVERSION",
            trigger_type=f"FR={funding_rate:.6f}_RSI={rsi:.1f}",
            confidence_score=confidence * self.risk_factor,
            risk_reward_ratio=rr,
            invalidation_level=sl,
            mtf_bias="extreme_funding",
            timestamp=timestamp,
            timeframe="funding",
        )

        result.signal = signal
        result.status = "CREATED"

        logger.info(
            f"[FUNDING_REV] {symbol} | SIGNAL"
            f" | dir={direction.value}"
            f" | funding={funding_rate:.6f}"
            f" | rsi={rsi:.1f}"
            f" | rr={rr:.2f}"
            f" | conf={signal.confidence_score:.2f}"
        )

        return result

    def _compute_rsi(self, candles: List[Dict]) -> float:
        """Compute RSI from candle list."""
        try:
            from backend.engine.strategy.rsi import calculate_rsi

            closes = [float(c["close"]) for c in candles if "close" in c]
            if len(closes) < self.rsi_period + 1:
                return 50.0
            return calculate_rsi(closes, self.rsi_period)
        except ImportError:
            return 50.0
