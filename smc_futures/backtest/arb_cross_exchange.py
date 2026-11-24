"""
AXE 3 — Cross-Exchange Arbitrage
═════════════════════════════════
Exploit price divergences between exchanges:
  - Monitor BTC price on Exchange A (Bitget) vs Exchange B (simulated)
  - When spread > threshold → buy cheap, sell expensive
  - Exit when spread normalizes

Reality constraints:
  - No real multi-exchange data → simulate Exchange B with realistic spread model
  - Transfer latency: 1-3 bars (execution delay)
  - Both legs pay fees
  - Withdrawal fees on some paths

This module is SYNTHETIC — results should be interpreted as upper-bound estimates.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

from .arb_engine import ArbBar, ArbTrade, ArbConfig, ArbState, ArbMetrics, compute_arb_metrics


@dataclass
class CrossExConfig:
    """Cross-exchange arbitrage parameters."""
    # Spread thresholds (bps)
    entry_threshold_bps: float = 6.0    # Enter when |spread| > 6 bps
    exit_threshold_bps: float = 1.5     # Exit when |spread| < 1.5 bps
    # Execution delay (bars) — models transfer/settlement
    execution_delay_bars: int = 1
    # Risk
    max_holding_bars: int = 12          # 2 days at 4H
    stop_loss_bps: float = 40.0
    # Position
    position_pct: float = 0.3           # Conservative
    leverage: int = 2
    cooldown_bars: int = 2
    # Exchange B extra costs
    extra_fee_bps: float = 1.0          # Additional fee for 2nd exchange
    withdrawal_fee_flat: float = 5.0    # Fixed $ cost per round-trip


def simulate_exchange_b_prices(
    candles: list,
    spread_mean_bps: float = 0.0,      # No systematic bias
    spread_std_bps: float = 4.0,        # Typical inter-exchange noise
    mean_reversion: float = 0.25,       # Fast mean-reversion (arb forces)
    autocorrelation: float = 0.6,       # Spreads are somewhat persistent
    seed: int = 44,
) -> List[float]:
    """
    Simulate Exchange B price as Exchange A + realistic spread.
    Spread follows OU process with higher mean-reversion (arb forces).
    """
    rng = random.Random(seed)
    prices = []
    current_spread = spread_mean_bps

    for c in candles:
        close = c.close if hasattr(c, 'close') else c[4]
        noise = rng.gauss(0, spread_std_bps)
        current_spread = (
            current_spread * autocorrelation +
            mean_reversion * (spread_mean_bps - current_spread) +
            noise * math.sqrt(1 - autocorrelation ** 2)
        )
        # Exchange B price
        spread_decimal = current_spread / 10000
        exchange_b = close * (1 + spread_decimal)
        prices.append(exchange_b)

    return prices


def build_arb_bars_cross_ex(
    candles: list,
    exchange_b_prices: List[float],
) -> List[ArbBar]:
    """Convert BTCandle + Exchange B prices into ArbBar list."""
    bars = []
    for i, c in enumerate(candles):
        ts = c.timestamp if hasattr(c, 'timestamp') else c[0]
        o = c.open if hasattr(c, 'open') else c[1]
        h = c.high if hasattr(c, 'high') else c[2]
        lo = c.low if hasattr(c, 'low') else c[3]
        cl = c.close if hasattr(c, 'close') else c[4]
        vol = c.volume if hasattr(c, 'volume') else (c[5] if len(c) > 5 else 0)
        sec = exchange_b_prices[i] if i < len(exchange_b_prices) else cl
        bars.append(ArbBar(
            timestamp=ts,
            perp_open=o, perp_high=h, perp_low=lo, perp_close=cl,
            perp_volume=vol,
            sec_close=sec,
        ))
    return bars


def run_cross_exchange_arb(
    bars: List[ArbBar],
    config: CrossExConfig = None,
    arb_config: ArbConfig = None,
) -> ArbState:
    """
    Run cross-exchange arbitrage backtest.

    Strategy:
      - Compute spread = (A - B) / A * 10000 bps
      - When spread > threshold: short A, long B (A overpriced)
      - When spread < -threshold: long A, short B (B overpriced)
      - Exit when spread normalizes
    """
    if config is None:
        config = CrossExConfig()
    if arb_config is None:
        arb_config = ArbConfig()

    state = ArbState(capital=arb_config.initial_capital)
    state.equity_curve.append(state.capital)
    cooldown_until = 0
    pending_entry: Optional[dict] = None

    # Pre-compute spread series
    spread_series = []
    for b in bars:
        if b.sec_close > 0:
            spread = (b.perp_close - b.sec_close) / b.perp_close * 10000
        else:
            spread = 0.0
        spread_series.append(spread)

    for i in range(1, len(bars)):
        bar = bars[i]

        # ── Handle delayed entry execution ──
        if pending_entry is not None:
            if i >= pending_entry["execute_at"]:
                trade = ArbTrade(
                    entry_bar=i,
                    entry_timestamp=bar.timestamp,
                    strategy="cross_exchange",
                    direction=pending_entry["direction"],
                    entry_price=bar.perp_open,
                    entry_spread=spread_series[pending_entry["signal_bar"]],
                    notional=pending_entry["notional"],
                )
                state.open_position = trade
                pending_entry = None

        # ── Check exits ──
        if state.open_position is not None:
            pos = state.open_position
            holding = i - pos.entry_bar
            current_spread = spread_series[i]

            # Stop loss on price
            if pos.direction == "short_a_long_b":
                adverse = (bar.perp_high - pos.entry_price) / pos.entry_price * 10000
            else:
                adverse = (pos.entry_price - bar.perp_low) / pos.entry_price * 10000

            should_exit = False
            if adverse > config.stop_loss_bps:
                should_exit = True
            elif abs(current_spread) < config.exit_threshold_bps:
                should_exit = True
            elif holding >= config.max_holding_bars:
                should_exit = True

            if should_exit:
                exit_price = bar.perp_close
                if pos.direction == "short_a_long_b":
                    pos.price_pnl = (pos.entry_price - exit_price) / pos.entry_price * pos.notional
                else:
                    pos.price_pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.notional

                # Spread convergence profit
                spread_change = abs(pos.entry_spread) - abs(current_spread)
                pos.carry_pnl = spread_change / 10000 * pos.notional

                legs = 2 if arb_config.two_legged else 1
                entry_fee = arb_config.maker_fee_rate * pos.notional * legs
                exit_fee = arb_config.taker_fee_rate * pos.notional * legs
                extra_fee = config.extra_fee_bps / 10000 * pos.notional
                pos.total_fees = entry_fee + exit_fee + extra_fee + config.withdrawal_fee_flat

                pos.slippage_cost = (
                    arb_config.entry_slippage_bps + arb_config.exit_slippage_bps
                ) / 10000 * pos.notional * legs

                pos.net_pnl = pos.price_pnl + pos.carry_pnl - pos.total_fees - pos.slippage_cost
                pos.exit_bar = i
                pos.exit_timestamp = bar.timestamp
                pos.exit_price = exit_price
                pos.exit_spread = current_spread
                pos.holding_bars = holding

                state.capital += pos.net_pnl
                state.trades.append(pos)
                state.open_position = None
                cooldown_until = i + config.cooldown_bars

        # ── Check entries ──
        elif pending_entry is None and i >= cooldown_until:
            prev_spread = spread_series[i - 1]  # No look-ahead

            if abs(prev_spread) > config.entry_threshold_bps:
                notional = state.capital * config.position_pct * config.leverage
                if notional <= 0:
                    state.equity_curve.append(state.capital)
                    continue

                direction = "short_a_long_b" if prev_spread > 0 else "long_a_short_b"

                if config.execution_delay_bars > 0:
                    pending_entry = {
                        "direction": direction,
                        "notional": notional,
                        "signal_bar": i - 1,
                        "execute_at": i + config.execution_delay_bars,
                    }
                else:
                    trade = ArbTrade(
                        entry_bar=i,
                        entry_timestamp=bar.timestamp,
                        strategy="cross_exchange",
                        direction=direction,
                        entry_price=bar.perp_open,
                        entry_spread=prev_spread,
                        notional=notional,
                    )
                    state.open_position = trade

        state.equity_curve.append(state.capital)

    # Force close
    if state.open_position is not None:
        pos = state.open_position
        last = bars[-1]
        if pos.direction == "short_a_long_b":
            pos.price_pnl = (pos.entry_price - last.perp_close) / pos.entry_price * pos.notional
        else:
            pos.price_pnl = (last.perp_close - pos.entry_price) / pos.entry_price * pos.notional
        current_spread = spread_series[-1]
        pos.carry_pnl = (abs(pos.entry_spread) - abs(current_spread)) / 10000 * pos.notional
        legs = 2 if arb_config.two_legged else 1
        pos.total_fees = (arb_config.maker_fee_rate + arb_config.taker_fee_rate) * pos.notional * legs + config.extra_fee_bps / 10000 * pos.notional + config.withdrawal_fee_flat
        pos.slippage_cost = (arb_config.entry_slippage_bps + arb_config.exit_slippage_bps) / 10000 * pos.notional * legs
        pos.net_pnl = pos.price_pnl + pos.carry_pnl - pos.total_fees - pos.slippage_cost
        pos.exit_bar = len(bars) - 1
        pos.exit_timestamp = last.timestamp
        pos.exit_price = last.perp_close
        pos.holding_bars = pos.exit_bar - pos.entry_bar
        state.capital += pos.net_pnl
        state.trades.append(pos)
        state.open_position = None

    return state
