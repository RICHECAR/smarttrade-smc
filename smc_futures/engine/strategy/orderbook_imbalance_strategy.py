"""
Order Book Imbalance Strategy
══════════════════════════════
Generates directional signals based on bid/ask depth imbalance
in the order book, a leading microstructure indicator.

  imbalance = bid_volume / (bid_volume + ask_volume)

  imbalance > 0.65  →  buy pressure   →  LONG
  imbalance < 0.35  →  sell pressure  →  SHORT

Confirmation via Cumulative Volume Delta (CVD) when available.
"""

import logging
from typing import Dict, List, Optional

from smc_futures.engine.strategy.signal_builder import (
    SignalDirection,
    SignalResult,
    SMCSignal,
)

logger = logging.getLogger(__name__)


class OrderBookImbalanceStrategy:
    """
    Directional entries based on real-time order book depth imbalance.
    Requires L2 order book snapshot (bid/ask depth arrays or aggregated volumes).
    """

    def __init__(
        self,
        imbalance_long_threshold: float = 0.65,
        imbalance_short_threshold: float = 0.35,
        depth_levels: int = 20,
        cvd_confirmation: bool = True,
        tp_atr_mult: float = 0.7,
        sl_atr_mult: float = 0.4,
        min_rr: float = 1.2,
        risk_factor: float = 0.50,
        min_depth_usd: float = 100_000,
        cooldown_ms: int = 60_000,
    ):
        """
        Args:
            imbalance_long_threshold: Imbalance ratio above which → LONG signal
            imbalance_short_threshold: Imbalance ratio below which → SHORT signal
            depth_levels: Number of order book levels to aggregate
            cvd_confirmation: Require CVD alignment for confirmation
            tp_atr_mult: TP distance in ATR units
            sl_atr_mult: SL distance in ATR units
            min_rr: Minimum risk-reward ratio
            risk_factor: Confidence scaling (0-1)
            min_depth_usd: Minimum total depth (bid+ask USD) to consider signal valid
            cooldown_ms: Min interval between signals per symbol (ms)
        """
        self.imbalance_long = imbalance_long_threshold
        self.imbalance_short = imbalance_short_threshold
        self.depth_levels = depth_levels
        self.cvd_confirmation = cvd_confirmation
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.min_rr = min_rr
        self.risk_factor = risk_factor
        self.min_depth_usd = min_depth_usd
        self.cooldown_ms = cooldown_ms
        self._last_signal_ts: Dict[str, int] = {}

    # ──────────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────────
    def build_signal(
        self,
        symbol: str,
        current_price: float,
        bid_volume: float,
        ask_volume: float,
        atr: float,
        timestamp: int = 0,
        cvd: Optional[float] = None,
        price_near_support: bool = False,
        price_near_resistance: bool = False,
    ) -> SignalResult:
        """
        Evaluate order book imbalance for entry.

        Args:
            symbol: Trading pair
            current_price: Last price
            bid_volume: Total bid depth (USD or base) across depth_levels
            ask_volume: Total ask depth (USD or base) across depth_levels
            atr: Current ATR value
            timestamp: Candle / snapshot timestamp (ms)
            cvd: Cumulative Volume Delta (positive = net buying). None to skip.
            price_near_support: True if price is near a known support level
            price_near_resistance: True if price is near a known resistance level

        Returns:
            SignalResult
        """
        result = SignalResult(symbol=symbol)

        if atr <= 0:
            result.reasons.append("NO_ATR_DATA")
            return result

        # ── Cooldown ──────────────────────────────
        last_ts = self._last_signal_ts.get(symbol, 0)
        if timestamp > 0 and last_ts > 0 and (timestamp - last_ts) < self.cooldown_ms:
            result.reasons.append("OBI_COOLDOWN")
            return result

        # ── Depth sanity check ────────────────────
        total_depth = bid_volume + ask_volume
        if total_depth < self.min_depth_usd:
            result.reasons.append(
                f"THIN_BOOK({total_depth:.0f}<{self.min_depth_usd:.0f})"
            )
            return result

        # ── Imbalance ratio ───────────────────────
        imbalance = bid_volume / total_depth  # 0..1

        direction: Optional[SignalDirection] = None

        if imbalance >= self.imbalance_long:
            direction = SignalDirection.LONG
        elif imbalance <= self.imbalance_short:
            direction = SignalDirection.SHORT
        else:
            result.reasons.append(
                f"NEUTRAL_BOOK(imb={imbalance:.3f},thresholds=[{self.imbalance_short},{self.imbalance_long}])"
            )
            return result

        # ── CVD confirmation (optional) ───────────
        if self.cvd_confirmation and cvd is not None:
            if direction == SignalDirection.LONG and cvd < 0:
                result.reasons.append(f"CVD_DISAGREES(cvd={cvd:.0f},dir=LONG)")
                return result
            if direction == SignalDirection.SHORT and cvd > 0:
                result.reasons.append(f"CVD_DISAGREES(cvd={cvd:.0f},dir=SHORT)")
                return result

        # ── S/R confluence bonus ──────────────────
        sr_bonus = 0.0
        if direction == SignalDirection.LONG and price_near_support:
            sr_bonus = 0.10
        elif direction == SignalDirection.SHORT and price_near_resistance:
            sr_bonus = 0.10

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
        #  Strength of imbalance beyond threshold
        if direction == SignalDirection.LONG:
            imb_excess = (imbalance - self.imbalance_long) / (1.0 - self.imbalance_long)
        else:
            imb_excess = (self.imbalance_short - imbalance) / self.imbalance_short

        imb_strength = min(max(imb_excess, 0.0), 1.0)

        # CVD contribution
        cvd_strength = 0.0
        if cvd is not None and total_depth > 0:
            cvd_strength = min(abs(cvd) / total_depth, 1.0)

        confidence = (
            0.35
            + 0.35 * imb_strength
            + 0.20 * cvd_strength
            + sr_bonus
        ) * self.risk_factor

        signal = SMCSignal(
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="ORDERBOOK_IMBALANCE",
            trigger_type=f"OBI={imbalance:.3f}_CVD={cvd:.0f}" if cvd is not None else f"OBI={imbalance:.3f}",
            confidence_score=confidence,
            risk_reward_ratio=rr,
            invalidation_level=sl,
            mtf_bias="book_pressure",
            timestamp=timestamp,
            timeframe="tick",
        )

        result.signal = signal
        result.status = "CREATED"
        self._last_signal_ts[symbol] = timestamp

        logger.info(
            f"[OBI] {symbol} | SIGNAL"
            f" | dir={direction.value}"
            f" | imbalance={imbalance:.3f}"
            f" | bids={bid_volume:.0f} asks={ask_volume:.0f}"
            f" | cvd={cvd:.0f}" if cvd is not None else ""
            f" | rr={rr:.2f}"
            f" | conf={confidence:.2f}"
        )

        return result
