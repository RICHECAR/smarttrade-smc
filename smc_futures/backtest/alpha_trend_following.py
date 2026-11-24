"""
Chantier 12 — Alpha Class 1: TREND FOLLOWING
=============================================

PARADIGM SHIFT: Low frequency, high conviction, structural edge.

Core thesis:
  - Crypto trends persist for days/weeks
  - Enter on pullbacks WITHIN confirmed trends
  - Hold for big moves (≥300-1000 bps)
  - Exit on structure break, not fixed TP
  - Filter ALL range/chop conditions

Key differences from old alphas:
  - NO short-term patterns (breakout/retest dead)
  - HTF trend confirmation required
  - Dynamic position management
  - Structural trailing stop
  - Minimum target = 300 bps
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Callable

from smc_futures.backtest.engine import BTCandle, BacktestSignal

# ═══════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════

@dataclass
class TrendFollowingConfig:
    # ── Trend Detection (HTF) ─────────────────
    trend_ema_fast: int = 21        # Fast EMA period
    trend_ema_slow: int = 55        # Slow EMA period
    trend_ema_filter: int = 100     # Ultra-slow filter
    min_trend_strength_bps: float = 12.0   # Min EMA separation (bps)
    trend_confirm_bars: int = 3     # Bars above/below to confirm

    # ── Chop/Range Filter ─────────────────────
    adx_period: int = 14
    adx_min_trend: float = 20.0     # ADX must be > 20 for trend
    chop_atr_ratio_max: float = 0.6 # body/range ratio for chop detection
    min_directional_bars: int = 3   # Consecutive bars in direction

    # ── Pullback Entry ────────────────────────
    pullback_min_bars: int = 2      # Min bars in pullback
    pullback_max_bars: int = 12     # Max bars before invalidation
    pullback_depth_min_atr: float = 0.15  # Min depth = 0.15 ATR
    pullback_depth_max_atr: float = 3.0   # Max depth = 3.0 ATR
    pullback_into_ema: bool = True  # Must touch fast EMA zone

    # ── Entry Confirmation ────────────────────
    rejection_candle: bool = True   # Require rejection at EMA
    min_rejection_body_ratio: float = 0.3  # Body > 30% of range
    volume_confirm: bool = True     # Volume must > avg
    volume_lookback: int = 20

    # ── Risk Management ───────────────────────
    atr_period: int = 14
    sl_atr_mult: float = 1.5       # SL = 1.5 ATR below entry
    min_target_bps: float = 150.0   # MINIMUM 150 bps target
    min_rr: float = 1.5            # Minimum 1.5:1 RR
    trailing_stop_atr: float = 2.5  # Trailing stop = 2.5 ATR

    # ── Position Filters ──────────────────────
    min_atr_bps: float = 5.0       # Min volatility (5m BTC = 7-40 bps)
    max_atr_bps: float = 1000.0    # Max volatility
    min_bars_between: int = 20     # Cooldown
    max_holding_bars: int = 288    # 24h for 5m bars

    # ── Structural Exit ───────────────────────
    structure_break_atr: float = 1.5  # Structure break = close beyond 1.5 ATR
    trend_exhaustion_bars: int = 50   # Max trend extension


# ═══════════════════════════════════════════════════
#  Indicators
# ═══════════════════════════════════════════════════

def _ema(values: List[float], period: int) -> List[float]:
    """Exponential Moving Average. Returns list same length as values, NaN-padded."""
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
    """Average True Range. Returns list same length as bars."""
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


def _adx(bars: List[BTCandle], period: int = 14) -> List[float]:
    """Average Directional Index. Returns list same length as bars."""
    n = len(bars)
    if n < period + 1:
        return [0.0] * n

    # +DM, -DM
    plus_dm = [0.0]
    minus_dm = [0.0]
    trs = [bars[0].high - bars[0].low]

    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        trs.append(tr)

    # Smoothed Wilder
    sm_tr = _wilder_smooth(trs, period)
    sm_plus = _wilder_smooth(plus_dm, period)
    sm_minus = _wilder_smooth(minus_dm, period)

    # DI+, DI-
    dx_vals = []
    for i in range(n):
        if sm_tr[i] > 0:
            di_plus = 100.0 * sm_plus[i] / sm_tr[i]
            di_minus = 100.0 * sm_minus[i] / sm_tr[i]
        else:
            di_plus = di_minus = 0.0
        di_sum = di_plus + di_minus
        dx = 100.0 * abs(di_plus - di_minus) / di_sum if di_sum > 0 else 0.0
        dx_vals.append(dx)

    # ADX = smoothed DX
    return _wilder_smooth(dx_vals, period)


def _wilder_smooth(values: List[float], period: int) -> List[float]:
    """Wilder's smoothing method."""
    n = len(values)
    if n < period:
        return [0.0] * n
    result = [0.0] * n
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def _avg_volume(bars: List[BTCandle], lookback: int) -> float:
    """Average volume over last N bars."""
    if not bars:
        return 0.0
    vols = [b.volume for b in bars[-lookback:]]
    return sum(vols) / len(vols) if vols else 0.0


