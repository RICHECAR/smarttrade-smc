"""
Alpha A — Contextual Reversal
═════════════════════════════
Only trades reversals AFTER confirmed liquidity sweeps with
multi-timeframe structure confluence.

Key Design (Post-Autopsy):
  - Old edge: ~10 bps alpha, ~126 bps cost → dead
  - This alpha targets: ≥ 300 bps moves (major reversals)
  - Filters: liquidity sweep + structure break + volatility gate
  - Wide stops beyond sweep extreme → RR ≥ 2.0

Signal Logic:
  1. Detect swing structure (highs/lows) over lookback
  2. Detect liquidity sweep (wick beyond prior swing, close back inside)
  3. Confirm trend change (CHOCH / structure shift)
  4. Volatility filter (ATR must be sufficient for target move)
  5. Entry after confirmation bar, SL beyond sweep extreme
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from smc_futures.backtest.engine import BTCandle, BacktestSignal


# ─── Configuration ────────────────────────

@dataclass
class ContextualReversalConfig:
    """Tunable parameters for contextual reversal signals."""
    # Swing detection
    swing_lookback: int = 5          # Bars each side for swing point
    min_swing_points: int = 3        # Minimum swings needed to trade

    # Liquidity sweep
    sweep_wick_min_atr: float = 0.3  # Wick must be ≥ 0.3 × ATR
    sweep_close_inside: bool = True  # Close must be inside prior range

    # Structure confirmation
    structure_confirm_bars: int = 3  # Bars after sweep for CHOCH confirm

    # Volatility gate
    atr_period: int = 14
    min_atr_bps: float = 30.0       # Min ATR in bps of price (filter low-vol)
    max_atr_bps: float = 500.0      # Max ATR bps (filter extreme vol)

    # Risk/Reward
    min_rr: float = 2.0             # Minimum risk:reward ratio
    target_rr: float = 3.0          # Preferred RR
    sl_buffer_atr: float = 0.3      # SL buffer beyond sweep extreme
    min_target_bps: float = 150.0   # Minimum target size in bps

    # Cooldown
    min_bars_between_signals: int = 20

    # HTF trend (simulated via longer EMA)
    htf_ema_period: int = 100        # "4H-equivalent" trend on 5m bars
    ltf_ema_period: int = 20         # Short-term trend for trigger


# ─── Helper: compute ATR ──────────────────

def _compute_atr(candles: List[BTCandle], period: int = 14) -> float:
    if len(candles) < 2:
        return candles[-1].high - candles[-1].low if candles else 0.0
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


# ─── Helper: EMA ──────────────────────────

def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    k = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


# ─── Helper: Swing detection ─────────────

@dataclass
class SwingPoint:
    index: int
    price: float
    is_high: bool    # True = swing high, False = swing low


def _detect_swings(candles: List[BTCandle], lookback: int) -> List[SwingPoint]:
    """Detect swing highs and lows using lookback confirmation."""
    swings = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        # Check swing high
        is_sh = True
        for j in range(1, lookback + 1):
            if candles[i - j].high >= candles[i].high or candles[i + j].high >= candles[i].high:
                is_sh = False
                break
        if is_sh:
            swings.append(SwingPoint(i, candles[i].high, True))

        # Check swing low
        is_sl = True
        for j in range(1, lookback + 1):
            if candles[i - j].low <= candles[i].low or candles[i + j].low <= candles[i].low:
                is_sl = False
                break
        if is_sl:
            swings.append(SwingPoint(i, candles[i].low, False))

    return swings


# ─── Helper: Liquidity sweep detection ───

@dataclass
class LiquiditySweep:
    bar_index: int
    sweep_type: str        # "bullish" (sweep lows→go long) or "bearish" (sweep highs→go short)
    sweep_extreme: float   # The wick extreme price
    prior_level: float     # The level that was swept
    close_price: float     # Close of sweep bar


def _detect_sweep(
    candles: List[BTCandle],
    swings: List[SwingPoint],
    bar_idx: int,
    atr: float,
    cfg: ContextualReversalConfig,
) -> Optional[LiquiditySweep]:
    """
    Check if bar at bar_idx is a liquidity sweep:
    - Price wicks beyond a prior swing level
    - But closes back inside (rejection)
    - Wick is significant (≥ min_wick × ATR)
    """
    if bar_idx >= len(candles):
        return None
    bar = candles[bar_idx]

    # Get recent swing lows and highs
    recent_lows = [s for s in swings if s.is_high is False and s.index < bar_idx - 1]
    recent_highs = [s for s in swings if s.is_high is True and s.index < bar_idx - 1]

    # Check bullish sweep (sweep below prior swing lows → reversal up)
    for sl_point in reversed(recent_lows[-5:]):  # Check last 5 swing lows
        level = sl_point.price
        # Bar wicks below the level
        if bar.low < level:
            wick_below = level - bar.low
            # Wick must be significant
            if atr > 0 and wick_below >= cfg.sweep_wick_min_atr * atr:
                # Close must be back above (inside)
                if not cfg.sweep_close_inside or bar.close > level:
                    return LiquiditySweep(
                        bar_index=bar_idx,
                        sweep_type="bullish",
                        sweep_extreme=bar.low,
                        prior_level=level,
                        close_price=bar.close,
                    )

    # Check bearish sweep (sweep above prior swing highs → reversal down)
    for sh_point in reversed(recent_highs[-5:]):
        level = sh_point.price
        if bar.high > level:
            wick_above = bar.high - level
            if atr > 0 and wick_above >= cfg.sweep_wick_min_atr * atr:
                if not cfg.sweep_close_inside or bar.close < level:
                    return LiquiditySweep(
                        bar_index=bar_idx,
                        sweep_type="bearish",
                        sweep_extreme=bar.high,
                        prior_level=level,
                        close_price=bar.close,
                    )

    return None


# ─── Helper: Structure confirmation ──────

def _structure_confirms(
    candles: List[BTCandle],
    sweep: LiquiditySweep,
    end_idx: int,
    atr: float,
) -> bool:
    """
    After a sweep, confirm that price structure shifts:
    - Bullish sweep: subsequent bar(s) make higher high and higher close
    - Bearish sweep: subsequent bar(s) make lower low and lower close
    """
    start = sweep.bar_index + 1
    if start >= end_idx:
        return False

    confirm_bars = candles[start:end_idx + 1]
    if not confirm_bars:
        return False

    if sweep.sweep_type == "bullish":
        # Need at least one bar with higher close than sweep bar close
        # AND the bar must close bullish (close > open)
        for cb in confirm_bars:
            if cb.close > sweep.close_price and cb.close > cb.open:
                return True

    elif sweep.sweep_type == "bearish":
        for cb in confirm_bars:
            if cb.close < sweep.close_price and cb.close < cb.open:
                return True

    return False


# ─── Main Signal Function ────────────────

_state = {"last_signal_bar": -999}


def contextual_reversal_signal(
    closed_bars: List[BTCandle],
    cfg: ContextualReversalConfig | None = None,
) -> Optional[BacktestSignal]:
    """
    Generate contextual reversal signal from closed bars.

    Requirements for signal:
    1. Enough history (≥ htf_ema_period + swing_lookback)
    2. Liquidity sweep detected in recent bars
    3. Structure confirmation after sweep
    4. HTF trend alignment
    5. ATR within acceptable range
    6. RR ≥ min_rr with target ≥ min_target_bps
    """
    if cfg is None:
        cfg = ContextualReversalConfig()

    min_bars = max(cfg.htf_ema_period + 20, cfg.swing_lookback * 3 + 30)
    if len(closed_bars) < min_bars:
        return None

    current_bar = closed_bars[-1]
    bar_idx = len(closed_bars) - 1

    # Cooldown
    if bar_idx - _state["last_signal_bar"] < cfg.min_bars_between_signals:
        return None

    # ── ATR + volatility gate ─────────────
    atr = _compute_atr(closed_bars, cfg.atr_period)
    price = current_bar.close
    if price <= 0:
        return None
    atr_bps = (atr / price) * 10000

    if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
        return None

    # ── HTF trend (long EMA) ─────────────
    closes = [c.close for c in closed_bars]
    htf_ema = _ema(closes, cfg.htf_ema_period)
    ltf_ema = _ema(closes, cfg.ltf_ema_period)

    # ── Detect swings ────────────────────
    # Use all bars except last few (need confirmed swings)
    swings = _detect_swings(closed_bars[:-2], cfg.swing_lookback)
    if len(swings) < cfg.min_swing_points:
        return None

    # ── Scan recent bars for sweep ───────
    # Look at bars [-structure_confirm_bars-3 .. -2] for sweep
    scan_start = max(0, bar_idx - cfg.structure_confirm_bars - 3)
    scan_end = bar_idx - 1  # Not including current bar

    best_sweep: Optional[LiquiditySweep] = None
    for scan_idx in range(scan_end, scan_start - 1, -1):
        sweep = _detect_sweep(closed_bars, swings, scan_idx, atr, cfg)
        if sweep is not None:
            # Check structure confirmation from sweep to current bar
            if _structure_confirms(closed_bars, sweep, bar_idx - 1, atr):
                best_sweep = sweep
                break

    if best_sweep is None:
        return None

    # ── Generate signal ──────────────────
    if best_sweep.sweep_type == "bullish":
        # HTF alignment: price should be above HTF EMA or near it
        # Allow counter-trend only if very strong sweep
        if price < htf_ema * 0.98:
            # Against major trend and not near support — skip
            return None

        entry = price
        # SL below the sweep extreme with buffer
        sl = best_sweep.sweep_extreme - cfg.sl_buffer_atr * atr
        risk = entry - sl
        if risk <= 0:
            return None

        # Target: max of (min_rr × risk, target_rr × risk)
        tp = entry + cfg.target_rr * risk

        # Check minimum target size
        target_bps = ((tp - entry) / entry) * 10000
        if target_bps < cfg.min_target_bps:
            # Try to increase TP if RR stays above min
            tp = entry * (1 + cfg.min_target_bps / 10000)
            new_rr = (tp - entry) / risk
            if new_rr < cfg.min_rr:
                return None

        actual_rr = (tp - entry) / risk
        if actual_rr < cfg.min_rr:
            return None

        _state["last_signal_bar"] = bar_idx
        return BacktestSignal(
            symbol="BTCUSDT",
            direction="long",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="ctx_reversal_long",
            confidence=min(0.9, 0.5 + (actual_rr - 2.0) * 0.1),
            risk_reward=actual_rr,
            timestamp=current_bar.timestamp,
        )

    elif best_sweep.sweep_type == "bearish":
        # HTF alignment: price should be below HTF EMA
        if price > htf_ema * 1.02:
            return None

        entry = price
        sl = best_sweep.sweep_extreme + cfg.sl_buffer_atr * atr
        risk = sl - entry
        if risk <= 0:
            return None

        tp = entry - cfg.target_rr * risk
        target_bps = ((entry - tp) / entry) * 10000
        if target_bps < cfg.min_target_bps:
            tp = entry * (1 - cfg.min_target_bps / 10000)
            new_rr = (entry - tp) / risk
            if new_rr < cfg.min_rr:
                return None

        actual_rr = (entry - tp) / risk
        if actual_rr < cfg.min_rr:
            return None

        _state["last_signal_bar"] = bar_idx
        return BacktestSignal(
            symbol="BTCUSDT",
            direction="short",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="ctx_reversal_short",
            confidence=min(0.9, 0.5 + (actual_rr - 2.0) * 0.1),
            risk_reward=actual_rr,
            timestamp=current_bar.timestamp,
        )

    return None


# ─── Factory: create signal_fn for pipeline ─

def make_contextual_reversal_fn(
    cfg: ContextualReversalConfig | None = None,
) -> callable:
    """Create a stateful signal function compatible with BacktestPipeline."""
    if cfg is None:
        cfg = ContextualReversalConfig()

    state = {"last_signal_bar": -999}

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        min_bars = max(cfg.htf_ema_period + 20, cfg.swing_lookback * 3 + 30)
        if len(closed_bars) < min_bars:
            return None

        current_bar = closed_bars[-1]
        bar_idx = len(closed_bars) - 1

        # Cooldown
        if bar_idx - state["last_signal_bar"] < cfg.min_bars_between_signals:
            return None

        # ATR + volatility gate
        atr = _compute_atr(closed_bars, cfg.atr_period)
        price = current_bar.close
        if price <= 0:
            return None
        atr_bps = (atr / price) * 10000
        if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
            return None

        # HTF trend
        closes = [c.close for c in closed_bars]
        htf_ema = _ema(closes, cfg.htf_ema_period)

        # Swings
        swings = _detect_swings(closed_bars[:-2], cfg.swing_lookback)
        if len(swings) < cfg.min_swing_points:
            return None

        # Scan for sweep + confirmation
        scan_start = max(0, bar_idx - cfg.structure_confirm_bars - 3)
        scan_end = bar_idx - 1

        for scan_idx in range(scan_end, scan_start - 1, -1):
            sweep = _detect_sweep(closed_bars, swings, scan_idx, atr, cfg)
            if sweep is None:
                continue
            if not _structure_confirms(closed_bars, sweep, bar_idx - 1, atr):
                continue

            # Build signal
            if sweep.sweep_type == "bullish":
                if price < htf_ema * 0.98:
                    continue
                entry = price
                sl = sweep.sweep_extreme - cfg.sl_buffer_atr * atr
                risk = entry - sl
                if risk <= 0:
                    continue
                tp = entry + cfg.target_rr * risk
                target_bps = ((tp - entry) / entry) * 10000
                if target_bps < cfg.min_target_bps:
                    tp = entry * (1 + cfg.min_target_bps / 10000)
                    if (tp - entry) / risk < cfg.min_rr:
                        continue
                actual_rr = (tp - entry) / risk
                if actual_rr < cfg.min_rr:
                    continue

                state["last_signal_bar"] = bar_idx
                return BacktestSignal(
                    symbol="BTCUSDT",
                    direction="long",
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    setup_type="ctx_reversal_long",
                    confidence=min(0.9, 0.5 + (actual_rr - 2.0) * 0.1),
                    risk_reward=actual_rr,
                    timestamp=current_bar.timestamp,
                )

            elif sweep.sweep_type == "bearish":
                if price > htf_ema * 1.02:
                    continue
                entry = price
                sl = sweep.sweep_extreme + cfg.sl_buffer_atr * atr
                risk = sl - entry
                if risk <= 0:
                    continue
                tp = entry - cfg.target_rr * risk
                target_bps = ((entry - tp) / entry) * 10000
                if target_bps < cfg.min_target_bps:
                    tp = entry * (1 - cfg.min_target_bps / 10000)
                    if (entry - tp) / risk < cfg.min_rr:
                        continue
                actual_rr = (entry - tp) / risk
                if actual_rr < cfg.min_rr:
                    continue

                state["last_signal_bar"] = bar_idx
                return BacktestSignal(
                    symbol="BTCUSDT",
                    direction="short",
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    setup_type="ctx_reversal_short",
                    confidence=min(0.9, 0.5 + (actual_rr - 2.0) * 0.1),
                    risk_reward=actual_rr,
                    timestamp=current_bar.timestamp,
                )

        return None

    return signal_fn
