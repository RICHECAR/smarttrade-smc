"""
AXE 1 — Funding Rate Arbitrage
═══════════════════════════════
Exploit extreme funding rates by taking the other side:
  - When funding > threshold → SHORT perp (crowd is long, collect positive funding)
  - When funding < -threshold → LONG perp (crowd is short, collect negative funding)
  - Exit when funding normalizes toward zero

Revenue model:
  Net PnL = Σ(funding_collected) + price_pnl - entry_fee - exit_fee - slippage

Data from Bitget API (BTC):
  - Mean funding: 0.0003%/8h (~near zero)
  - Std: 0.0063%/8h
  - Extreme events (>|0.01%|): ~5-7% of periods
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .arb_engine import ArbBar, ArbTrade, ArbConfig, ArbState, ArbMetrics, compute_arb_metrics


@dataclass
class FundingArbConfig:
    """Funding rate arbitrage parameters."""
    # Thresholds (absolute, per funding period = 8h)
    entry_threshold_bps: float = 1.0    # Enter when |funding| > 1 bps/8h
    exit_threshold_bps: float = 0.3     # Exit when |funding| < 0.3 bps/8h
    # Risk management
    max_holding_bars: int = 18          # Max hold = 18 bars (3 days at 4H)
    stop_loss_bps: float = 50.0         # Cut loss if price moves 50 bps against
    # Funding period (every N bars)
    funding_every_n_bars: int = 2       # On 4H bars, funding every 2 bars (8h)
    # Position
    position_pct: float = 0.5           # 50% capital deployed
    leverage: int = 3
    # Cooldown
    cooldown_bars: int = 2              # Min bars between trades


def simulate_funding_rates(
    n_bars: int,
    mean: float = 0.03,          # 0.03 bps per 8h (near zero)
    std: float = 0.63,           # 0.63 bps per 8h
    mean_reversion: float = 0.15,  # OU mean-reversion speed
    seed: int = 42,
) -> List[float]:
    """
    Generate realistic funding rates using Ornstein-Uhlenbeck process.
    Returns funding rate per bar in BPS.
    Funding only applies every 2 bars (8h) but we return per-bar for simplicity.
    """
    rng = random.Random(seed)
    rates = []
    current = mean
    for _ in range(n_bars):
        noise = rng.gauss(0, std)
        current = current + mean_reversion * (mean - current) + noise * math.sqrt(1 - mean_reversion ** 2)
        rates.append(current)
    return rates


def build_arb_bars_funding(
    candles: list,
    funding_rates: List[float],
) -> List[ArbBar]:
    """Convert BTCandle + funding rates into ArbBar list."""
    bars = []
    for i, c in enumerate(candles):
        ts = c.timestamp if hasattr(c, 'timestamp') else c[0]
        o = c.open if hasattr(c, 'open') else c[1]
        h = c.high if hasattr(c, 'high') else c[2]
        lo = c.low if hasattr(c, 'low') else c[3]
        cl = c.close if hasattr(c, 'close') else c[4]
        vol = c.volume if hasattr(c, 'volume') else (c[5] if len(c) > 5 else 0)
        fr = funding_rates[i] if i < len(funding_rates) else 0.0
        bars.append(ArbBar(
            timestamp=ts,
            perp_open=o, perp_high=h, perp_low=lo, perp_close=cl,
            perp_volume=vol,
            funding_rate=fr,
        ))
    return bars


def run_funding_arb(
    bars: List[ArbBar],
    config: FundingArbConfig = None,
    arb_config: ArbConfig = None,
) -> ArbState:
    """
    Run funding rate arbitrage backtest.

    Strategy:
      - SHORT when funding > entry_threshold (crowd is long paying shorts)
      - LONG when funding < -entry_threshold (crowd is short paying longs)
      - Exit when |funding| < exit_threshold or max holding reached
      - Exit on stop-loss if price moves against position
    """
    if config is None:
        config = FundingArbConfig()
    if arb_config is None:
        arb_config = ArbConfig()

    state = ArbState(capital=arb_config.initial_capital)
    state.equity_curve.append(state.capital)
    cooldown_until = 0

    for i in range(1, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]

        # ── Check exits for open position ──
        if state.open_position is not None:
            pos = state.open_position
            holding = i - pos.entry_bar

            # Stop loss check (price adverse move)
            if pos.direction == "short_carry":
                adverse_pct = (bar.perp_high - pos.entry_price) / pos.entry_price * 10000
            else:
                adverse_pct = (pos.entry_price - bar.perp_low) / pos.entry_price * 10000

            # Collect funding if this is a funding bar
            if i % config.funding_every_n_bars == 0:
                # Funding income: position earns funding when betting against crowd
                fr_bps = bar.funding_rate
                if pos.direction == "short_carry":
                    # Short position earns positive funding
                    funding_income = fr_bps / 10000 * pos.notional
                else:
                    # Long position earns when funding is negative
                    funding_income = -fr_bps / 10000 * pos.notional
                pos.carry_pnl += funding_income
                pos.funding_collected += funding_income
                pos.bars_funding.append(fr_bps)

            should_exit = False
            exit_reason = ""

            # Stop loss
            if adverse_pct > config.stop_loss_bps:
                should_exit = True
                exit_reason = "stop_loss"

            # Funding normalized
            elif abs(bar.funding_rate) < config.exit_threshold_bps:
                should_exit = True
                exit_reason = "funding_normalized"

            # Max holding
            elif holding >= config.max_holding_bars:
                should_exit = True
                exit_reason = "max_holding"

            # Funding flipped direction
            elif (pos.direction == "short_carry" and bar.funding_rate < -config.entry_threshold_bps):
                should_exit = True
                exit_reason = "funding_flipped"
            elif (pos.direction == "long_carry" and bar.funding_rate > config.entry_threshold_bps):
                should_exit = True
                exit_reason = "funding_flipped"

            if should_exit:
                # Close position
                exit_price = bar.perp_close
                if pos.direction == "short_carry":
                    pos.price_pnl = (pos.entry_price - exit_price) / pos.entry_price * pos.notional
                else:
                    pos.price_pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.notional

                # Fees: entry + exit, both legs if two-legged
                legs = 2 if arb_config.two_legged else 1
                entry_fee = arb_config.maker_fee_rate * pos.notional * legs
                exit_fee = arb_config.taker_fee_rate * pos.notional * legs  # Taker for exit
                pos.total_fees = entry_fee + exit_fee

                # Slippage
                pos.slippage_cost = (
                    arb_config.entry_slippage_bps / 10000 * pos.notional +
                    arb_config.exit_slippage_bps / 10000 * pos.notional
                ) * legs

                pos.net_pnl = pos.price_pnl + pos.carry_pnl - pos.total_fees - pos.slippage_cost
                pos.exit_bar = i
                pos.exit_timestamp = bar.timestamp
                pos.exit_price = exit_price
                pos.exit_spread = bar.funding_rate
                pos.holding_bars = holding

                state.capital += pos.net_pnl
                state.trades.append(pos)
                state.open_position = None
                cooldown_until = i + config.cooldown_bars

        # ── Check entry signals ──
        elif i >= cooldown_until:
            fr = prev.funding_rate  # Use previous bar's funding (no look-ahead)

            if abs(fr) > config.entry_threshold_bps:
                notional = state.capital * config.position_pct * config.leverage
                if notional <= 0:
                    continue

                if fr > config.entry_threshold_bps:
                    direction = "short_carry"
                else:
                    direction = "long_carry"

                trade = ArbTrade(
                    entry_bar=i,
                    entry_timestamp=bar.timestamp,
                    strategy="funding",
                    direction=direction,
                    entry_price=bar.perp_open,
                    entry_spread=fr,
                    notional=notional,
                )
                state.open_position = trade

        state.equity_curve.append(state.capital)

    # Force close any open position at end
    if state.open_position is not None:
        pos = state.open_position
        last_bar = bars[-1]
        if pos.direction == "short_carry":
            pos.price_pnl = (pos.entry_price - last_bar.perp_close) / pos.entry_price * pos.notional
        else:
            pos.price_pnl = (last_bar.perp_close - pos.entry_price) / pos.entry_price * pos.notional

        legs = 2 if arb_config.two_legged else 1
        pos.total_fees = (arb_config.maker_fee_rate + arb_config.taker_fee_rate) * pos.notional * legs
        pos.slippage_cost = (arb_config.entry_slippage_bps + arb_config.exit_slippage_bps) / 10000 * pos.notional * legs
        pos.net_pnl = pos.price_pnl + pos.carry_pnl - pos.total_fees - pos.slippage_cost
        pos.exit_bar = len(bars) - 1
        pos.exit_timestamp = last_bar.timestamp
        pos.exit_price = last_bar.perp_close
        pos.holding_bars = pos.exit_bar - pos.entry_bar
        state.capital += pos.net_pnl
        state.trades.append(pos)
        state.open_position = None

    return state