# ═══════════════════════════════════════════════════
#  Signal Logic
# ═══════════════════════════════════════════════════

def _detect_trend(
    bars: List[BTCandle],
    ema_f: List[float],
    ema_s: List[float],
    ema_filter: List[float],
    adx_vals: List[float],
    cfg: TrendFollowingConfig,
) -> Optional[str]:
    """
    Detect HTF trend direction. Returns "long", "short", or None.
    Requires:
      - EMA stack alignment (fast > slow > filter for long)
      - ADX > threshold (trending, not chop)
      - Sufficient EMA separation (trend strength)
      - Confirmation bars above/below
    """
    i = len(bars) - 1
    if i < 1:
        return None

    ef = ema_f[i]
    es = ema_s[i]
    efl = ema_filter[i]
    adx_val = adx_vals[i]

    if math.isnan(ef) or math.isnan(es) or math.isnan(efl):
        return None

    # ADX filter — must be trending
    if adx_val < cfg.adx_min_trend:
        return None

    # EMA separation (trend strength)
    mid = (ef + es) / 2
    if mid == 0:
        return None
    separation_bps = abs(ef - es) / mid * 10000
    if separation_bps < cfg.min_trend_strength_bps:
        return None

    # EMA stack alignment
    if ef > es > efl:
        direction = "long"
    elif ef < es < efl:
        direction = "short"
    else:
        return None

    # Confirmation: check last N bars stayed in trend
    for j in range(max(0, i - cfg.trend_confirm_bars + 1), i + 1):
        if j >= len(ema_f) or math.isnan(ema_f[j]) or math.isnan(ema_s[j]):
            return None
        if direction == "long" and ema_f[j] <= ema_s[j]:
            return None
        if direction == "short" and ema_f[j] >= ema_s[j]:
            return None

    return direction


def _detect_pullback(
    bars: List[BTCandle],
    ema_f: List[float],
    atr_vals: List[float],
    direction: str,
    cfg: TrendFollowingConfig,
) -> Optional[int]:
    """
    Detect pullback to EMA zone.
    Returns pullback start index, or None.

    Logic: price was away from EMA (trending), then pulled back near the EMA.
    We look for a swing high/low in the lookback window, then measure
    the depth of the pullback from that swing to the current price.
    """
    i = len(bars) - 1
    atr = atr_vals[i]
    if atr <= 0 or math.isnan(atr):
        return None

    ef = ema_f[i]
    if math.isnan(ef):
        return None

    price = bars[i].close

    # Current price must be near the fast EMA (within reasonable distance)
    if cfg.pullback_into_ema:
        dist_to_ema = abs(price - ef) / atr
        if dist_to_ema > 1.5:  # Within 1.5 ATR of EMA
            return None

    # Find swing point in lookback window (excluding last pullback_min_bars)
    lb_start = max(0, i - cfg.pullback_max_bars)
    lb_end = max(lb_start, i - cfg.pullback_min_bars)

    if lb_end <= lb_start:
        return None

    lookback_bars = bars[lb_start:lb_end + 1]
    recent_bars = bars[lb_end:i + 1]

    if direction == "long":
        # Swing high before pullback, current price near EMA
        swing_high = max(b.high for b in lookback_bars)
        current_low = min(b.low for b in recent_bars)
        depth = swing_high - current_low
    else:
        # Swing low before pullback, current price near EMA from below
        swing_low = min(b.low for b in lookback_bars)
        current_high = max(b.high for b in recent_bars)
        depth = current_high - swing_low

    if depth < cfg.pullback_depth_min_atr * atr:
        return None
    if depth > cfg.pullback_depth_max_atr * atr:
        return None

    return lb_start


def _confirm_entry(
    bars: List[BTCandle],
    atr_vals: List[float],
    direction: str,
    cfg: TrendFollowingConfig,
) -> bool:
    """
    Confirm entry with rejection candle + volume.
    The LAST bar must show rejection in the trend direction.
    """
    i = len(bars) - 1
    bar = bars[i]
    body = abs(bar.close - bar.open)
    full_range = bar.high - bar.low

    if full_range <= 0:
        return False

    # Rejection candle: strong body in trend direction
    if cfg.rejection_candle:
        body_ratio = body / full_range
        if body_ratio < cfg.min_rejection_body_ratio:
            return False

        if direction == "long" and bar.close <= bar.open:
            return False  # Must be bullish candle
        if direction == "short" and bar.close >= bar.open:
            return False  # Must be bearish candle

    # Volume confirmation
    if cfg.volume_confirm and len(bars) > cfg.volume_lookback:
        avg_vol = _avg_volume(bars[:-1], cfg.volume_lookback)
        if avg_vol > 0 and bar.volume < avg_vol:
            return False

    return True


