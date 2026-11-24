"""
Liquidation Cascade Strategy
═════════════════════════════
Exploits chain-reaction liquidation events on crypto perpetual futures.

Two modes:

  A — Momentum cascade:
      Large liquidation spike + structure break → trade WITH the cascade.
      Example: 10M long liquidations → SHORT continuation.

  B — Exhaustion cascade:
      Massive liquidation volume + violent rejection wick → trade the REVERSAL.
      Example: 50M liquidations + exhaustion wick → LONG bounce.

The mode is selected dynamically based on price_velocity vs wick rejection:
  high velocity + structure break  → MOMENTUM
  extreme volume + rejection wick  → EXHAUSTION
"""

import logging
import time as _time
from typing import Dict, List, Optional

from smc_futures.engine.strategy.signal_builder import (
    SignalDirection,
    SignalResult,
    SMCSignal,
)

logger = logging.getLogger(__name__)


class LiquidationCascadeStrategy:
    """
    Generates signals around liquidation cascade events.
    Requires external liquidation data feed (Coinglass / exchange WS).
    """

    def __init__(
        self,
        spike_threshold_usd: float = 5_000_000,
        exhaustion_mult: float = 10.0,
        momentum_velocity_min: float = 0.003,
        reversal_wick_ratio: float = 0.50,
        tp_atr_mult: float = 1.0,
        sl_atr_mult: float = 0.5,
        min_rr: float = 1.5,
        risk_factor: float = 0.55,
        cooldown_ms: int = 120_000,
    ):
        """
        Args:
            spike_threshold_usd: Min liquidation volume (USD) to qualify as spike
            exhaustion_mult: Multiplier over spike_threshold to qualify as exhaustion event
            momentum_velocity_min: Min |price_change / price| over lookback for momentum mode
            reversal_wick_ratio: Min wick-to-body ratio for exhaustion reversal confirmation
            tp_atr_mult: TP in ATR units
            sl_atr_mult: SL in ATR units
            min_rr: Minimum R:R
            risk_factor: Confidence scaling (0-1)
            cooldown_ms: Min delay between signals per symbol (ms)
        """
        self.spike_threshold_usd = spike_threshold_usd
        self.exhaustion_mult = exhaustion_mult
        self.momentum_velocity_min = momentum_velocity_min
        self.reversal_wick_ratio = reversal_wick_ratio
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.min_rr = min_rr
        self.risk_factor = risk_factor
        self.cooldown_ms = cooldown_ms
        self._last_signal_ts: Dict[str, int] = {}

    # ──────────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────────
    def build_signal(
        self,
        symbol: str,
        current_price: float,
        candles: List[Dict],
        liquidation_volume_usd: float,
        liquidation_side: str,
        atr: float,
        timestamp: int = 0,
    ) -> SignalResult:
        """
        Evaluate liquidation event for cascade trading.

        Args:
            symbol: Trading pair
            current_price: Last close
            candles: Recent LTF candles (at least 5)
            liquidation_volume_usd: Total recent liquidation volume in USD
            liquidation_side: "long" or "short" — which side got liquidated
            atr: Current ATR value
            timestamp: Candle timestamp (ms)

        Returns:
            SignalResult
        """
        result = SignalResult(symbol=symbol)

        if atr <= 0 or len(candles) < 3:
            result.reasons.append("INSUFFICIENT_DATA")
            return result

        # ── Cooldown ──────────────────────────────
        last_ts = self._last_signal_ts.get(symbol, 0)
        if timestamp > 0 and last_ts > 0 and (timestamp - last_ts) < self.cooldown_ms:
            result.reasons.append("CASCADE_COOLDOWN")
            return result

        # ── Spike check ───────────────────────────
        if liquidation_volume_usd < self.spike_threshold_usd:
            result.reasons.append(
                f"LOW_LIQ_VOL({liquidation_volume_usd:.0f}<{self.spike_threshold_usd:.0f})"
            )
            return result

        # ── Determine mode ────────────────────────
        velocity = self._price_velocity(candles, current_price)
        is_exhaustion = liquidation_volume_usd >= (
            self.spike_threshold_usd * self.exhaustion_mult
        )
        has_wick = self._has_rejection_wick(candles[-1], liquidation_side)

        mode: Optional[str] = None

        if is_exhaustion and has_wick:
            mode = "EXHAUSTION"
        elif abs(velocity) >= self.momentum_velocity_min:
            mode = "MOMENTUM"
        else:
            result.reasons.append(
                f"NO_MODE(vel={velocity:.5f},exhaustion={is_exhaustion},wick={has_wick})"
            )
            return result

        # ── Direction ─────────────────────────────
        if mode == "MOMENTUM":
            # Trade WITH cascade: longs liquidated → price going down → SHORT
            direction = (
                SignalDirection.SHORT
                if liquidation_side == "long"
                else SignalDirection.LONG
            )
        else:  # EXHAUSTION
            # Trade the reversal: longs liquidated → bounce → LONG
            direction = (
                SignalDirection.LONG
                if liquidation_side == "long"
                else SignalDirection.SHORT
            )

        # ── Levels ────────────────────────────────
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
        vol_strength = min(
            liquidation_volume_usd / self.spike_threshold_usd, 5.0
        ) / 5.0
        vel_strength = min(abs(velocity) / self.momentum_velocity_min, 3.0) / 3.0

        if mode == "MOMENTUM":
            confidence = 0.35 + 0.35 * vol_strength + 0.30 * vel_strength
        else:
            wick_str = self._wick_strength(candles[-1], liquidation_side)
            confidence = 0.30 + 0.40 * vol_strength + 0.30 * min(wick_str, 1.0)

        confidence *= self.risk_factor

        signal = SMCSignal(
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type=f"LIQ_CASCADE_{mode}",
            trigger_type=f"LIQ_{liquidation_side.upper()}_{liquidation_volume_usd:.0f}USD",
            confidence_score=confidence,
            risk_reward_ratio=rr,
            invalidation_level=sl,
            mtf_bias=f"cascade_{mode.lower()}",
            timestamp=timestamp,
            timeframe="event",
        )

        result.signal = signal
        result.status = "CREATED"
        self._last_signal_ts[symbol] = timestamp

        logger.info(
            f"[LIQ_CASCADE] {symbol} | {mode}"
            f" | dir={direction.value}"
            f" | liq_side={liquidation_side}"
            f" | liq_vol=${liquidation_volume_usd:,.0f}"
            f" | velocity={velocity:.5f}"
            f" | rr={rr:.2f}"
            f" | conf={confidence:.2f}"
        )

        return result

    # ──────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────
    @staticmethod
    def _price_velocity(candles: List[Dict], current_price: float) -> float:
        """Return relative price change over recent candles."""
        if len(candles) < 2:
            return 0.0
        ref_price = float(candles[-min(5, len(candles))].get("close", current_price))
        if ref_price <= 0:
            return 0.0
        return (current_price - ref_price) / ref_price

    def _has_rejection_wick(self, candle: Dict, liq_side: str) -> bool:
        """Check if last candle shows rejection wick opposite to liquidation side."""
        o, h, l, c = (
            float(candle.get("open", 0)),
            float(candle.get("high", 0)),
            float(candle.get("low", 0)),
            float(candle.get("close", 0)),
        )
        body = abs(c - o) or 1e-12
        if liq_side == "long":
            # Longs liquidated → price dropped → look for lower wick (bounce)
            lower_wick = min(o, c) - l
            return (lower_wick / body) >= self.reversal_wick_ratio
        else:
            # Shorts liquidated → price spiked → look for upper wick (rejection)
            upper_wick = h - max(o, c)
            return (upper_wick / body) >= self.reversal_wick_ratio

    @staticmethod
    def _wick_strength(candle: Dict, liq_side: str) -> float:
        """Return normalized wick-to-body ratio for confidence calc."""
        o, h, l, c = (
            float(candle.get("open", 0)),
            float(candle.get("high", 0)),
            float(candle.get("low", 0)),
            float(candle.get("close", 0)),
        )
        body = abs(c - o) or 1e-12
        if liq_side == "long":
            return (min(o, c) - l) / body
        else:
            return (h - max(o, c)) / body
