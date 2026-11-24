"""
Alpha C — Liquidity / SMC Events
═════════════════════════════════
Trades institutional footprints: FVG fills, order block reactions,
liquidity sweeps with immediate reversal, stop hunts.

Key Design (Post-Autopsy):
  - Target: ≥ 150 bps moves from institutional footprints
  - FVG: 3-candle imbalance + return to fill = entry
  - Order Block: impulse candle + displacement + retest = entry
  - Liquidity Sweep: aggressive wick + close back inside → immediate entry
  - All require volume and structure confirmation
  - RR ≥ 2.0

Signal Logic (3 sub-strategies, first match wins):
  A. FVG Fill: detect gap, wait for fill, enter on rejection
  B. Order Block Retest: detect OB, wait for retest, confirm hold
  C. Stop Hunt: detect aggressive sweep + immediate reversal
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from smc_futures.backtest.engine import BTCandle, BacktestSignal


# ─── Configuration ────────────────────────

@dataclass
class LiquiditySMCConfig:
    """Tunable parameters for liquidity/SMC event signals."""

    # ── FVG Detection ──
    fvg_min_gap_bps: float = 5.0       # Min gap size in bps (filter noise)
    fvg_displacement_min: float = 0.6  # C1 body ratio for displacement
    fvg_max_age_bars: int = 50         # FVG expires after N bars
    fvg_fill_zone_atr: float = 0.3     # Price must enter gap zone by this ATR margin

    # ── Order Block Detection ──
    ob_impulse_body_ratio: float = 0.6   # Body/range ratio for impulse candle
    ob_volume_mult: float = 1.3          # Volume ≥ this × avg
    ob_max_age_bars: int = 40            # OB expires after N bars
    ob_displacement_bars: int = 3        # Displacement must happen within N bars
    ob_displacement_min_atr: float = 1.5 # Displacement ≥ this × ATR

    # ── Stop Hunt / Sweep ──
    hunt_wick_ratio: float = 0.6       # Wick / range ratio for stop hunt
    hunt_min_wick_atr: float = 0.5     # Wick must be ≥ this × ATR
    hunt_lookback: int = 20            # Bars to look back for levels

    # ── Shared Filters ──
    atr_period: int = 14
    min_atr_bps: float = 20.0
    max_atr_bps: float = 500.0
    trend_ema_period: int = 80         # HTF trend

    # ── Risk/Reward ──
    min_rr: float = 2.0
    sl_buffer_atr: float = 0.3
    min_target_bps: float = 150.0

    # ── Cooldown ──
    min_bars_between_signals: int = 15


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


# ─── FVG Detection ────────────────────────

@dataclass
class FVG:
    """Fair Value Gap (3-candle imbalance)."""
    formation_idx: int     # Index of middle candle (C2)
    direction: str         # "bullish" or "bearish"
    gap_high: float        # Top of gap
    gap_low: float         # Bottom of gap
    gap_size: float        # In price units
    filled: bool = False


def _detect_fvgs(candles: List[BTCandle], cfg: LiquiditySMCConfig) -> List[FVG]:
    """Detect all FVGs in the candle history."""
    fvgs = []
    n = len(candles)
    for i in range(2, n):
        c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]

        # Bullish FVG: C1 high < C3 low (gap up in middle)
        if c1.high < c3.low:
            gap_size = c3.low - c1.high
            price = c2.close
            if price > 0 and (gap_size / price) * 10000 >= cfg.fvg_min_gap_bps:
                # Check displacement: C2 should have decent body
                body = abs(c2.close - c2.open)
                rng = c2.high - c2.low
                if rng > 0 and body / rng >= cfg.fvg_displacement_min:
                    fvgs.append(FVG(
                        formation_idx=i - 1,
                        direction="bullish",
                        gap_high=c3.low,
                        gap_low=c1.high,
                        gap_size=gap_size,
                    ))

        # Bearish FVG: C1 low > C3 high (gap down in middle)
        if c1.low > c3.high:
            gap_size = c1.low - c3.high
            price = c2.close
            if price > 0 and (gap_size / price) * 10000 >= cfg.fvg_min_gap_bps:
                body = abs(c2.close - c2.open)
                rng = c2.high - c2.low
                if rng > 0 and body / rng >= cfg.fvg_displacement_min:
                    fvgs.append(FVG(
                        formation_idx=i - 1,
                        direction="bearish",
                        gap_high=c1.low,
                        gap_low=c3.high,
                        gap_size=gap_size,
                    ))

    return fvgs


def _check_fvg_signal(
    candles: List[BTCandle],
    bar_idx: int,
    atr: float,
    trend_ema: float,
    cfg: LiquiditySMCConfig,
) -> Optional[BacktestSignal]:
    """Check if current bar triggers a FVG fill signal."""
    fvgs = _detect_fvgs(candles[:bar_idx], cfg)  # Only from closed bars before current scan
    current = candles[bar_idx]
    price = current.close

    for fvg in reversed(fvgs):  # Most recent first
        age = bar_idx - fvg.formation_idx
        if age > cfg.fvg_max_age_bars:
            continue
        if fvg.filled:
            continue

        fill_margin = cfg.fvg_fill_zone_atr * atr

        if fvg.direction == "bullish":
            # Price dips into the FVG zone (coming from above)
            if current.low <= fvg.gap_high + fill_margin and current.close > fvg.gap_low:
                # Rejection: close above the gap (bullish)
                if current.close > current.open:
                    # Trend alignment
                    if price < trend_ema * 0.97:
                        continue

                    entry = price
                    sl = fvg.gap_low - cfg.sl_buffer_atr * atr
                    risk = entry - sl
                    if risk <= 0:
                        continue
                    tp = entry + max(cfg.min_rr * risk, entry * cfg.min_target_bps / 10000)
                    actual_rr = (tp - entry) / risk
                    if actual_rr < cfg.min_rr:
                        continue

                    fvg.filled = True
                    return BacktestSignal(
                        symbol="BTCUSDT",
                        direction="long",
                        entry_price=entry,
                        stop_loss=sl,
                        take_profit=tp,
                        setup_type="fvg_fill_long",
                        confidence=min(0.85, 0.5 + age / 100),
                        risk_reward=actual_rr,
                        timestamp=current.timestamp,
                    )

        elif fvg.direction == "bearish":
            if current.high >= fvg.gap_low - fill_margin and current.close < fvg.gap_high:
                if current.close < current.open:
                    if price > trend_ema * 1.03:
                        continue

                    entry = price
                    sl = fvg.gap_high + cfg.sl_buffer_atr * atr
                    risk = sl - entry
                    if risk <= 0:
                        continue
                    tp = entry - max(cfg.min_rr * risk, entry * cfg.min_target_bps / 10000)
                    actual_rr = (entry - tp) / risk
                    if actual_rr < cfg.min_rr:
                        continue

                    fvg.filled = True
                    return BacktestSignal(
                        symbol="BTCUSDT",
                        direction="short",
                        entry_price=entry,
                        stop_loss=sl,
                        take_profit=tp,
                        setup_type="fvg_fill_short",
                        confidence=min(0.85, 0.5 + age / 100),
                        risk_reward=actual_rr,
                        timestamp=current.timestamp,
                    )

    return None


# ─── Order Block Detection ────────────────

@dataclass
class OrderBlock:
    """Institutional order block."""
    formation_idx: int
    direction: str        # "bullish" or "bearish"
    zone_high: float
    zone_low: float
    volume: float
    retested: bool = False


def _detect_order_blocks(
    candles: List[BTCandle],
    cfg: LiquiditySMCConfig,
    avg_vol: float,
    atr: float,
) -> List[OrderBlock]:
    """Detect order blocks: impulse candle + displacement."""
    obs = []
    n = len(candles)
    for i in range(1, n - cfg.ob_displacement_bars):
        c = candles[i]
        body = abs(c.close - c.open)
        rng = c.high - c.low
        if rng <= 0:
            continue

        # Check impulse candle
        if body / rng < cfg.ob_impulse_body_ratio:
            continue
        if c.volume < avg_vol * cfg.ob_volume_mult:
            continue

        # Check displacement after impulse
        displacement = 0
        if c.close > c.open:  # Bullish impulse
            # Look for continued upward movement
            max_high = c.high
            for j in range(1, cfg.ob_displacement_bars + 1):
                if i + j < n:
                    max_high = max(max_high, candles[i + j].high)
            displacement = max_high - c.high
            if atr > 0 and displacement >= cfg.ob_displacement_min_atr * atr:
                obs.append(OrderBlock(
                    formation_idx=i,
                    direction="bullish",
                    zone_high=max(c.open, c.close),
                    zone_low=min(c.open, c.close),
                    volume=c.volume,
                ))
        else:  # Bearish impulse
            min_low = c.low
            for j in range(1, cfg.ob_displacement_bars + 1):
                if i + j < n:
                    min_low = min(min_low, candles[i + j].low)
            displacement = c.low - min_low
            if atr > 0 and displacement >= cfg.ob_displacement_min_atr * atr:
                obs.append(OrderBlock(
                    formation_idx=i,
                    direction="bearish",
                    zone_high=max(c.open, c.close),
                    zone_low=min(c.open, c.close),
                    volume=c.volume,
                ))

    return obs


def _check_ob_signal(
    candles: List[BTCandle],
    bar_idx: int,
    atr: float,
    trend_ema: float,
    avg_vol: float,
    cfg: LiquiditySMCConfig,
) -> Optional[BacktestSignal]:
    """Check if current bar triggers an order block retest signal."""
    obs = _detect_order_blocks(candles[:bar_idx], cfg, avg_vol, atr)
    current = candles[bar_idx]
    price = current.close

    for ob in reversed(obs):
        age = bar_idx - ob.formation_idx
        if age > cfg.ob_max_age_bars:
            continue
        if ob.retested:
            continue

        if ob.direction == "bullish":
            # Price dips into OB zone
            if current.low <= ob.zone_high and current.close >= ob.zone_low:
                if current.close > current.open:  # Rejection candle
                    if price < trend_ema * 0.97:
                        continue

                    entry = price
                    sl = ob.zone_low - cfg.sl_buffer_atr * atr
                    risk = entry - sl
                    if risk <= 0:
                        continue
                    tp = entry + max(cfg.min_rr * risk, entry * cfg.min_target_bps / 10000)
                    actual_rr = (tp - entry) / risk
                    if actual_rr < cfg.min_rr:
                        continue

                    ob.retested = True
                    return BacktestSignal(
                        symbol="BTCUSDT",
                        direction="long",
                        entry_price=entry,
                        stop_loss=sl,
                        take_profit=tp,
                        setup_type="ob_retest_long",
                        confidence=min(0.85, 0.5 + ob.volume / (avg_vol * 3)),
                        risk_reward=actual_rr,
                        timestamp=current.timestamp,
                    )

        elif ob.direction == "bearish":
            if current.high >= ob.zone_low and current.close <= ob.zone_high:
                if current.close < current.open:
                    if price > trend_ema * 1.03:
                        continue

                    entry = price
                    sl = ob.zone_high + cfg.sl_buffer_atr * atr
                    risk = sl - entry
                    if risk <= 0:
                        continue
                    tp = entry - max(cfg.min_rr * risk, entry * cfg.min_target_bps / 10000)
                    actual_rr = (entry - tp) / risk
                    if actual_rr < cfg.min_rr:
                        continue

                    ob.retested = True
                    return BacktestSignal(
                        symbol="BTCUSDT",
                        direction="short",
                        entry_price=entry,
                        stop_loss=sl,
                        take_profit=tp,
                        setup_type="ob_retest_short",
                        confidence=min(0.85, 0.5 + ob.volume / (avg_vol * 3)),
                        risk_reward=actual_rr,
                        timestamp=current.timestamp,
                    )

    return None


# ─── Stop Hunt Detection ─────────────────

def _check_stop_hunt_signal(
    candles: List[BTCandle],
    bar_idx: int,
    atr: float,
    trend_ema: float,
    cfg: LiquiditySMCConfig,
) -> Optional[BacktestSignal]:
    """
    Detect stop hunt: aggressive wick beyond prior level,
    close back inside → immediate reversal entry.
    """
    if bar_idx < cfg.hunt_lookback + 2:
        return None

    current = candles[bar_idx]
    price = current.close
    rng = current.high - current.low
    if rng <= 0:
        return None

    # Find prior swing levels
    lookback_bars = candles[bar_idx - cfg.hunt_lookback:bar_idx]
    prior_high = max(c.high for c in lookback_bars)
    prior_low = min(c.low for c in lookback_bars)

    # ── Bullish stop hunt (sweep below lows → close above) ──
    wick_below = current.open - current.low if current.close > current.open else current.close - current.low
    if wick_below < 0:
        wick_below = min(current.open, current.close) - current.low

    if (current.low < prior_low
            and wick_below >= cfg.hunt_min_wick_atr * atr
            and wick_below / rng >= cfg.hunt_wick_ratio
            and current.close > prior_low):  # Close back above
        # Trend check
        if price < trend_ema * 0.97:
            return None

        entry = price
        sl = current.low - cfg.sl_buffer_atr * atr
        risk = entry - sl
        if risk <= 0:
            return None
        tp = entry + max(cfg.min_rr * risk, entry * cfg.min_target_bps / 10000)
        actual_rr = (tp - entry) / risk
        if actual_rr < cfg.min_rr:
            return None

        return BacktestSignal(
            symbol="BTCUSDT",
            direction="long",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="stop_hunt_long",
            confidence=0.70,
            risk_reward=actual_rr,
            timestamp=current.timestamp,
        )

    # ── Bearish stop hunt (sweep above highs → close below) ──
    wick_above = current.high - max(current.open, current.close)

    if (current.high > prior_high
            and wick_above >= cfg.hunt_min_wick_atr * atr
            and wick_above / rng >= cfg.hunt_wick_ratio
            and current.close < prior_high):
        if price > trend_ema * 1.03:
            return None

        entry = price
        sl = current.high + cfg.sl_buffer_atr * atr
        risk = sl - entry
        if risk <= 0:
            return None
        tp = entry - max(cfg.min_rr * risk, entry * cfg.min_target_bps / 10000)
        actual_rr = (entry - tp) / risk
        if actual_rr < cfg.min_rr:
            return None

        return BacktestSignal(
            symbol="BTCUSDT",
            direction="short",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="stop_hunt_short",
            confidence=0.70,
            risk_reward=actual_rr,
            timestamp=current.timestamp,
        )

    return None


# ─── Factory: create signal_fn for pipeline ─

def make_liquidity_smc_fn(
    cfg: LiquiditySMCConfig | None = None,
) -> callable:
    """Create a stateful signal function compatible with BacktestPipeline."""
    if cfg is None:
        cfg = LiquiditySMCConfig()

    state = {
        "last_signal_bar": -999,
        "fvg_cache": [],
        "ob_cache": [],
    }

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        min_bars = max(cfg.fvg_max_age_bars, cfg.ob_max_age_bars, cfg.hunt_lookback) + 20
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

        # Trend
        closes = [c.close for c in closed_bars]
        trend_ema = _ema(closes, cfg.trend_ema_period)
        avg_vol = _avg_volume(closed_bars, 20)

        # ── Priority 1: Stop Hunt (most immediate) ──
        signal = _check_stop_hunt_signal(closed_bars, bar_idx, atr, trend_ema, cfg)
        if signal:
            state["last_signal_bar"] = bar_idx
            return signal

        # ── Priority 2: FVG Fill ──
        signal = _check_fvg_signal(closed_bars, bar_idx, atr, trend_ema, cfg)
        if signal:
            state["last_signal_bar"] = bar_idx
            return signal

        # ── Priority 3: Order Block Retest ──
        signal = _check_ob_signal(closed_bars, bar_idx, atr, trend_ema, avg_vol, cfg)
        if signal:
            state["last_signal_bar"] = bar_idx
            return signal

        return None

    return signal_fn
