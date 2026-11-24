"""
Chantier 13 — AXE 2: LONG-ONLY BIAS (Bull Regime Filter)
==========================================================

Core thesis:
  - Crypto has structural long bias (BTC long-term trend up)
  - Shorts bleed during bull regimes (proven in Chantier 12)
  - Filter: ONLY trade long, ONLY in confirmed bull regime
  - Use regime detection (price > 200-EMA + positive momentum)
  - Removes ~50% of losing trades (all shorts that got destroyed)

Works on any timeframe but optimized for 4H:
  - Bull regime: EMA200 rising + price above
  - Entry: any dip in bull regime → long
  - No shorts ever → eliminates the losing side

Key finding from Chantier 12:
  - Long WR 40-50% vs Short WR 6-16%
  - Removing shorts = instant improvement
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Callable

from smc_futures.backtest.engine import BTCandle, BacktestSignal


@dataclass
class LongOnlyConfig:
    # ── Regime Detection ─────────────────────
    regime_ema: int = 50        # Trend regime (~8 days on 4H)
    regime_ema_long: int = 100  # Long-term regime (~17 days on 4H)
    regime_rising_bars: int = 5 # EMA must be rising for 5 bars
    regime_min_slope_bps: float = 5.0  # EMA slope > 5 bps/bar

    # ── Entry: Dip Buying ────────────────────
    atr_period: int = 14
    dip_depth_min_atr: float = 0.5  # Price dipped 0.5-3 ATR from recent high
    dip_depth_max_atr: float = 4.0
    dip_lookback: int = 10         # Bars to find recent high
    confirm_bounce: bool = True    # Current bar must close green
    confirm_above_ema: bool = True # Must close above regime EMA

    # ── Volume ───────────────────────────────
    volume_lookback: int = 20
    min_volume_ratio: float = 0.8  # Not in dead volume (> 80% of avg)

    # ── Risk Management ──────────────────────
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 6.0
    tp_swing_mult: float = 1.5
    min_target_bps: float = 250.0
    min_rr: float = 1.5

    # ── Cooldown ─────────────────────────────
    min_bars_between: int = 6
    max_holding_bars: int = 60

    # ATR range
    min_atr_bps: float = 15.0
    max_atr_bps: float = 2000.0


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


# ═══════════════════════════════════════════
#  Regime Detection
# ═══════════════════════════════════════════

def _is_bull_regime(
    closes: List[float], ema_vals: List[float],
    ema_long: List[float], i: int, cfg: LongOnlyConfig,
) -> bool:
    """
    Bull regime = price above EMA + EMA rising + EMA above long-term EMA.
    """
    if math.isnan(ema_vals[i]) or math.isnan(ema_long[i]):
        return False

    # Price above regime EMA
    if closes[i] < ema_vals[i]:
        return False

    # Regime EMA above long-term EMA
    if ema_vals[i] < ema_long[i]:
        return False

    # EMA must be rising
    if i < cfg.regime_rising_bars:
        return False

    for j in range(1, cfg.regime_rising_bars + 1):
        if i - j < 0 or math.isnan(ema_vals[i - j]):
            return False
        if ema_vals[i] <= ema_vals[i - j]:
            return False

    # EMA slope check
    ema_prev = ema_vals[i - cfg.regime_rising_bars]
    if not math.isnan(ema_prev) and ema_prev > 0:
        slope_bps = (ema_vals[i] - ema_prev) / ema_prev * 10000 / cfg.regime_rising_bars
        if slope_bps < cfg.regime_min_slope_bps:
            return False

    return True


# ═══════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════

def make_long_only_fn(
    cfg: Optional[LongOnlyConfig] = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Factory: Long-Only Dip Buying in Bull Regime.

    Entry: Bull regime confirmed + price dips to support + bounces
    SL: Below dip low + ATR buffer
    TP: ATR-based + swing projection
    Direction: ALWAYS LONG
    """
    if cfg is None:
        cfg = LongOnlyConfig()

    last_signal_bar = [-1]

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        n = len(closed_bars)
        min_bars = cfg.regime_ema_long + 20
        if n < min_bars:
            return None

        if last_signal_bar[0] >= 0 and (n - last_signal_bar[0]) < cfg.min_bars_between:
            return None

        i = n - 1
        price = closed_bars[i].close
        if price <= 0:
            return None

        closes = [b.close for b in closed_bars]
        atr_vals = _atr(closed_bars, cfg.atr_period)
        ema_vals = _ema(closes, cfg.regime_ema)
        ema_long = _ema(closes, cfg.regime_ema_long)

        atr = atr_vals[i]
        if math.isnan(atr) or atr <= 0:
            return None

        atr_bps = atr / price * 10000
        if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
            return None

        # ── 1. Bull regime check ──
        if not _is_bull_regime(closes, ema_vals, ema_long, i, cfg):
            return None

        # ── 2. Dip detection ──
        lb = max(0, i - cfg.dip_lookback)
        recent_high = max(b.high for b in closed_bars[lb:i + 1])
        dip_depth = recent_high - price
        dip_depth_atr = dip_depth / atr if atr > 0 else 0

        if dip_depth_atr < cfg.dip_depth_min_atr or dip_depth_atr > cfg.dip_depth_max_atr:
            return None

        # ── 3. Bounce confirmation ──
        if cfg.confirm_bounce:
            if closed_bars[i].close <= closed_bars[i].open:
                return None

        if cfg.confirm_above_ema:
            if not math.isnan(ema_vals[i]) and price < ema_vals[i]:
                return None

        # ── 4. Volume check ──
        if i >= cfg.volume_lookback:
            avg_vol = sum(b.volume for b in closed_bars[i - cfg.volume_lookback:i]) / cfg.volume_lookback
            if avg_vol > 0 and closed_bars[i].volume < cfg.min_volume_ratio * avg_vol:
                return None

        # ── 5. SL/TP ──
        swing_low = min(b.low for b in closed_bars[lb:i + 1])
        sl = min(swing_low, price - cfg.sl_atr_mult * atr)

        swing_range = recent_high - swing_low
        tp = price + max(
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
            direction="long",
            entry_price=price,
            stop_loss=sl,
            take_profit=tp,
            setup_type="long_only_dip_buy",
            confidence=min(0.85, 0.5 + dip_depth_atr / 10.0),
            risk_reward=rr,
            timestamp=closed_bars[i].timestamp,
        )

    return signal_fn
