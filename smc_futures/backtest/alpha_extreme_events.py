"""
Chantier 12 — Alpha Class 3: EXTREME EVENTS
=============================================

Core thesis:
  - Extreme moves (≥3 ATR) create regime shifts
  - Liquidation cascades cause overshoots
  - Post-extreme: high probability of continuation OR reversion
  - Both patterns capture ≥500 bps moves
  - Very low frequency, very high conviction

Two sub-modes:
  A) EXTREME CONTINUATION: Momentum continues after extreme bar
  B) EXTREME REVERSION: Mean reversion after overshoot/exhaustion
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Callable

from smc_futures.backtest.engine import BTCandle, BacktestSignal

# ═══════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════

@dataclass
class ExtremeEventsConfig:
    # ── Extreme Detection ─────────────────────
    atr_period: int = 14
    extreme_atr_mult: float = 2.0       # Bar > 2 ATR = extreme
    volume_surge_mult: float = 1.5      # Volume > 1.5x avg required
    volume_lookback: int = 20
    extreme_body_ratio: float = 0.45    # Body > 45% of range (conviction)

    # ── Continuation Mode ─────────────────────
    continuation_confirm_bars: int = 2  # Wait 2 bars after extreme
    continuation_min_followthrough: float = 0.3  # Next bars must move ≥30% of extreme bar range
    continuation_trend_ema: int = 50    # Extreme must be in trend direction
    continuation_sl_atr: float = 2.0    # SL = 2 ATR
    continuation_target_mult: float = 3.0  # TP = extreme range x 3

    # ── Reversion Mode ────────────────────────
    reversion_exhaustion_bars: int = 2  # Wait for exhaustion signal
    reversion_body_shrink: float = 0.6  # Bodies must shrink (exhaustion)
    reversion_wick_ratio: float = 0.4   # Wicks > 40% (rejection)
    reversion_sl_atr: float = 1.5      # Tight SL beyond extreme
    reversion_target_atr: float = 6.0  # TP = 6 ATR (mean reversion target)

    # ── Liquidation Cascade Detection ─────────
    cascade_min_bars: int = 2           # Multiple extreme bars = cascade
    cascade_acceleration: float = 1.2   # Each bar > 1.2x previous
    cascade_cooldown_bars: int = 5      # Wait after cascade ends

    # ── Risk Management ───────────────────────
    min_target_bps: float = 150.0       # 150 bps minimum for extreme events
    min_rr: float = 1.5
    min_atr_bps: float = 5.0
    max_atr_bps: float = 1200.0        # Higher tolerance for extreme volatility
    min_bars_between: int = 30         # Conservative cooldown

    # ── Mode Selection ────────────────────────
    enable_continuation: bool = True
    enable_reversion: bool = True


# ═══════════════════════════════════════════════════
#  Indicators
# ═══════════════════════════════════════════════════

def _ema(values: List[float], period: int) -> List[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return [float("nan")] * len(values)
    result = [float("nan")] * (period - 1)
    sma = sum(values[:period]) / period
    result.append(sma)
    mult = 2.0 / (period + 1)
    for i in range(period, len(values)):
        sma = (values[i] - sma) * mult + sma
        result.append(sma)
    return result


def _atr(bars: List[BTCandle], period: int) -> List[float]:
    """Average True Range."""
    if len(bars) < 2:
        return [0.0] * len(bars)
    trs = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        trs.append(tr)
    return _ema(trs, period)


# ═══════════════════════════════════════════════════
#  Extreme Bar Detection
# ═══════════════════════════════════════════════════

def _find_extreme_bar(
    bars: List[BTCandle],
    atr_vals: List[float],
    cfg: ExtremeEventsConfig,
    scan_start: int,
    scan_end: int,
) -> Optional[int]:
    """
    Find the most recent extreme bar (range > N × ATR + volume surge).
    Returns index or None.
    """
    for j in range(scan_end, scan_start - 1, -1):
        if j < 1 or j >= len(bars):
            continue
        atr = atr_vals[j]
        if math.isnan(atr) or atr <= 0:
            continue

        bar_range = bars[j].high - bars[j].low
        if bar_range < cfg.extreme_atr_mult * atr:
            continue

        # Body ratio
        body = abs(bars[j].close - bars[j].open)
        if bar_range > 0 and body / bar_range < cfg.extreme_body_ratio:
            continue

        # Volume surge
        if j > cfg.volume_lookback:
            avg_vol = sum(b.volume for b in bars[j - cfg.volume_lookback:j]) / cfg.volume_lookback
            if avg_vol > 0 and bars[j].volume < cfg.volume_surge_mult * avg_vol:
                continue

        return j

    return None


def _detect_cascade(
    bars: List[BTCandle],
    atr_vals: List[float],
    extreme_idx: int,
    cfg: ExtremeEventsConfig,
) -> int:
    """
    Detect liquidation cascade (multiple extreme bars in same direction).
    Returns number of cascade bars.
    """
    direction = "up" if bars[extreme_idx].close > bars[extreme_idx].open else "down"
    cascade_count = 1
    prev_range = bars[extreme_idx].high - bars[extreme_idx].low

    for j in range(extreme_idx - 1, max(extreme_idx - 10, -1), -1):
        if j < 0:
            break
        bar_range = bars[j].high - bars[j].low
        atr = atr_vals[j]
        if math.isnan(atr) or atr <= 0:
            break

        # Must be extreme AND accelerating in same direction
        is_same_dir = (
            (direction == "up" and bars[j].close > bars[j].open) or
            (direction == "down" and bars[j].close < bars[j].open)
        )
        is_extreme = bar_range >= cfg.extreme_atr_mult * atr * 0.8  # Slightly relaxed
        if is_same_dir and is_extreme:
            cascade_count += 1
            prev_range = bar_range
        else:
            break

    return cascade_count


# ═══════════════════════════════════════════════════
#  Continuation Mode
# ═══════════════════════════════════════════════════

def _check_continuation(
    bars: List[BTCandle],
    atr_vals: List[float],
    ema_trend: List[float],
    extreme_idx: int,
    cfg: ExtremeEventsConfig,
    current_idx: int,
) -> Optional[BacktestSignal]:
    """
    After extreme bar, check if momentum continues.
    Enter in same direction as extreme move.
    """
    if not cfg.enable_continuation:
        return None

    extreme_bar = bars[extreme_idx]
    extreme_direction = "long" if extreme_bar.close > extreme_bar.open else "short"
    extreme_range = extreme_bar.high - extreme_bar.low
    bars_since = current_idx - extreme_idx

    if bars_since < cfg.continuation_confirm_bars:
        return None
    if bars_since > cfg.continuation_confirm_bars + 3:
        return None  # Too late

    # Trend alignment
    if not math.isnan(ema_trend[current_idx]):
        price = bars[current_idx].close
        if extreme_direction == "long" and price < ema_trend[current_idx]:
            return None
        if extreme_direction == "short" and price > ema_trend[current_idx]:
            return None

    # Follow-through: post-extreme bars must continue in direction
    follow_through = 0.0
    for j in range(extreme_idx + 1, min(current_idx + 1, len(bars))):
        if extreme_direction == "long":
            follow_through += bars[j].close - bars[j].open
        else:
            follow_through += bars[j].open - bars[j].close

    if follow_through < cfg.continuation_min_followthrough * extreme_range:
        return None

    # Compute SL/TP
    price = bars[current_idx].close
    atr = atr_vals[current_idx]

    if extreme_direction == "long":
        sl = min(extreme_bar.low, price - cfg.continuation_sl_atr * atr)
        tp = price + extreme_range * cfg.continuation_target_mult
    else:
        sl = max(extreme_bar.high, price + cfg.continuation_sl_atr * atr)
        tp = price - extreme_range * cfg.continuation_target_mult

    sl_dist = abs(price - sl)
    tp_dist = abs(tp - price)
    if sl_dist <= 0:
        return None

    target_bps = tp_dist / price * 10000
    rr = tp_dist / sl_dist

    if target_bps < cfg.min_target_bps or rr < cfg.min_rr:
        return None

    return BacktestSignal(
        symbol="BTCUSDT",
        direction=extreme_direction,
        entry_price=price,
        stop_loss=sl,
        take_profit=tp,
        setup_type=f"extreme_continuation_{extreme_direction}",
        confidence=min(0.9, 0.6 + (extreme_range / atr / 10.0)),
        risk_reward=rr,
        timestamp=bars[current_idx].timestamp,
    )


# ═══════════════════════════════════════════════════
#  Reversion Mode
# ═══════════════════════════════════════════════════

def _check_reversion(
    bars: List[BTCandle],
    atr_vals: List[float],
    extreme_idx: int,
    cfg: ExtremeEventsConfig,
    current_idx: int,
) -> Optional[BacktestSignal]:
    """
    After extreme bar, check for exhaustion and reversion setup.
    Enter AGAINST the extreme move direction.
    """
    if not cfg.enable_reversion:
        return None

    extreme_bar = bars[extreme_idx]
    extreme_direction = "up" if extreme_bar.close > extreme_bar.open else "down"
    bars_since = current_idx - extreme_idx

    if bars_since < cfg.reversion_exhaustion_bars:
        return None
    if bars_since > cfg.reversion_exhaustion_bars + 5:
        return None

    # Exhaustion detection: bodies shrinking, wicks growing
    exhaustion_count = 0
    for j in range(extreme_idx + 1, min(current_idx + 1, len(bars))):
        bar = bars[j]
        body = abs(bar.close - bar.open)
        full_range = bar.high - bar.low
        if full_range <= 0:
            continue

        body_ratio = body / full_range
        if body_ratio < cfg.reversion_body_shrink:
            exhaustion_count += 1

        # Check for rejection wicks
        if extreme_direction == "up":
            upper_wick = bar.high - max(bar.close, bar.open)
            if upper_wick / full_range > cfg.reversion_wick_ratio:
                exhaustion_count += 1
        else:
            lower_wick = min(bar.close, bar.open) - bar.low
            if lower_wick / full_range > cfg.reversion_wick_ratio:
                exhaustion_count += 1

    if exhaustion_count < 2:
        return None

    # Reversion direction is opposite to extreme
    reversion_dir = "short" if extreme_direction == "up" else "long"

    # Current bar should confirm reversion
    current_bar = bars[current_idx]
    if reversion_dir == "long" and current_bar.close <= current_bar.open:
        return None
    if reversion_dir == "short" and current_bar.close >= current_bar.open:
        return None

    # Compute SL/TP
    price = bars[current_idx].close
    atr = atr_vals[current_idx]
    if math.isnan(atr) or atr <= 0:
        return None

    if reversion_dir == "long":
        sl = min(b.low for b in bars[extreme_idx:current_idx + 1]) - cfg.reversion_sl_atr * atr * 0.3
        tp = price + cfg.reversion_target_atr * atr
    else:
        sl = max(b.high for b in bars[extreme_idx:current_idx + 1]) + cfg.reversion_sl_atr * atr * 0.3
        tp = price - cfg.reversion_target_atr * atr

    sl_dist = abs(price - sl)
    tp_dist = abs(tp - price)
    if sl_dist <= 0:
        return None

    target_bps = tp_dist / price * 10000
    rr = tp_dist / sl_dist

    if target_bps < cfg.min_target_bps or rr < cfg.min_rr:
        return None

    return BacktestSignal(
        symbol="BTCUSDT",
        direction=reversion_dir,
        entry_price=price,
        stop_loss=sl,
        take_profit=tp,
        setup_type=f"extreme_reversion_{reversion_dir}",
        confidence=min(0.85, 0.5 + (exhaustion_count / 6.0)),
        risk_reward=rr,
        timestamp=bars[current_idx].timestamp,
    )


# ═══════════════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════════════

def make_extreme_events_fn(
    cfg: Optional[ExtremeEventsConfig] = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Factory: returns a signal function for the Extreme Events alpha.

    Logic:
      1. Detect extreme bars (>3 ATR + volume surge)
      2. Detect liquidation cascades (accelerating extreme moves)
      3. After extreme: check continuation (same direction) OR reversion (opposite)
      4. Enter on confirmation with structural SL/TP
    """
    if cfg is None:
        cfg = ExtremeEventsConfig()

    last_signal_bar = [-1]

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        n = len(closed_bars)
        min_bars = max(cfg.atr_period, cfg.continuation_trend_ema, cfg.volume_lookback) + 20
        if n < min_bars:
            return None

        # Cooldown
        if last_signal_bar[0] >= 0 and (n - last_signal_bar[0]) < cfg.min_bars_between:
            return None

        i = n - 1
        price = closed_bars[i].close
        if price <= 0:
            return None

        # ── Compute indicators ──────────────
        closes = [b.close for b in closed_bars]
        atr_vals = _atr(closed_bars, cfg.atr_period)
        ema_trend = _ema(closes, cfg.continuation_trend_ema)

        atr = atr_vals[i]
        if math.isnan(atr) or atr <= 0:
            return None

        # Hard filter: volatility range
        atr_bps = atr / price * 10000
        if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
            return None

        # ── Find recent extreme bar ─────────
        scan_end = i - 1  # Don't count current bar
        scan_start = max(0, i - 15)  # Look back up to 15 bars

        extreme_idx = _find_extreme_bar(closed_bars, atr_vals, cfg, scan_start, scan_end)
        if extreme_idx is None:
            return None

        # ── Check for cascade ───────────────
        cascade_bars = _detect_cascade(closed_bars, atr_vals, extreme_idx, cfg)
        is_cascade = cascade_bars >= cfg.cascade_min_bars

        # If cascade, wait for cooldown
        if is_cascade:
            cascade_end = extreme_idx
            if i - cascade_end < cfg.cascade_cooldown_bars:
                return None

        # ── Try continuation first ──────────
        signal = _check_continuation(
            closed_bars, atr_vals, ema_trend, extreme_idx, cfg, i
        )

        # ── Try reversion if no continuation ─
        if signal is None:
            signal = _check_reversion(
                closed_bars, atr_vals, extreme_idx, cfg, i
            )

        if signal is not None:
            # Final hard filter
            tp_dist = abs(signal.take_profit - signal.entry_price)
            target_bps = tp_dist / signal.entry_price * 10000
            if target_bps < 100:
                return None

            last_signal_bar[0] = n

        return signal

    return signal_fn
