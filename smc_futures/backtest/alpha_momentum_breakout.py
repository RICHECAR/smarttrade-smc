"""
Alpha B — Momentum Breakout
═══════════════════════════
Trades validated breakouts with volume confirmation, structure break,
and false-breakout filter. Continuation, not anticipation.

Key Design (Post-Autopsy):
  - Target: ≥ 200 bps continuation moves
  - Entry on RETEST of breakout level (not the initial break)
  - Volume must be 1.5x+ average at breakout
  - False breakout filter: N bars above/below level
  - RR ≥ 2.0

Signal Logic:
  1. Detect consolidation range (compression)
  2. Detect breakout (price closes outside range with volume surge)
  3. Wait for retest (price pulls back near breakout level)
  4. Confirm retest holds (rejection candle / structure support)
  5. Enter with SL below retest low, TP at range projection
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from smc_futures.backtest.engine import BTCandle, BacktestSignal


# ─── Configuration ────────────────────────

@dataclass
class MomentumBreakoutConfig:
    """Tunable parameters for momentum breakout signals."""
    # Range detection
    range_lookback: int = 30         # Bars to scan for consolidation
    range_max_atr_mult: float = 3.0  # Range width ≤ this × ATR = consolidation
    range_min_bars: int = 10         # Minimum bars in range

    # Breakout validation
    volume_surge_mult: float = 1.5   # Volume must be ≥ this × avg volume
    breakout_close_outside: bool = True  # Must close outside range
    breakout_margin_atr: float = 0.1 # Must break by at least this × ATR

    # Retest
    retest_max_bars: int = 8         # Max bars to wait for retest
    retest_zone_atr: float = 0.5     # Retest within this × ATR of level
    retest_rejection: bool = True    # Need rejection candle at retest

    # False breakout filter
    min_bars_above_level: int = 2    # At least N bars closing above/below level

    # Volatility gate
    atr_period: int = 14
    min_atr_bps: float = 20.0       # Minimum volatility
    max_atr_bps: float = 500.0      # Maximum volatility

    # Risk/Reward
    min_rr: float = 2.0
    target_mult: float = 1.5        # TP = range_height × this from breakout level
    sl_buffer_atr: float = 0.3      # SL buffer below retest low
    min_target_bps: float = 150.0   # Minimum target in bps

    # Cooldown
    min_bars_between_signals: int = 25

    # Trend filter (EMA)
    trend_ema_period: int = 50


# ─── Helpers ──────────────────────────────

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


def _avg_volume(candles: List[BTCandle], period: int = 20) -> float:
    vols = [c.volume for c in candles[-period:]]
    return sum(vols) / len(vols) if vols else 0.0


# ─── Range Detection ─────────────────────

@dataclass
class ConsolidationRange:
    start_idx: int
    end_idx: int
    high: float
    low: float
    width: float       # In price units
    bar_count: int


def _detect_consolidation(
    candles: List[BTCandle],
    end_idx: int,
    cfg: MomentumBreakoutConfig,
    atr: float,
) -> Optional[ConsolidationRange]:
    """
    Detect a consolidation range ending near end_idx.
    Range = series of bars where the total range ≤ max_atr_mult × ATR.
    """
    if end_idx < cfg.range_min_bars:
        return None

    start = max(0, end_idx - cfg.range_lookback)
    best_range: Optional[ConsolidationRange] = None
    best_bars = 0

    # Sliding window: find longest consolidation ending at end_idx
    for s in range(end_idx - cfg.range_min_bars, start - 1, -1):
        segment = candles[s:end_idx + 1]
        hi = max(c.high for c in segment)
        lo = min(c.low for c in segment)
        width = hi - lo

        if atr > 0 and width <= cfg.range_max_atr_mult * atr:
            bars = len(segment)
            if bars > best_bars:
                best_bars = bars
                best_range = ConsolidationRange(
                    start_idx=s, end_idx=end_idx,
                    high=hi, low=lo,
                    width=width, bar_count=bars,
                )
        else:
            # Range too wide — stop expanding backward
            break

    if best_range and best_range.bar_count >= cfg.range_min_bars:
        return best_range
    return None


# ─── Breakout Detection ──────────────────

@dataclass
class Breakout:
    bar_index: int
    direction: str       # "bullish" or "bearish"
    breakout_level: float
    close_price: float
    volume: float
    consolidation: ConsolidationRange


def _detect_breakout(
    candles: List[BTCandle],
    bar_idx: int,
    consolidation: ConsolidationRange,
    atr: float,
    avg_vol: float,
    cfg: MomentumBreakoutConfig,
) -> Optional[Breakout]:
    """Check if bar at bar_idx breaks out of the consolidation range."""
    if bar_idx >= len(candles):
        return None
    bar = candles[bar_idx]

    margin = cfg.breakout_margin_atr * atr

    # Bullish breakout
    if bar.close > consolidation.high + margin:
        if bar.volume >= avg_vol * cfg.volume_surge_mult:
            return Breakout(
                bar_index=bar_idx,
                direction="bullish",
                breakout_level=consolidation.high,
                close_price=bar.close,
                volume=bar.volume,
                consolidation=consolidation,
            )

    # Bearish breakout
    if bar.close < consolidation.low - margin:
        if bar.volume >= avg_vol * cfg.volume_surge_mult:
            return Breakout(
                bar_index=bar_idx,
                direction="bearish",
                breakout_level=consolidation.low,
                close_price=bar.close,
                volume=bar.volume,
                consolidation=consolidation,
            )

    return None


# ─── Retest Detection ────────────────────

def _detect_retest(
    candles: List[BTCandle],
    breakout: Breakout,
    current_idx: int,
    atr: float,
    cfg: MomentumBreakoutConfig,
) -> bool:
    """
    Check if price has retested the breakout level with rejection.
    Retest = price comes back near the level then bounces away.
    """
    level = breakout.breakout_level
    zone = cfg.retest_zone_atr * atr
    bars_since = current_idx - breakout.bar_index

    if bars_since < 1 or bars_since > cfg.retest_max_bars:
        return False

    # Check that at least min_bars_above_level bars closed outside
    bars_outside = 0
    retest_happened = False

    for i in range(breakout.bar_index + 1, current_idx + 1):
        if i >= len(candles):
            break
        bar = candles[i]

        if breakout.direction == "bullish":
            if bar.close > level:
                bars_outside += 1
            # Retest: low comes near the level
            if abs(bar.low - level) <= zone:
                retest_happened = True
        else:
            if bar.close < level:
                bars_outside += 1
            if abs(bar.high - level) <= zone:
                retest_happened = True

    if bars_outside < cfg.min_bars_above_level:
        return False

    if not retest_happened:
        return False

    # Check rejection on current bar
    if cfg.retest_rejection:
        current = candles[current_idx]
        if breakout.direction == "bullish":
            # Current bar should close bullish and above level
            if current.close <= current.open or current.close <= level:
                return False
        else:
            if current.close >= current.open or current.close >= level:
                return False

    return True


# ─── Factory: create signal_fn for pipeline ─

def make_momentum_breakout_fn(
    cfg: MomentumBreakoutConfig | None = None,
) -> callable:
    """Create a stateful signal function compatible with BacktestPipeline."""
    if cfg is None:
        cfg = MomentumBreakoutConfig()

    state = {"last_signal_bar": -999}

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        min_bars = cfg.range_lookback + cfg.retest_max_bars + 20
        if len(closed_bars) < min_bars:
            return None

        bar_idx = len(closed_bars) - 1
        current_bar = closed_bars[-1]

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

        # Trend filter
        closes = [c.close for c in closed_bars]
        trend_ema = _ema(closes, cfg.trend_ema_period)
        avg_vol = _avg_volume(closed_bars, 20)

        # Scan backward for recent breakout candidate
        # The breakout might have happened a few bars ago
        for bo_offset in range(cfg.retest_max_bars + 3, 1, -1):
            bo_candidate_idx = bar_idx - bo_offset
            if bo_candidate_idx < cfg.range_min_bars + 5:
                continue

            # Look for consolidation ending just before breakout
            range_end = bo_candidate_idx - 1
            consolidation = _detect_consolidation(
                closed_bars, range_end, cfg, atr
            )
            if consolidation is None:
                continue

            # Check if breakout happened at bo_candidate_idx
            breakout = _detect_breakout(
                closed_bars, bo_candidate_idx, consolidation,
                atr, avg_vol, cfg,
            )
            if breakout is None:
                continue

            # Trend alignment
            if breakout.direction == "bullish" and price < trend_ema * 0.99:
                continue
            if breakout.direction == "bearish" and price > trend_ema * 1.01:
                continue

            # Check retest at current bar
            if not _detect_retest(closed_bars, breakout, bar_idx, atr, cfg):
                continue

            # ── Build signal ──
            range_height = consolidation.width

            if breakout.direction == "bullish":
                entry = price
                # SL below retest low
                recent_low = min(c.low for c in closed_bars[breakout.bar_index:bar_idx + 1])
                sl = recent_low - cfg.sl_buffer_atr * atr
                risk = entry - sl
                if risk <= 0:
                    continue

                # TP: range projection from breakout level
                tp = breakout.breakout_level + range_height * cfg.target_mult
                if tp <= entry:
                    tp = entry + cfg.min_rr * risk

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
                    setup_type="momentum_breakout_long",
                    confidence=min(0.9, 0.5 + (actual_rr - 2.0) * 0.1),
                    risk_reward=actual_rr,
                    timestamp=current_bar.timestamp,
                )

            elif breakout.direction == "bearish":
                entry = price
                recent_high = max(c.high for c in closed_bars[breakout.bar_index:bar_idx + 1])
                sl = recent_high + cfg.sl_buffer_atr * atr
                risk = sl - entry
                if risk <= 0:
                    continue

                tp = breakout.breakout_level - range_height * cfg.target_mult
                if tp >= entry:
                    tp = entry - cfg.min_rr * risk

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
                    setup_type="momentum_breakout_short",
                    confidence=min(0.9, 0.5 + (actual_rr - 2.0) * 0.1),
                    risk_reward=actual_rr,
                    timestamp=current_bar.timestamp,
                )

        return None

    return signal_fn