def _compute_sl_tp(
    bars: List[BTCandle],
    atr_vals: List[float],
    ema_f: List[float],
    ema_s: List[float],
    direction: str,
    cfg: TrendFollowingConfig,
) -> Optional[tuple]:
    """
    Compute structural SL and projected TP.
    SL = below swing low (long) or above swing high (short)
    TP = structural projection based on trend continuation
    Returns (sl, tp, target_bps, rr) or None.
    """
    i = len(bars) - 1
    atr = atr_vals[i]
    if atr <= 0:
        return None

    price = bars[i].close

    if direction == "long":
        # SL below recent swing low + buffer
        lookback_bars = bars[max(0, i - 20):i + 1]
        swing_low = min(b.low for b in lookback_bars)
        sl = swing_low - cfg.sl_atr_mult * atr * 0.3  # Buffer

        # TP: project trend continuation
        # Use both EMA separation and ATR-based projection
        ema_anchor = ema_s[i] if not math.isnan(ema_s[i]) else price
        trend_range = price - ema_anchor
        # Aggressive TP: max of trend projection, ATR stretch, and swing measurement
        swing_range = max(b.high for b in lookback_bars) - swing_low
        tp = price + max(trend_range * 4.0, atr * 10.0, swing_range * 1.5)

    else:
        lookback_bars = bars[max(0, i - 20):i + 1]
        swing_high = max(b.high for b in lookback_bars)
        sl = swing_high + cfg.sl_atr_mult * atr * 0.3

        ema_anchor = ema_s[i] if not math.isnan(ema_s[i]) else price
        trend_range = ema_anchor - price
        swing_range = swing_high - min(b.low for b in lookback_bars)
        tp = price - max(trend_range * 4.0, atr * 10.0, swing_range * 1.5)

    # Validate
    sl_dist = abs(price - sl)
    tp_dist = abs(tp - price)

    if sl_dist <= 0:
        return None

    target_bps = tp_dist / price * 10000
    if target_bps < cfg.min_target_bps:
        return None

    rr = tp_dist / sl_dist
    if rr < cfg.min_rr:
        return None

    return (sl, tp, target_bps, rr)


# ═══════════════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════════════

def make_trend_following_fn(
    cfg: Optional[TrendFollowingConfig] = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Factory: returns a signal function for the Trend Following alpha.

    Usage:
        signal_fn = make_trend_following_fn()
        result = pipeline.run("BTCUSDT", candles, signal_fn)
    """
    if cfg is None:
        cfg = TrendFollowingConfig()

    last_signal_bar = [-1]

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        n = len(closed_bars)
        min_bars = max(cfg.trend_ema_filter, cfg.atr_period, cfg.adx_period) + 20
        if n < min_bars:
            return None

        # Cooldown
        if last_signal_bar[0] >= 0 and (n - last_signal_bar[0]) < cfg.min_bars_between:
            return None

        # ── Compute indicators ──────────────
        closes = [b.close for b in closed_bars]
        ema_f = _ema(closes, cfg.trend_ema_fast)
        ema_s = _ema(closes, cfg.trend_ema_slow)
        ema_filter = _ema(closes, cfg.trend_ema_filter)
        atr_vals = _atr(closed_bars, cfg.atr_period)
        adx_vals = _adx(closed_bars, cfg.adx_period)

        i = n - 1
        atr = atr_vals[i]
        if math.isnan(atr) or atr <= 0:
            return None

        price = closed_bars[i].close
        if price <= 0:
            return None

        # ── Hard filters ────────────────────
        atr_bps = atr / price * 10000
        if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
            return None

        # ── Step 1: Trend detection ─────────
        direction = _detect_trend(closed_bars, ema_f, ema_s, ema_filter, adx_vals, cfg)
        if direction is None:
            return None

        # ── Step 2: Pullback detection ──────
        pb_start = _detect_pullback(closed_bars, ema_f, atr_vals, direction, cfg)
        if pb_start is None:
            return None

        # ── Step 3: Entry confirmation ──────
        if not _confirm_entry(closed_bars, atr_vals, direction, cfg):
            return None

        # ── Step 4: SL/TP computation ───────
        levels = _compute_sl_tp(closed_bars, atr_vals, ema_f, ema_s, direction, cfg)
        if levels is None:
            return None

        sl, tp, target_bps, rr = levels

        # ── HARD FILTER: minimum move ───────
        if target_bps < 100:
            return None

        last_signal_bar[0] = n

        return BacktestSignal(
            symbol="BTCUSDT",
            direction=direction,
            entry_price=price,
            stop_loss=sl,
            take_profit=tp,
            setup_type=f"trend_follow_{direction}",
            confidence=min(0.9, 0.5 + (atr_bps / 1000) + (rr / 10)),
            risk_reward=rr,
            timestamp=closed_bars[i].timestamp,
        )

    return signal_fn
