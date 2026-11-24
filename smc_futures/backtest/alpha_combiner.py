"""
Multi-Alpha Combiner + Strict Multi-Layer Filter
═════════════════════════════════════════════════
Combines all 3 alpha classes with strict filtering:
  - Regime filter (trending vs choppy)
  - Spread/volatility gate (already in each alpha)
  - Signal quality threshold
  - Daily limit + cooldown
  - Conflict resolution (multiple alphas on same bar)

Usage:
    from smc_futures.backtest.alpha_combiner import make_combined_signal_fn
    signal_fn = make_combined_signal_fn()
    result = pipeline.run("BTCUSDT", candles, signal_fn)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Callable

from smc_futures.backtest.engine import BTCandle, BacktestSignal

from smc_futures.backtest.alpha_contextual_reversal import (
    make_contextual_reversal_fn,
    ContextualReversalConfig,
)
from smc_futures.backtest.alpha_momentum_breakout import (
    make_momentum_breakout_fn,
    MomentumBreakoutConfig,
)
from smc_futures.backtest.alpha_liquidity_smc import (
    make_liquidity_smc_fn,
    LiquiditySMCConfig,
)


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


# ─── Regime Detection ────────────────────

def _detect_regime(candles: List[BTCandle], atr_period: int = 14) -> str:
    """
    Classify market regime:
    - 'trending': ADX equivalent > threshold (strong directional movement)
    - 'ranging': Low directional movement (choppy)
    - 'volatile': High ATR with no clear direction
    """
    if len(candles) < atr_period + 10:
        return "unknown"

    closes = [c.close for c in candles]
    atr = _compute_atr(candles, atr_period)
    price = closes[-1]
    if price <= 0:
        return "unknown"

    # Directional movement proxy: distance from start to end vs cumulative range
    lookback = min(50, len(closes) - 1)
    start_price = closes[-lookback - 1]
    end_price = closes[-1]
    net_move = abs(end_price - start_price)
    total_range = sum(abs(closes[i] - closes[i - 1]) for i in range(-lookback, 0))

    if total_range <= 0:
        return "ranging"

    efficiency = net_move / total_range  # 1.0 = perfect trend, 0.0 = noise

    atr_bps = (atr / price) * 10000

    if efficiency > 0.30 and atr_bps > 30:
        return "trending"
    elif atr_bps > 200:
        return "volatile"
    else:
        return "ranging"


# ─── Combiner Configuration ──────────────

@dataclass
class CombinerConfig:
    """Configuration for multi-alpha combiner."""
    # Enable/disable individual alphas
    enable_contextual_reversal: bool = True
    enable_momentum_breakout: bool = True
    enable_liquidity_smc: bool = True

    # Regime filter
    allow_reversal_in_ranging: bool = True
    allow_reversal_in_trending: bool = True
    allow_breakout_in_ranging: bool = False   # Breakout needs trend
    allow_breakout_in_trending: bool = True
    allow_smc_in_ranging: bool = True
    allow_smc_in_trending: bool = True

    # Quality gate
    min_confidence: float = 0.45
    min_rr: float = 2.0

    # Daily limits
    max_signals_per_day: int = 5
    global_cooldown_bars: int = 10   # Bars between ANY signal

    # Priority (lower = higher priority, used for conflict resolution)
    priority_contextual_reversal: int = 2
    priority_momentum_breakout: int = 3
    priority_liquidity_smc: int = 1  # Highest: most acute events


# ─── Combined Signal Factory ─────────────

def make_combined_signal_fn(
    combiner_cfg: CombinerConfig | None = None,
    reversal_cfg: ContextualReversalConfig | None = None,
    breakout_cfg: MomentumBreakoutConfig | None = None,
    smc_cfg: LiquiditySMCConfig | None = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Create a combined signal function that queries all 3 alphas,
    applies regime filter, quality gate, and returns the best signal.
    """
    if combiner_cfg is None:
        combiner_cfg = CombinerConfig()

    # Create individual alpha functions
    alpha_fns = []
    alpha_names = []
    alpha_priorities = []

    if combiner_cfg.enable_contextual_reversal:
        alpha_fns.append(make_contextual_reversal_fn(reversal_cfg))
        alpha_names.append("contextual_reversal")
        alpha_priorities.append(combiner_cfg.priority_contextual_reversal)

    if combiner_cfg.enable_momentum_breakout:
        alpha_fns.append(make_momentum_breakout_fn(breakout_cfg))
        alpha_names.append("momentum_breakout")
        alpha_priorities.append(combiner_cfg.priority_momentum_breakout)

    if combiner_cfg.enable_liquidity_smc:
        alpha_fns.append(make_liquidity_smc_fn(smc_cfg))
        alpha_names.append("liquidity_smc")
        alpha_priorities.append(combiner_cfg.priority_liquidity_smc)

    state = {
        "last_signal_bar": -999,
        "daily_counts": {},       # day_key -> count
    }

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        if len(closed_bars) < 130:
            return None

        bar_idx = len(closed_bars) - 1
        current_bar = closed_bars[-1]

        # Global cooldown
        if bar_idx - state["last_signal_bar"] < combiner_cfg.global_cooldown_bars:
            return None

        # Daily limit
        import datetime
        dt = datetime.datetime.fromtimestamp(current_bar.timestamp / 1000, tz=datetime.timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        if state["daily_counts"].get(day_key, 0) >= combiner_cfg.max_signals_per_day:
            return None

        # Detect regime
        regime = _detect_regime(closed_bars)

        # Collect candidate signals from all alphas
        candidates = []
        for fn, name, priority in zip(alpha_fns, alpha_names, alpha_priorities):
            # Regime gate
            if name == "contextual_reversal":
                if regime == "ranging" and not combiner_cfg.allow_reversal_in_ranging:
                    continue
                if regime == "trending" and not combiner_cfg.allow_reversal_in_trending:
                    continue
            elif name == "momentum_breakout":
                if regime == "ranging" and not combiner_cfg.allow_breakout_in_ranging:
                    continue
                if regime == "trending" and not combiner_cfg.allow_breakout_in_trending:
                    continue
            elif name == "liquidity_smc":
                if regime == "ranging" and not combiner_cfg.allow_smc_in_ranging:
                    continue
                if regime == "trending" and not combiner_cfg.allow_smc_in_trending:
                    continue

            signal = fn(closed_bars)
            if signal is not None:
                candidates.append((signal, priority))

        if not candidates:
            return None

        # Apply quality gate
        filtered = [
            (sig, prio) for sig, prio in candidates
            if sig.confidence >= combiner_cfg.min_confidence
            and sig.risk_reward >= combiner_cfg.min_rr
        ]

        if not filtered:
            return None

        # Pick best: lowest priority number (highest priority), then highest RR
        filtered.sort(key=lambda x: (x[1], -x[0].risk_reward))
        best_signal = filtered[0][0]

        # Update state
        state["last_signal_bar"] = bar_idx
        state["daily_counts"][day_key] = state["daily_counts"].get(day_key, 0) + 1

        return best_signal

    return signal_fn


# ─── Individual alpha factories (convenience wrappers) ─

def make_reversal_only_fn(
    cfg: ContextualReversalConfig | None = None
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """Signal function using only Contextual Reversal alpha."""
    combiner_cfg = CombinerConfig(
        enable_contextual_reversal=True,
        enable_momentum_breakout=False,
        enable_liquidity_smc=False,
    )
    return make_combined_signal_fn(combiner_cfg, reversal_cfg=cfg)


def make_breakout_only_fn(
    cfg: MomentumBreakoutConfig | None = None
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """Signal function using only Momentum Breakout alpha."""
    combiner_cfg = CombinerConfig(
        enable_contextual_reversal=False,
        enable_momentum_breakout=True,
        enable_liquidity_smc=False,
    )
    return make_combined_signal_fn(combiner_cfg, breakout_cfg=cfg)


def make_smc_only_fn(
    cfg: LiquiditySMCConfig | None = None
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """Signal function using only Liquidity/SMC alpha."""
    combiner_cfg = CombinerConfig(
        enable_contextual_reversal=False,
        enable_momentum_breakout=False,
        enable_liquidity_smc=True,
    )
    return make_combined_signal_fn(combiner_cfg, smc_cfg=cfg)
