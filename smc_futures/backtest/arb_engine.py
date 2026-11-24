"""
Arbitrage Engine — Structural Edge Backtester
═══════════════════════════════════════════════
Backtests non-directional strategies that exploit market structure:
  - Funding rate carry
  - Perp/spot basis convergence
  - Cross-exchange price divergence
  - Pairs trading (spread mean-reversion)

Key difference from directional engine:
  - Positions can be two-legged (long + short simultaneously)
  - PnL comes from spread convergence AND carry, not direction
  - Maker fees assumed (limit order entry)
  - No SL/TP — positions exit on signal reversal or spread normalization
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────
#  Data Structures
# ───────────────────────────────────────────

@dataclass
class ArbBar:
    """Unified bar for arbitrage analysis."""
    timestamp: int
    perp_open: float
    perp_high: float
    perp_low: float
    perp_close: float
    perp_volume: float = 0.0
    # Spot leg (optional, for basis arb)
    spot_close: float = 0.0
    # Funding rate for this period (annualized equivalent is rate * 3 * 365)
    funding_rate: float = 0.0
    # Secondary instrument (for pairs/cross-exchange)
    sec_close: float = 0.0


@dataclass
class ArbTrade:
    """Record of an arbitrage trade."""
    entry_bar: int
    entry_timestamp: int
    strategy: str           # "funding", "basis", "cross_ex", "pairs"
    direction: str          # "long_carry", "short_carry", "long_basis", etc.
    # Entry
    entry_price: float = 0.0
    entry_spread: float = 0.0  # Spread at entry (bps)
    notional: float = 0.0
    # Exit
    exit_bar: int = 0
    exit_timestamp: int = 0
    exit_price: float = 0.0
    exit_spread: float = 0.0
    # PnL components
    price_pnl: float = 0.0       # From directional move
    carry_pnl: float = 0.0       # From funding/basis carry
    total_fees: float = 0.0      # Both legs
    slippage_cost: float = 0.0
    net_pnl: float = 0.0
    # Metadata
    holding_bars: int = 0
    funding_collected: float = 0.0
    bars_funding: List[float] = field(default_factory=list)


@dataclass
class ArbConfig:
    """Configuration for arbitrage backtester."""
    initial_capital: float = 10_000.0
    position_size_pct: float = 0.5      # Use 50% of capital per trade
    leverage: int = 3                    # Conservative for arb
    # Fees (maker for arb)
    maker_fee_rate: float = 0.0002      # 2 bps per leg
    taker_fee_rate: float = 0.0006      # 6 bps per leg (for urgent exits)
    # Slippage (very low for maker orders)
    entry_slippage_bps: float = 0.5     # Limit order entry
    exit_slippage_bps: float = 1.0      # May need taker exit
    # Two-legged cost multiplier (both legs pay fees)
    two_legged: bool = True
    # Bar duration
    bar_duration_ms: int = 28_800_000   # 8h default (funding period)


@dataclass
class ArbState:
    """Backtest state."""
    capital: float = 10_000.0
    equity_curve: List[float] = field(default_factory=list)
    trades: List[ArbTrade] = field(default_factory=list)
    open_position: Optional[ArbTrade] = None


# ───────────────────────────────────────────
#  Metrics
# ───────────────────────────────────────────

@dataclass
class ArbMetrics:
    """Performance metrics for arbitrage strategy."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_net_pnl: float = 0.0
    total_carry_pnl: float = 0.0
    total_price_pnl: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    avg_trade_pnl: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_holding_bars: float = 0.0
    pnl_stability: float = 0.0     # Std of trade PnLs / abs(mean)
    annualized_return_pct: float = 0.0
    cost_ratio: float = 0.0        # Total costs / gross profit


def compute_arb_metrics(
    state: ArbState,
    config: ArbConfig,
    total_bars: int,
) -> ArbMetrics:
    """Compute comprehensive arbitrage metrics."""
    m = ArbMetrics()
    trades = state.trades
    if not trades:
        return m

    m.total_trades = len(trades)
    pnls = [t.net_pnl for t in trades]
    m.winning_trades = sum(1 for p in pnls if p > 0)
    m.losing_trades = sum(1 for p in pnls if p <= 0)
    m.win_rate = m.winning_trades / m.total_trades * 100

    m.total_net_pnl = sum(pnls)
    m.total_carry_pnl = sum(t.carry_pnl for t in trades)
    m.total_price_pnl = sum(t.price_pnl for t in trades)
    m.total_fees = sum(t.total_fees for t in trades)
    m.total_slippage = sum(t.slippage_cost for t in trades)
    m.avg_trade_pnl = m.total_net_pnl / m.total_trades

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
        float('inf') if gross_profit > 0 else 0
    )

    # Drawdown from equity curve
    if state.equity_curve:
        peak = state.equity_curve[0]
        max_dd = 0.0
        for eq in state.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        m.max_drawdown_pct = max_dd

    # Sharpe
    if len(pnls) >= 2:
        avg = sum(pnls) / len(pnls)
        var = sum((p - avg) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(var) if var > 0 else 1e-10
        # Annualize: assume 3 funding periods per day
        periods_per_year = 365 * 3
        trades_per_period = m.total_trades / max(total_bars, 1)
        m.sharpe_ratio = (avg / std) * math.sqrt(
            trades_per_period * periods_per_year
        ) if std > 0 else 0

    m.avg_holding_bars = sum(t.holding_bars for t in trades) / m.total_trades

    # Stability
    if len(pnls) >= 2 and m.avg_trade_pnl != 0:
        std_pnl = math.sqrt(sum((p - m.avg_trade_pnl) ** 2 for p in pnls) / (len(pnls) - 1))
        m.pnl_stability = std_pnl / abs(m.avg_trade_pnl)
    else:
        m.pnl_stability = float('inf')

    # Annualized return
    bar_hours = config.bar_duration_ms / 3_600_000
    total_hours = total_bars * bar_hours
    total_years = total_hours / 8760 if total_hours > 0 else 1
    m.annualized_return_pct = (m.total_net_pnl / config.initial_capital * 100) / total_years

    # Cost ratio
    total_costs = m.total_fees + m.total_slippage
    m.cost_ratio = total_costs / gross_profit if gross_profit > 0 else float('inf')

    return m
