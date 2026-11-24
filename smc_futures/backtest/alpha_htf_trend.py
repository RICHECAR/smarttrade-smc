"""
Chantier 13 — AXE 1: HIGH TIMEFRAME TREND FOLLOWING
=====================================================

Core thesis:
  - 4H/1D timeframes → much less noise, bigger moves
  - Trend following with multi-EMA alignment
  - Targets: 500–2000 bps (structural moves)
  - Hold time: days to weeks
  - Very low frequency (1-2 trades/week max)
  - Costs become negligible vs target size

Key advantage over 5m:
  - ~120 bps costs are 5-10% of target vs 100%+ on 5m
  - Trends are stronger and more persistent on HTF
  - Less noise → better signal quality
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Callable

from smc_futures.backtest.engine import BTCandle, BacktestSignal


@dataclass
class HTFTrendConfig:
    # ── Trend Detection (multi-EMA) ──────────
    ema_fast: int = 8           # ~1.3 days on 4H
    ema_mid: int = 21           # ~3.5 days on 4H
    ema_slow: int = 55          # ~9 days on 4H
    ema_filter: int = 100       # ~17 days on 4H (regime)

    # ── ADX Trend Strength ───────────────────
    adx_period: int = 14
    adx_min: float = 15.0       # Minimum trend strength (relaxed from 20)

    # ── ATR & Volatility ─────────────────────
    atr_period: int = 14
    min_atr_bps: float = 20.0   # HTF has higher ATR in bps
    max_atr_bps: float = 2000.0

    # ── Entry: Pullback to EMA ───────────────
    pullback_ema: str = "mid"   # Price must touch mid EMA
    pullback_depth_min_atr: float = 0.3
    pullback_depth_max_atr: float = 4.0
    pullback_lookback: int = 8  # Bars to look back for swing

    # ── Entry Confirmation ───────────────────
    confirm_close_above_fast: bool = True
    confirm_bullish_bar: bool = True

    # ── Risk Management ──────────────────────
    sl_atr_mult: float = 2.0       # SL = 2 ATR
    tp_atr_mult: float = 8.0       # TP = 8 ATR (targeting 500+ bps)
    tp_swing_mult: float = 2.0     # TP also uses swing range
    min_target_bps: float = 300.0   # Minimum 300 bps target
    min_rr: float = 2.0            # Risk:reward >= 2.0

    # ── Cooldown ─────────────────────────────
    min_bars_between: int = 3   # ~12h on 4H (relaxed from 6)
    max_holding_bars: int = 60  # ~10 days on 4H


# ═══════════════════════════════════════════
#  Indicators
# ═══════════════════════════════════════════

def _ema(values: List[float], period: int) -> List[float]:
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
    n = len(bars)
    if n < period + 1:
        return [float("nan")] * n

    plus_dm = []
    minus_dm = []
    trs = []

    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(max(up, 0) if up > down else 0)
        minus_dm.append(max(down, 0) if down > up else 0)
        trs.append(max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        ))

    atr_s = _ema(trs, period)
    plus_di_raw = _ema(plus_dm, period)
    minus_di_raw = _ema(minus_dm, period)

    dx_vals = []
    for j in range(len(atr_s)):
        a = atr_s[j]
        p = plus_di_raw[j] if j < len(plus_di_raw) else float("nan")
        m = minus_di_raw[j] if j < len(minus_di_raw) else float("nan")
        if math.isnan(a) or math.isnan(p) or math.isnan(m) or a <= 0:
            dx_vals.append(float("nan"))
            continue
        pdi = (p / a) * 100
        mdi = (m / a) * 100
        s = pdi + mdi
        dx_vals.append(abs(pdi - mdi) / s * 100 if s > 0 else 0)

    adx_smoothed = _ema(dx_vals, period)

    # If triple-EMA warmup poisoned all values, fall back to direct smoothing
    # over valid (non-NaN) dx entries only.
    if all(math.isnan(v) for v in adx_smoothed):
        valid_dx = [(j, v) for j, v in enumerate(dx_vals) if not math.isnan(v)]
        if len(valid_dx) >= period:
            seed = sum(v for _, v in valid_dx[:period]) / period
            adx_smoothed = [float("nan")] * len(dx_vals)
            adx_smoothed[valid_dx[period - 1][0]] = seed
            k_f = 2 / (period + 1)
            for vi in range(period, len(valid_dx)):
                idx = valid_dx[vi][0]
                prev_idx = valid_dx[vi - 1][0]
                prev = adx_smoothed[prev_idx]
                adx_smoothed[idx] = valid_dx[vi][1] * k_f + prev * (1 - k_f)

    result = [float("nan")] * n
    offset = 1
    for j, v in enumerate(adx_smoothed):
        idx = j + offset
        if idx < n:
            result[idx] = v
    return result


# ═══════════════════════════════════════════
#  Trend Detection
# ═══════════════════════════════════════════

def _detect_htf_trend(
    ema_f: List[float], ema_m: List[float],
    ema_s: List[float], ema_fil: List[float],
    adx_vals: List[float], i: int, cfg: HTFTrendConfig,
) -> Optional[str]:
    """
    Detect strong trend using multi-EMA alignment + ADX.
    Returns 'long', 'short', or None.
    """
    if any(math.isnan(v[i]) for v in [ema_f, ema_m, ema_s, ema_fil]):
        return None

    adx = adx_vals[i]
    if math.isnan(adx) or adx < cfg.adx_min:
        return None

    # Allow 3/4 EMA alignment (relaxed from strict 4/4)
    long_checks = [
        ema_f[i] > ema_m[i],
        ema_m[i] > ema_s[i],
        ema_s[i] > ema_fil[i],
        ema_f[i] > ema_s[i],  # fast above slow (redundant if first 2 true)
    ]
    short_checks = [
        ema_f[i] < ema_m[i],
        ema_m[i] < ema_s[i],
        ema_s[i] < ema_fil[i],
        ema_f[i] < ema_s[i],
    ]
    if sum(long_checks) >= 3:
        return "long"
    if sum(short_checks) >= 3:
        return "short"

    return None


# ═══════════════════════════════════════════
#  Pullback Detection
# ═══════════════════════════════════════════

def _detect_pullback(
    bars: List[BTCandle], atr_vals: List[float],
    ema_m: List[float], i: int, direction: str,
    cfg: HTFTrendConfig,
) -> bool:
    """
    Check if price has pulled back to mid-EMA zone.
    """
    lb = max(0, i - cfg.pullback_lookback)
    atr = atr_vals[i]
    if math.isnan(atr) or atr <= 0:
        return False

    ema_val = ema_m[i]
    if math.isnan(ema_val):
        return False

    price = bars[i].close

    # Price must be near mid-EMA (within pullback depth range)
    if direction == "long":
        # Look for swing high in lookback, then pullback toward EMA
        swing_high = max(b.high for b in bars[lb:i + 1])
        depth = swing_high - price
        depth_atr = depth / atr if atr > 0 else 0

        # Price near or below mid EMA
        near_ema = price <= ema_val + 0.5 * atr
        depth_ok = cfg.pullback_depth_min_atr <= depth_atr <= cfg.pullback_depth_max_atr

        return near_ema and depth_ok
    else:
        swing_low = min(b.low for b in bars[lb:i + 1])
        depth = price - swing_low
        depth_atr = depth / atr if atr > 0 else 0

        near_ema = price >= ema_val - 0.5 * atr
        depth_ok = cfg.pullback_depth_min_atr <= depth_atr <= cfg.pullback_depth_max_atr

        return near_ema and depth_ok


# ═══════════════════════════════════════════
#  Entry Confirmation
# ═══════════════════════════════════════════

def _confirm_entry(
    bars: List[BTCandle], ema_f: List[float],
    i: int, direction: str, cfg: HTFTrendConfig,
) -> bool:
    """Confirm entry with bar characteristics."""
    bar = bars[i]

    if cfg.confirm_close_above_fast:
        if not math.isnan(ema_f[i]):
            if direction == "long" and bar.close < ema_f[i]:
                return False
            if direction == "short" and bar.close > ema_f[i]:
                return False

    if cfg.confirm_bullish_bar:
        if direction == "long" and bar.close <= bar.open:
            return False
        if direction == "short" and bar.close >= bar.open:
            return False

    return True


# ═══════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════

def make_htf_trend_fn(
    cfg: Optional[HTFTrendConfig] = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Factory: HTF Trend Following alpha.

    Entry: EMA stack aligned + ADX strong + pullback to mid-EMA + bounce confirm
    SL: Below swing + ATR buffer
    TP: ATR-based (8x ATR) or swing projection
    """
    if cfg is None:
        cfg = HTFTrendConfig()

    last_signal_bar = [-1]

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        n = len(closed_bars)
        min_bars_needed = cfg.ema_filter + 20
        if n < min_bars_needed:
            return None

        if last_signal_bar[0] >= 0 and (n - last_signal_bar[0]) < cfg.min_bars_between:
            return None

        i = n - 1
        price = closed_bars[i].close
        if price <= 0:
            return None

        # ── Indicators ──
        closes = [b.close for b in closed_bars]
        atr_vals = _atr(closed_bars, cfg.atr_period)
        ema_f = _ema(closes, cfg.ema_fast)
        ema_m = _ema(closes, cfg.ema_mid)
        ema_s = _ema(closes, cfg.ema_slow)
        ema_fil = _ema(closes, cfg.ema_filter)
        adx_vals = _adx(closed_bars, cfg.adx_period)

        atr = atr_vals[i]
        if math.isnan(atr) or atr <= 0:
            return None

        atr_bps = atr / price * 10000
        if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
            return None

        # ── Trend ──
        direction = _detect_htf_trend(ema_f, ema_m, ema_s, ema_fil, adx_vals, i, cfg)
        if direction is None:
            return None

        # ── Pullback ──
        if not _detect_pullback(closed_bars, atr_vals, ema_m, i, direction, cfg):
            return None

        # ── Confirm ──
        if not _confirm_entry(closed_bars, ema_f, i, direction, cfg):
            return None

        # ── SL/TP ──
        lb = max(0, i - cfg.pullback_lookback)

        if direction == "long":
            swing_low = min(b.low for b in closed_bars[lb:i + 1])
            sl = min(swing_low, price - cfg.sl_atr_mult * atr)
            # TP: max of ATR-based, swing projection
            swing_range = max(b.high for b in closed_bars[lb:i + 1]) - swing_low
            tp = price + max(
                cfg.tp_atr_mult * atr,
                swing_range * cfg.tp_swing_mult,
            )
        else:
            swing_high = max(b.high for b in closed_bars[lb:i + 1])
            sl = max(swing_high, price + cfg.sl_atr_mult * atr)
            swing_range = swing_high - min(b.low for b in closed_bars[lb:i + 1])
            tp = price - max(
                cfg.tp_atr_mult * atr,
                swing_range * cfg.tp_swing_mult,
            )

        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        if sl_dist <= 0:
            return None

        target_bps = tp_dist / price * 10000
        rr = tp_dist / sl_dist

        if target_bps < cfg.min_target_bps or rr < cfg.min_rr:
            return None

        last_signal_bar[0] = n

        return BacktestSignal(
            symbol="BTCUSDT",
            direction=direction,
            entry_price=price,
            stop_loss=sl,
            take_profit=tp,
            setup_type=f"htf_trend_{direction}",
            confidence=min(0.9, 0.5 + (adx_vals[i] / 100.0)),
            risk_reward=rr,
            timestamp=closed_bars[i].timestamp,
        )

    return signal_fn
