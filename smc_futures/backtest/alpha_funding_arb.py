"""
Chantier 13 — AXE 3: FUNDING RATE ARBITRAGE
=============================================

Core thesis:
  - Perp funding rate is a real, measurable edge
  - When funding > threshold → SHORT perp (collect funding)
  - When funding < -threshold → LONG perp (collect funding)
  - This is a structural market-neutral edge
  - Does NOT depend on price direction — pure carry

Strategy modes:
  A) FUNDING CARRY: Hold position that collects funding
     - Long when funding deeply negative (shorts pay longs)
     - Short when funding deeply positive (longs pay shorts)
  B) REGIME FUNDING: Combine directional bias WITH funding alignment
     - Only trade when direction AND funding agree

Key advantage:
  - Funding is structural income, not speculative alpha
  - Average 0.01% per 8h = 0.03%/day = ~10%/year on notional
  - Spikes to 0.1-0.5% per 8h during extreme sentiment
  - Low correlation with price movement
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Callable

from smc_futures.backtest.engine import BTCandle, BacktestSignal


@dataclass
class FundingConfig:
    # ── Funding Rate Estimation ──────────────
    # Since we don't have direct funding rate data, we estimate from
    # price momentum and OI proxy (volume burst)
    momentum_period: int = 24     # 24 bars lookback for momentum
    volume_ma_period: int = 20    # Volume moving average
    atr_period: int = 14

    # ── Funding Proxy Thresholds ─────────────
    # Strong positive momentum + high volume = likely positive funding
    # Strong negative momentum + high volume = likely negative funding
    momentum_threshold_bps: float = 200.0  # 200 bps momentum to qualify
    volume_surge_mult: float = 1.3         # Volume > 1.3x average

    # ── Entry: Counter-Funding Position ──────
    # Go SHORT when estimated funding is very positive (crowd is long)
    # Go LONG when estimated funding is very negative (crowd is short)
    entry_delay_bars: int = 2      # Wait 2 bars after detection
    ema_trend: int = 50            # Avoid counter-trend (optional)
    use_trend_filter: bool = False # If True, only trade WITH trend

    # ── Carry Duration ───────────────────────
    min_carry_bars: int = 6        # Hold at least 6 bars (1 day on 4H)
    max_carry_bars: int = 42       # Max 7 days on 4H

    # ── Risk Management ──────────────────────
    sl_atr_mult: float = 3.0      # Wider SL for carry trades
    tp_atr_mult: float = 4.0      # Moderate TP (funding accumulation matters more)
    min_target_bps: float = 200.0
    min_rr: float = 1.2

    # ── Cooldown ─────────────────────────────
    min_bars_between: int = 6
    min_atr_bps: float = 10.0
    max_atr_bps: float = 2000.0

    # ── Mode ─────────────────────────────────
    mode: str = "counter_crowd"  # "counter_crowd" or "trend_aligned"


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


def _roc(values: List[float], period: int) -> List[float]:
    """Rate of change in bps."""
    result = [float("nan")] * len(values)
    for i in range(period, len(values)):
        if values[i - period] > 0:
            result[i] = (values[i] - values[i - period]) / values[i - period] * 10000
    return result


# ═══════════════════════════════════════════
#  Funding Rate Estimation
# ═══════════════════════════════════════════

def _estimate_funding_regime(
    closes: List[float], volumes: List[float],
    i: int, cfg: FundingConfig,
) -> Optional[str]:
    """
    Estimate funding regime from momentum + volume.
    Returns 'positive' (crowd long), 'negative' (crowd short), or None.

    When crowd is heavily LONG:
      - Strong upward momentum
      - High volume (FOMO buying)
      - Funding likely positive → shorts collect
    When crowd is heavily SHORT:
      - Strong downward momentum
      - High volume (panic selling)
      - Funding likely negative → longs collect
    """
    if i < cfg.momentum_period or i < cfg.volume_ma_period:
        return None

    # Momentum
    mom = (closes[i] - closes[i - cfg.momentum_period]) / closes[i - cfg.momentum_period] * 10000
    if abs(mom) < cfg.momentum_threshold_bps:
        return None

    # Volume surge
    avg_vol = sum(volumes[i - cfg.volume_ma_period:i]) / cfg.volume_ma_period
    if avg_vol <= 0:
        return None
    vol_ratio = volumes[i] / avg_vol
    if vol_ratio < cfg.volume_surge_mult:
        return None

    if mom > cfg.momentum_threshold_bps:
        return "positive"  # Crowd long → funding positive
    elif mom < -cfg.momentum_threshold_bps:
        return "negative"  # Crowd short → funding negative

    return None


# ═══════════════════════════════════════════
#  Factory
# ═══════════════════════════════════════════

def make_funding_arb_fn(
    cfg: Optional[FundingConfig] = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Factory: Funding Rate Arbitrage / Carry alpha.

    counter_crowd mode:
      - Short when crowd is heavily long (collect positive funding)
      - Long when crowd is heavily short (collect negative funding)

    trend_aligned mode:
      - Only enter when funding + trend direction agree
    """
    if cfg is None:
        cfg = FundingConfig()

    last_signal_bar = [-1]
    pending_signal = [None]  # (direction, detection_bar)

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        n = len(closed_bars)
        min_bars = max(cfg.momentum_period, cfg.volume_ma_period, cfg.ema_trend) + 20
        if n < min_bars:
            return None

        if last_signal_bar[0] >= 0 and (n - last_signal_bar[0]) < cfg.min_bars_between:
            return None

        i = n - 1
        price = closed_bars[i].close
        if price <= 0:
            return None

        closes = [b.close for b in closed_bars]
        volumes = [b.volume for b in closed_bars]
        atr_vals = _atr(closed_bars, cfg.atr_period)

        atr = atr_vals[i]
        if math.isnan(atr) or atr <= 0:
            return None

        atr_bps = atr / price * 10000
        if atr_bps < cfg.min_atr_bps or atr_bps > cfg.max_atr_bps:
            return None

        # ── Check for pending signal ──
        if pending_signal[0] is not None:
            pend_dir, pend_bar = pending_signal[0]
            if (i - pend_bar) >= cfg.entry_delay_bars:
                pending_signal[0] = None
                # Execute the delayed entry
                direction = pend_dir
            elif (i - pend_bar) > cfg.entry_delay_bars + 3:
                pending_signal[0] = None  # Expired
                return None
            else:
                return None  # Still waiting
        else:
            # ── Detect funding regime ──
            regime = _estimate_funding_regime(closes, volumes, i, cfg)
            if regime is None:
                return None

            if cfg.mode == "counter_crowd":
                direction = "short" if regime == "positive" else "long"
            else:
                # trend_aligned: only if trend agrees
                ema_vals = _ema(closes, cfg.ema_trend)
                if math.isnan(ema_vals[i]):
                    return None
                if regime == "positive" and price > ema_vals[i]:
                    direction = "long"  # Trend up + crowd long
                elif regime == "negative" and price < ema_vals[i]:
                    direction = "short"  # Trend down + crowd short
                else:
                    return None

            if cfg.entry_delay_bars > 0:
                pending_signal[0] = (direction, i)
                return None

        # ── Entry now ──
        ema_trend = _ema(closes, cfg.ema_trend)
        if cfg.use_trend_filter and not math.isnan(ema_trend[i]):
            if direction == "long" and price < ema_trend[i]:
                return None
            if direction == "short" and price > ema_trend[i]:
                return None

        # ── SL/TP ──
        if direction == "long":
            sl = price - cfg.sl_atr_mult * atr
            tp = price + cfg.tp_atr_mult * atr
        else:
            sl = price + cfg.sl_atr_mult * atr
            tp = price - cfg.tp_atr_mult * atr

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
            setup_type=f"funding_{cfg.mode}_{direction}",
            confidence=0.6,
            risk_reward=rr,
            timestamp=closed_bars[i].timestamp,
        )

    return signal_fn
