"""
Chantier 12 — Alpha Class 2: VOLATILITY EXPANSION
===================================================

Core thesis:
  - Markets alternate between compression and expansion
  - Compression = energy storage → expansion = energy release
  - The expansion move is large (≥300 bps) and directional
  - Enter ONLY when compression is confirmed AND expansion triggers
  - Filter false breakouts with volume + momentum confirmation

Key metrics:
  - Bollinger Band width (squeeze detection)
  - ATR compression ratio
  - Keltner Channel inside Bollinger = squeeze
  - Expansion = close outside Keltner + volume surge
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
class VolatilityExpansionConfig:
    # ── Squeeze Detection ─────────────────────
    bb_period: int = 20             # Bollinger Band period
    bb_std: float = 2.0             # BB standard deviations
    kc_period: int = 20             # Keltner Channel period
    kc_atr_mult: float = 1.5       # KC ATR multiplier
    squeeze_min_bars: int = 4       # Min bars in squeeze
    squeeze_max_bars: int = 50      # Max bars before stale

    # ── ATR Compression ───────────────────────
    atr_period: int = 14
    atr_compression_lookback: int = 50  # Compare ATR to N-bar avg
    atr_compression_ratio: float = 0.75  # Current ATR / avg ATR < 0.75 = compressed

    # ── Expansion Trigger ─────────────────────
    expansion_close_outside_kc: bool = True  # Close must be outside KC
    expansion_volume_surge: float = 1.2      # Volume > 1.2x average
    expansion_body_ratio: float = 0.4        # Body > 40% of range
    expansion_momentum_bars: int = 2         # Momentum confirmation bars

    # ── False Breakout Filter ─────────────────
    confirm_bars: int = 2           # Bars to confirm direction
    reentry_lookback: int = 5       # Check for whipsaw
    max_wick_ratio: float = 0.4     # Wick < 40% of range (clean break)

    # ── Direction Bias ────────────────────────
    trend_ema: int = 50             # EMA for direction bias
    use_trend_filter: bool = True   # Only trade with trend

    # ── Risk Management ───────────────────────
    sl_atr_mult: float = 1.5       # SL = 1.5 ATR from entry
    min_target_bps: float = 150.0   # Min 150 bps target
    min_rr: float = 1.5            # RR for expansion trades
    target_expansion_mult: float = 4.0  # TP = squeeze width x 4

    # ── Hard Filters ──────────────────────────
    min_atr_bps: float = 5.0
    max_atr_bps: float = 1000.0
    min_bars_between: int = 25     # Cooldown
    volume_lookback: int = 20


# ═══════════════════════════════════════════════════
#  Indicators
# ═══════════════════════════════════════════════════

def _sma(values: List[float], period: int) -> List[float]:
    """Simple Moving Average."""
    result = [float("nan")] * len(values)
    if len(values) < period:
        return result
    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period
    return result


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
    """Average True Range via EMA."""
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


def _std_dev(values: List[float], period: int) -> List[float]:
    """Rolling standard deviation."""
    result = [float("nan")] * len(values)
    if len(values) < period:
        return result
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        result[i] = math.sqrt(var)
    return result


def _bollinger_bands(
    closes: List[float], period: int, std_mult: float
) -> tuple:
    """Returns (upper, middle, lower, width) lists."""
    mid = _sma(closes, period)
    sd = _std_dev(closes, period)
    n = len(closes)
    upper = [float("nan")] * n
    lower = [float("nan")] * n
    width = [float("nan")] * n
    for i in range(n):
        if not math.isnan(mid[i]) and not math.isnan(sd[i]):
            upper[i] = mid[i] + std_mult * sd[i]
            lower[i] = mid[i] - std_mult * sd[i]
            width[i] = (upper[i] - lower[i]) / mid[i] * 10000 if mid[i] > 0 else 0.0
    return upper, mid, lower, width


def _keltner_channels(
    closes: List[float], atr_vals: List[float], period: int, atr_mult: float
) -> tuple:
    """Returns (upper, middle, lower) lists."""
    mid = _ema(closes, period)
    n = len(closes)
    upper = [float("nan")] * n
    lower = [float("nan")] * n
    for i in range(n):
        if not math.isnan(mid[i]) and not math.isnan(atr_vals[i]):
            upper[i] = mid[i] + atr_mult * atr_vals[i]
            lower[i] = mid[i] - atr_mult * atr_vals[i]
    return upper, mid, lower


# ═══════════════════════════════════════════════════
#  Squeeze Detection
# ═══════════════════════════════════════════════════

def _detect_squeeze(
    bb_upper: List[float],
    bb_lower: List[float],
    kc_upper: List[float],
    kc_lower: List[float],
    atr_vals: List[float],
    cfg: VolatilityExpansionConfig,
    idx: int,
) -> Optional[int]:
    """
    Detect volatility squeeze (Bollinger inside Keltner).
    Returns number of bars in squeeze, or None if not squeezed.
    """
    squeeze_count = 0
    for j in range(idx, max(idx - cfg.squeeze_max_bars, -1), -1):
        if (
            not math.isnan(bb_upper[j]) and not math.isnan(kc_upper[j])
            and bb_upper[j] < kc_upper[j] and bb_lower[j] > kc_lower[j]
        ):
            squeeze_count += 1
        else:
            break

    if squeeze_count < cfg.squeeze_min_bars:
        return None
    return squeeze_count


def _detect_atr_compression(
    atr_vals: List[float],
    cfg: VolatilityExpansionConfig,
    idx: int,
) -> bool:
    """Check if current ATR is compressed relative to historical."""
    if idx < cfg.atr_compression_lookback:
        return False
    current_atr = atr_vals[idx]
    if math.isnan(current_atr) or current_atr <= 0:
        return False

    historical = [
        a for a in atr_vals[idx - cfg.atr_compression_lookback: idx]
        if not math.isnan(a) and a > 0
    ]
    if not historical:
        return False

    avg_atr = sum(historical) / len(historical)
    return (current_atr / avg_atr) < cfg.atr_compression_ratio


# ═══════════════════════════════════════════════════
#  Expansion Trigger
# ═══════════════════════════════════════════════════

def _detect_expansion(
    bars: List[BTCandle],
    kc_upper: List[float],
    kc_lower: List[float],
    atr_vals: List[float],
    cfg: VolatilityExpansionConfig,
    idx: int,
) -> Optional[str]:
    """
    Detect expansion (close outside Keltner with conviction).
    Returns "long" or "short" or None.
    """
    bar = bars[idx]
    body = abs(bar.close - bar.open)
    full_range = bar.high - bar.low

    if full_range <= 0:
        return None

    # Body ratio check
    if body / full_range < cfg.expansion_body_ratio:
        return None

    # Direction: close outside KC
    kc_u = kc_upper[idx]
    kc_l = kc_lower[idx]
    if math.isnan(kc_u) or math.isnan(kc_l):
        return None

    if bar.close > kc_u and bar.close > bar.open:
        direction = "long"
    elif bar.close < kc_l and bar.close < bar.open:
        direction = "short"
    else:
        return None

    # Wick filter: clean break, not heavy rejection
    if direction == "long":
        upper_wick = bar.high - bar.close
        if full_range > 0 and upper_wick / full_range > cfg.max_wick_ratio:
            return None
    else:
        lower_wick = bar.close - bar.low
        if full_range > 0 and lower_wick / full_range > cfg.max_wick_ratio:
            return None

    # Volume surge
    if idx > cfg.volume_lookback:
        avg_vol = sum(b.volume for b in bars[idx - cfg.volume_lookback:idx]) / cfg.volume_lookback
        if avg_vol > 0 and bar.volume < cfg.expansion_volume_surge * avg_vol:
            return None

    # Momentum confirmation: check previous bars building momentum
    momentum_count = 0
    for j in range(max(0, idx - cfg.expansion_momentum_bars), idx):
        if direction == "long" and bars[j].close > bars[j].open:
            momentum_count += 1
        elif direction == "short" and bars[j].close < bars[j].open:
            momentum_count += 1

    if momentum_count < max(1, cfg.expansion_momentum_bars // 2):
        return None

    return direction


# ═══════════════════════════════════════════════════
#  Signal Logic
# ═══════════════════════════════════════════════════

def make_volatility_expansion_fn(
    cfg: Optional[VolatilityExpansionConfig] = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Factory: returns a signal function for the Volatility Expansion alpha.

    Logic:
      1. Detect volatility squeeze (BB inside KC + ATR compression)
      2. Wait for expansion trigger (close outside KC + volume)
      3. Filter false breakouts (wick ratio, momentum)
      4. Enter with structural SL/TP
    """
    if cfg is None:
        cfg = VolatilityExpansionConfig()

    last_signal_bar = [-1]

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        n = len(closed_bars)
        min_bars = max(cfg.bb_period, cfg.kc_period, cfg.atr_period, cfg.trend_ema) + cfg.squeeze_max_bars + 10
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
        atr = atr_vals[i]
        if math.isnan(atr) or atr <= 0:
            return None

        # Hard filter: volatility
        atr_bps = atr / price * 10000
        if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
            return None

        # Bollinger Bands
        bb_upper, bb_mid, bb_lower, bb_width = _bollinger_bands(
            closes, cfg.bb_period, cfg.bb_std
        )

        # Keltner Channels
        kc_upper, kc_mid, kc_lower = _keltner_channels(
            closes, atr_vals, cfg.kc_period, cfg.kc_atr_mult
        )

        # ── Step 1: Was there a squeeze RECENTLY? ──
        # Check if previous bars were in squeeze (not current — current is expansion)
        squeeze_bars = _detect_squeeze(
            bb_upper, bb_lower, kc_upper, kc_lower, atr_vals, cfg, i - 1
        )
        if squeeze_bars is None:
            return None

        # Additionally check ATR compression was present
        atr_compressed = _detect_atr_compression(atr_vals, cfg, i - 1)
        # Not mandatory but boosts confidence
        combined_squeeze = squeeze_bars >= cfg.squeeze_min_bars

        if not combined_squeeze:
            return None

        # ── Step 2: Expansion trigger on CURRENT bar ──
        direction = _detect_expansion(
            closed_bars, kc_upper, kc_lower, atr_vals, cfg, i
        )
        if direction is None:
            return None

        # ── Step 3: Trend filter ────────────
        if cfg.use_trend_filter:
            ema_trend = _ema(closes, cfg.trend_ema)
            if not math.isnan(ema_trend[i]):
                if direction == "long" and price < ema_trend[i]:
                    return None
                if direction == "short" and price > ema_trend[i]:
                    return None

        # ── Step 4: Compute SL/TP ──────────
        if direction == "long":
            # SL below squeeze low
            squeeze_start = max(0, i - squeeze_bars - 1)
            squeeze_low = min(b.low for b in closed_bars[squeeze_start:i])
            sl = squeeze_low - cfg.sl_atr_mult * atr * 0.3

            # TP: squeeze width x expansion multiplier OR ATR-based minimum
            squeeze_range = max(b.high for b in closed_bars[squeeze_start:i]) - squeeze_low
            tp = price + max(squeeze_range * cfg.target_expansion_mult, atr * 10.0)
        else:
            squeeze_start = max(0, i - squeeze_bars - 1)
            squeeze_high = max(b.high for b in closed_bars[squeeze_start:i])
            sl = squeeze_high + cfg.sl_atr_mult * atr * 0.3

            squeeze_range = squeeze_high - min(b.low for b in closed_bars[squeeze_start:i])
            tp = price - max(squeeze_range * cfg.target_expansion_mult, atr * 10.0)

        # ── Validate levels ─────────────────
        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        if sl_dist <= 0:
            return None

        target_bps = tp_dist / price * 10000
        rr = tp_dist / sl_dist

        if target_bps < cfg.min_target_bps:
            return None
        if rr < cfg.min_rr:
            return None

        # HARD FILTER: minimum move
        if target_bps < 100:
            return None

        last_signal_bar[0] = n

        confidence = min(0.95, 0.5 + (squeeze_bars / 30.0) + (0.1 if atr_compressed else 0.0))

        return BacktestSignal(
            symbol="BTCUSDT",
            direction=direction,
            entry_price=price,
            stop_loss=sl,
            take_profit=tp,
            setup_type=f"vol_expansion_{direction}",
            confidence=confidence,
            risk_reward=rr,
            timestamp=closed_bars[i].timestamp,
        )

    return signal_fn
