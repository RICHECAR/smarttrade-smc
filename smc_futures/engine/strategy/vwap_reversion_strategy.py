"""
VWAP Mean Reversion Strategy
═════════════════════════════
Generates mean-reversion signals when price deviates significantly
from the Volume-Weighted Average Price (VWAP).

  price >> VWAP + 1.5×ATR  → SHORT (revert down)
  price << VWAP - 1.5×ATR  → LONG  (revert up)

Best suited for RANGE / LOW_VOL regimes.
"""

import logging
from typing import Dict, List, Optional

from smc_futures.engine.strategy.signal_builder import (
    SignalDirection,
    SignalResult,
    SMCSignal,
)

logger = logging.getLogger(__name__)


class VWAPReversionStrategy:
    """
    Mean-reversion entries based on ATR-normalized distance from VWAP.
    """

    def __init__(
        self,
        distance_atr_mult: float = 1.5,
        sl_atr_mult: float = 1.0,
        tp_mode: str = "vwap",
        min_rr: float = 1.2,
        risk_factor: float = 0.65,
    ):
        """
        Args:
            distance_atr_mult: Minimum price-VWAP distance (in ATR units) to trigger
            sl_atr_mult: SL distance in ATR units beyond entry
            tp_mode: "vwap" targets VWAP, "atr" uses fixed ATR multiple
            min_rr: Minimum risk-reward ratio
            risk_factor: Confidence scaling factor (0-1)
        """
        self.distance_atr_mult = distance_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.tp_mode = tp_mode
        self.min_rr = min_rr
        self.risk_factor = risk_factor

    def build_signal(
        self,
        symbol: str,
        current_price: float,
        vwap: float,
        atr: float,
        timestamp: int = 0,
    ) -> SignalResult:
        """
        Check if price is far enough from VWAP to enter a mean-reversion trade.

        Args:
            symbol: Trading pair
            current_price: Last close
            vwap: Current VWAP value (session or 24h)
            atr: Current ATR value
            timestamp: Candle timestamp (ms)

        Returns:
            SignalResult
        """
        result = SignalResult(symbol=symbol)

        if atr <= 0 or vwap <= 0:
            result.reasons.append("NO_ATR_OR_VWAP")
            return result

        distance = current_price - vwap
        distance_abs = abs(distance)
        threshold = atr * self.distance_atr_mult

        if distance_abs < threshold:
            result.reasons.append(
                f"VWAP_TOO_CLOSE(dist={distance_abs:.2f}_thresh={threshold:.2f})"
            )
            return result

        # ── Direction ─────────────────────────────
        if distance > 0:
            direction = SignalDirection.SHORT
        else:
            direction = SignalDirection.LONG

        # ── Levels ────────────────────────────────
        sl_dist = atr * self.sl_atr_mult

        if self.tp_mode == "vwap":
            tp_dist = distance_abs
        else:
            tp_dist = atr * 2.0

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
        deviation_strength = min(distance_abs / threshold, 3.0) / 3.0
        confidence = (0.5 + 0.5 * deviation_strength) * self.risk_factor

        signal = SMCSignal(
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="VWAP_REVERSION",
            trigger_type=f"VWAP_DIST={distance_abs:.2f}",
            confidence_score=confidence,
            risk_reward_ratio=rr,
            invalidation_level=sl,
            mtf_bias="vwap_deviation",
            timestamp=timestamp,
            timeframe="5m",
        )

        result.signal = signal
        result.status = "CREATED"

        logger.info(
            f"[VWAP_REV] {symbol} | SIGNAL"
            f" | dir={direction.value}"
            f" | price={current_price:.2f} vwap={vwap:.2f}"
            f" | dist_atr={distance_abs/atr:.2f}"
            f" | rr={rr:.2f}"
            f" | conf={confidence:.2f}"
        )

        return result


def compute_session_vwap(candles: List[Dict]) -> float:
    """
    Compute VWAP from candle history.
    Uses typical price × volume / cumulative volume.
    Falls back to simple average if volume missing.

    Args:
        candles: List of dicts with 'high', 'low', 'close', and optionally 'volume'

    Returns:
        VWAP value, or 0.0 if insufficient data
    """
    if not candles:
        return 0.0

    has_volume = all("volume" in c and float(c.get("volume", 0)) > 0 for c in candles)

    if has_volume:
        cum_vol = 0.0
        cum_pv = 0.0
        for c in candles:
            tp = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
            vol = float(c["volume"])
            cum_pv += tp * vol
            cum_vol += vol
        return cum_pv / cum_vol if cum_vol > 0 else 0.0
    else:
        # Fallback: simple typical price average
        tps = [
            (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
            for c in candles
        ]
        return sum(tps) / len(tps) if tps else 0.0
