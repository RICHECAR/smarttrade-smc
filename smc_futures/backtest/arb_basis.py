"""
AXE 2 — Perp vs Spot Basis Arbitrage
═════════════════════════════════════
Exploit deviations between perpetual futures and spot price:
  - basis = (perp - spot) / spot * 10000  (in bps)
  - When basis > threshold → SHORT perp (overvalued vs spot, converge down)
  - When basis < -threshold → LONG perp (undervalued vs spot, converge up)
  - Exit when basis normalizes

Data: Bitget measured basis BTC: -4.5 bps (slight backwardation)
Typical range: -15 to +15 bps with mean-reversion.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

from .arb_engine import ArbBar, ArbTrade, ArbConfig, ArbState, ArbMetrics, compute_arb_metrics


@dataclass
class BasisArbConfig:
    """Basis arbitrage parameters."""
    # Entry: trade when |basis| exceeds this (bps)
    entry_threshold_bps: float = 8.0
    # Exit: close when |basis| < this or basis crosses zero
    exit_threshold_bps: float = 2.0
    # Risk
    max_holding_bars: int = 24      # 4 days at 4H
    stop_loss_bps: float = 60.0     # Price adverse move limit
    # Position
    position_pct: float = 0.4
    leverage: int = 2               # Conservative for basis
    cooldown_bars: int = 3
    # MA smoothing for basis
    basis_ma_period: int = 6        # 24h MA on 4H bars


def simulate_spot_prices(
    candles: list,
    basis_mean_bps: float = -2.0,   # Slight backwardation
    basis_std_bps: float = 6.0,
    mean_reversion: float = 0.12,
    seed: int = 43,
) -> List[float]:
    """
    Generate realistic spot prices from perp prices + simulated basis.
    basis = (perp - spot) / spot, so spot = perp / (1 + basis)
    """
    rng = random.Random(seed)
    spot_prices = []
    current_basis = basis_mean_bps

    for c in candles:
        close = c.close if hasattr(c, 'close') else c[4]
        # OU process for basis
        noise = rng.gauss(0, basis_std_bps)
        current_basis = (
            current_basis +
            mean_reversion * (basis_mean_bps - current_basis) +
            noise * math.sqrt(1 - mean_reversion ** 2)
        )
        # Spot = perp / (1 + basis_decimal)
        basis_decimal = current_basis / 10000
        spot = close / (1 + basis_decimal)
        spot_prices.append(spot)

    return spot_prices


def build_arb_bars_basis(
    candles: list,
    spot_prices: List[float],
) -> List[ArbBar]:
    """Convert BTCandle + spot prices into ArbBar list."""
    bars = []
    for i, c in enumerate(candles):
        ts = c.timestamp if hasattr(c, 'timestamp') else c[0]
        o = c.open if hasattr(c, 'open') else c[1]
        h = c.high if hasattr(c, 'high') else c[2]
        lo = c.low if hasattr(c, 'low') else c[3]
        cl = c.close if hasattr(c, 'close') else c[4]
        vol = c.volume if hasattr(c, 'volume') else (c[5] if len(c) > 5 else 0)
        spot = spot_prices[i] if i < len(spot_prices) else cl
        bars.append(ArbBar(
            timestamp=ts,
            perp_open=o, perp_high=h, perp_low=lo, perp_close=cl,
            perp_volume=vol,
            spot_close=spot,
        ))
    return bars


def _compute_basis_bps(perp: float, spot: float) -> float:
    """Compute basis in bps."""
    if spot <= 0:
        return 0.0
    return (perp - spot) / spot * 10000


def run_basis_arb(
    bars: List[ArbBar],
    config: BasisArbConfig = None,
    arb_config: ArbConfig = None,
) -> ArbState:
    """
    Run perp vs spot basis arbitrage backtest.

    Strategy:
      - Compute rolling basis MA
      - SHORT perp when basis_ma > entry_threshold (perp overvalued)
      - LONG perp when basis_ma < -entry_threshold (perp undervalued)
      - Exit when basis normalizes or max holding reached
    """
    if config is None:
        config = BasisArbConfig()
    if arb_config is None:
        arb_config = ArbConfig()

    state = ArbState(capital=arb_config.initial_capital)
    state.equity_curve.append(state.capital)
    cooldown_until = 0

    # Pre-compute basis series
    basis_series = [
        _compute_basis_bps(b.perp_close, b.spot_close) for b in bars
    ]

    # Compute basis MA
    basis_ma = []
    for i in range(len(bars)):
        start = max(0, i - config.basis_ma_period + 1)
        window = basis_series[start:i + 1]
        basis_ma.append(sum(window) / len(window))

    for i in range(config.basis_ma_period, len(bars)):
        bar = bars[i]
        bma = basis_ma[i - 1]  # No look-ahead: use previous bar's MA

        # ── Check exits ──
        if state.open_position is not None:
            pos = state.open_position
            holding = i - pos.entry_bar
            current_basis = basis_series[i]

            # Stop loss
            if pos.direction == "short_basis":
                adverse = (bar.perp_high - pos.entry_price) / pos.entry_price * 10000
            else:
                adverse = (pos.entry_price - bar.perp_low) / pos.entry_price * 10000

            should_exit = False
            if adverse > config.stop_loss_bps:
                should_exit = True
            elif abs(current_basis) < config.exit_threshold_bps:
                should_exit = True
            elif holding >= config.max_holding_bars:
                should_exit = True
            # Basis flipped
            elif pos.direction == "short_basis" and current_basis < -config.entry_threshold_bps:
                should_exit = True
            elif pos.direction == "long_basis" and current_basis > config.entry_threshold_bps:
                should_exit = True

            if should_exit:
                exit_price = bar.perp_close
                if pos.direction == "short_basis":
                    pos.price_pnl = (pos.entry_price - exit_price) / pos.entry_price * pos.notional
                else:
                    pos.price_pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.notional

                # Basis convergence profit (main arb revenue)
                basis_change = abs(pos.entry_spread) - abs(current_basis)
                pos.carry_pnl = basis_change / 10000 * pos.notional

                legs = 2 if arb_config.two_legged else 1
                entry_fee = arb_config.maker_fee_rate * pos.notional * legs
                exit_fee = arb_config.taker_fee_rate * pos.notional * legs
                pos.total_fees = entry_fee + exit_fee
                pos.slippage_cost = (
                    arb_config.entry_slippage_bps + arb_config.exit_slippage_bps
                ) / 10000 * pos.notional * legs

                pos.net_pnl = pos.price_pnl + pos.carry_pnl - pos.total_fees - pos.slippage_cost
                pos.exit_bar = i
                pos.exit_timestamp = bar.timestamp
                pos.exit_price = exit_price
                pos.exit_spread = current_basis
                pos.holding_bars = holding

                state.capital += pos.net_pnl
                state.trades.append(pos)
                state.open_position = None
                cooldown_until = i + config.cooldown_bars

        # ── Check entries ──
        elif i >= cooldown_until:
            if bma > config.entry_threshold_bps:
                # Basis positive and high → short perp (overvalued)
                direction = "short_basis"
            elif bma < -config.entry_threshold_bps:
                # Basis negative and extreme → long perp (undervalued)
                direction = "long_basis"
            else:
                state.equity_curve.append(state.capital)
                continue

            notional = state.capital * config.position_pct * config.leverage
            if notional <= 0:
                state.equity_curve.append(state.capital)
                continue

            trade = ArbTrade(
                entry_bar=i,
                entry_timestamp=bar.timestamp,
                strategy="basis",
                direction=direction,
                entry_price=bar.perp_open,
                entry_spread=bma,
                notional=notional,
            )
            state.open_position = trade

        state.equity_curve.append(state.capital)

    # Force close
    if state.open_position is not None:
        pos = state.open_position
        last = bars[-1]
        if pos.direction == "short_basis":
            pos.price_pnl = (pos.entry_price - last.perp_close) / pos.entry_price * pos.notional
        else:
            pos.price_pnl = (last.perp_close - pos.entry_price) / pos.entry_price * pos.notional

        current_basis = basis_series[-1]
        basis_change = abs(pos.entry_spread) - abs(current_basis)
        pos.carry_pnl = basis_change / 10000 * pos.notional

        legs = 2 if arb_config.two_legged else 1
        pos.total_fees = (arb_config.maker_fee_rate + arb_config.taker_fee_rate) * pos.notional * legs
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
