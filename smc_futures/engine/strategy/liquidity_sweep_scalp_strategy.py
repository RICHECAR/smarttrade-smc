"""
Liquidity Sweep Scalping Strategy
══════════════════════════════════
Fast-execution strategy that enters immediately after a liquidity sweep
(stop hunt) is confirmed by a rejection wick.

  Equal highs swept + rejection wick → SHORT scalp
  Equal lows  swept + rejection wick → LONG  scalp

TP is tight (0.5×ATR) for rapid profit capture.
Designed for high-frequency execution (5-20 trades/day).
"""

import logging
from typing import Any, Dict, List, Optional

from smc_futures.engine.strategy.signal_builder import (
    SignalDirection,
    SignalResult,
    SMCSignal,
)

logger = logging.getLogger(__name__)


class LiquiditySweepScalpStrategy:
    """
    Scalping entries on liquidity sweeps detected by the LiquidityEngine.
    Uses wick rejection as confirmation then targets 0.5×ATR.
    """

    def __init__(
        self,
        tp_atr_mult: float = 0.5,
        sl_atr_mult: float = 0.8,
        min_wick_ratio: float = 0.6,
        min_rr: float = 0.6,
        risk_factor: float = 0.4,
        cooldown_candles: int = 3,
    ):
        """
        Args:
            tp_atr_mult: TP distance in ATR (tight for scalping)
            sl_atr_mult: SL distance in ATR
            min_wick_ratio: Minimum wick-to-body ratio to confirm rejection
            min_rr: Minimum R:R (can be lower for scalps)
            risk_factor: Position size relative to normal (smaller for scalps)
            cooldown_candles: Candles to wait between scalp signals
        """
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.min_wick_ratio = min_wick_ratio
        self.min_rr = min_rr
        self.risk_factor = risk_factor
        self.cooldown_candles = cooldown_candles
        self._last_signal_ts: Dict[str, int] = {}

    def build_signal(
        self,
        symbol: str,
        current_price: float,
        candles: List[Dict],
        liquidity_state: Dict[str, Any],
        atr: float,
        timestamp: int = 0,
    ) -> SignalResult:
        """
        Evaluate recent liquidity sweep and wick rejection.

        Args:
            symbol: Trading pair
            current_price: Last close
            candles: Recent LTF candles (5m) — at least 5
            liquidity_state: From LiquidityEngine.get_current_state()
            atr: Current ATR value
            timestamp: Candle timestamp (ms)

        Returns:
            SignalResult
        """
        result = SignalResult(symbol=symbol)

        if atr <= 0 or len(candles) < 3:
            result.reasons.append("INSUFFICIENT_DATA")
            return result

        # ── Cooldown check ────────────────────────
        last_ts = self._last_signal_ts.get(symbol, 0)
        if timestamp > 0 and last_ts > 0:
            candle_interval = self._estimate_interval(candles)
            if candle_interval > 0 and (timestamp - last_ts) < candle_interval * self.cooldown_candles:
                result.reasons.append("SCALP_COOLDOWN")
                return result

        # ── Detect recent sweep ───────────────────
        sweep = self._detect_sweep(liquidity_state, candles)
        if not sweep:
            result.reasons.append("NO_SWEEP_DETECTED")
            return result

        sweep_side = sweep["side"]
        sweep_candle = sweep["candle"]

        # ── Check rejection wick ──────────────────
        if not self._has_rejection_wick(sweep_candle, sweep_side):
            result.reasons.append("NO_REJECTION_WICK")
            return result

        # ── Direction: sweep high → short, sweep low → long ──
        if sweep_side == "high":
            direction = SignalDirection.SHORT
        else:
            direction = SignalDirection.LONG

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
        wick_strength = self._wick_strength(sweep_candle, sweep_side)
        confidence = (0.4 + 0.6 * min(wick_strength, 1.0)) * self.risk_factor

        signal = SMCSignal(
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="LIQUIDITY_SWEEP_SCALP",
            trigger_type=f"SWEEP_{sweep_side.upper()}",
            confidence_score=confidence,
            risk_reward_ratio=rr,
            invalidation_level=sl,
            mtf_bias="sweep_rejection",
            timestamp=timestamp,
            timeframe="5m",
        )

        result.signal = signal
        result.status = "CREATED"
        self._last_signal_ts[symbol] = timestamp

        logger.info(
            f"[SWEEP_SCALP] {symbol} | SIGNAL"
            f" | dir={direction.value}"
            f" | sweep_side={sweep_side}"
            f" | rr={rr:.2f}"
            f" | conf={confidence:.2f}"
            f" | wick_ratio={wick_strength:.2f}"
        )

        return result

    # ── Internal helpers ──────────────────────────

    def _detect_sweep(
        self, liquidity_state: Dict[str, Any], candles: List[Dict]
    ) -> Optional[Dict]:
        """Check if a liquidity sweep happened on the last 1-2 candles."""
        recent_sweeps = liquidity_state.get("recent_sweeps", [])
        if recent_sweeps:
            sweep = recent_sweeps[-1]
            return {
                "side": sweep.get("side", sweep.get("type", "high")),
                "candle": candles[-1],
            }

        # Fallback: check equal-high / equal-low clusters via heatmap
        heatmap = liquidity_state.get("heatmap", {})
        levels = heatmap.get("levels", [])
        if not levels or len(candles) < 2:
            return None

        last = candles[-1]
        high = float(last.get("high", 0))
        low = float(last.get("low", 0))
        close = float(last.get("close", 0))

        for lv in levels:
            lv_price = float(lv.get("price", 0))
            lv_score = float(lv.get("score", 0))
            if lv_score < 3.0:
                continue

            # Swept above a high liquidity level then closed back below
            if high > lv_price > close and (high - lv_price) / max(high - low, 1e-10) < 0.4:
                return {"side": "high", "candle": last}

            # Swept below a low liquidity level then closed back above
            if low < lv_price < close and (lv_price - low) / max(high - low, 1e-10) < 0.4:
                return {"side": "low", "candle": last}

        return None

    def _has_rejection_wick(self, candle: Dict, sweep_side: str) -> bool:
        """Check if the candle has a rejection wick after sweep."""
        o = float(candle.get("open", 0))
        h = float(candle.get("high", 0))
        l = float(candle.get("low", 0))
        c = float(candle.get("close", 0))

        body = abs(c - o)
        full = h - l
        if full <= 0:
            return False

        if sweep_side == "high":
            upper_wick = h - max(o, c)
            return (upper_wick / full) >= self.min_wick_ratio
        else:
            lower_wick = min(o, c) - l
            return (lower_wick / full) >= self.min_wick_ratio

    def _wick_strength(self, candle: Dict, sweep_side: str) -> float:
        """Compute wick-to-range ratio as strength metric."""
        o = float(candle.get("open", 0))
        h = float(candle.get("high", 0))
        l = float(candle.get("low", 0))
        c = float(candle.get("close", 0))

        full = h - l
        if full <= 0:
            return 0.0

        if sweep_side == "high":
            return (h - max(o, c)) / full
        else:
            return (min(o, c) - l) / full

    def _estimate_interval(self, candles: List[Dict]) -> int:
        """Estimate candle interval in ms from timestamps."""
        if len(candles) < 2:
            return 300_000  # 5m default
        ts = [int(c.get("timestamp", 0)) for c in candles[-5:] if c.get("timestamp")]
        if len(ts) < 2:
            return 300_000
        diffs = [ts[i] - ts[i - 1] for i in range(1, len(ts)) if ts[i] > ts[i - 1]]
        return int(sum(diffs) / len(diffs)) if diffs else 300_000
