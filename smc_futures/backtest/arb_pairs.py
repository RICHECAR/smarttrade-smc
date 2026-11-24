"""
AXE 4 — Pairs Trading (BTC vs ETH)
═══════════════════════════════════
Exploit cointegration between BTC and ETH perpetual futures:
  - Compute log spread: log(BTC) - β * log(ETH)
  - Estimate β via rolling OLS regression
  - Compute z-score of spread
  - Enter when |z| > entry_z → mean-reversion
  - Exit when |z| < exit_z or z flips

Key insight: BTC and ETH are highly correlated but not perfectly.
Divergences tend to revert, providing a statistical edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .arb_engine import ArbBar, ArbTrade, ArbConfig, ArbState, ArbMetrics, compute_arb_metrics


@dataclass
class PairsConfig:
    """Pairs trading parameters."""
    # Z-score thresholds
    entry_z: float = 2.0            # Enter when |z| > 2.0
    exit_z: float = 0.5             # Exit when |z| < 0.5
    # Rolling windows
    beta_window: int = 60           # 10 days at 4H (for regression)
    zscore_window: int = 30         # 5 days at 4H (for z-score)
    # Risk
    max_holding_bars: int = 36      # 6 days at 4H
    stop_loss_z: float = 4.0        # Cut if z-score moves to 4 (divergence expanding)
    # Position
    position_pct: float = 0.4
    leverage: int = 2
    cooldown_bars: int = 4
    # Min data
    warmup_bars: int = 60           # Need at least this many bars for regression


def _rolling_ols_beta(
    x: List[float],
    y: List[float],
    window: int,
) -> List[Optional[float]]:
    """
    Compute rolling OLS beta: y = α + β*x.
    Returns β for each position (None if insufficient data).
    """
    betas = [None] * len(x)
    for i in range(window - 1, len(x)):
        x_win = x[i - window + 1:i + 1]
        y_win = y[i - window + 1:i + 1]
        n = len(x_win)
        sum_x = sum(x_win)
        sum_y = sum(y_win)
        sum_xy = sum(a * b for a, b in zip(x_win, y_win))
        sum_xx = sum(a * a for a in x_win)
        denom = n * sum_xx - sum_x * sum_x
        if abs(denom) < 1e-15:
            betas[i] = 1.0
        else:
            betas[i] = (n * sum_xy - sum_x * sum_y) / denom
    return betas


def _rolling_zscore(
    series: List[float],
    window: int,
) -> List[Optional[float]]:
    """Compute rolling z-score."""
    zscores = [None] * len(series)
    for i in range(window - 1, len(series)):
        win = series[i - window + 1:i + 1]
        mean = sum(win) / len(win)
        var = sum((v - mean) ** 2 for v in win) / len(win)
        std = math.sqrt(var) if var > 0 else 1e-10
        zscores[i] = (series[i] - mean) / std
    return zscores


def build_arb_bars_pairs(
    btc_candles: list,
    eth_candles: list,
) -> List[ArbBar]:
    """
    Align BTC and ETH candles into ArbBar list.
    perp_close = BTC, sec_close = ETH.
    Assumes both lists are sorted by timestamp and have same length.
    """
    n = min(len(btc_candles), len(eth_candles))
    bars = []
    for i in range(n):
        bc = btc_candles[i]
        ec = eth_candles[i]

        ts = bc.timestamp if hasattr(bc, 'timestamp') else bc[0]
        o = bc.open if hasattr(bc, 'open') else bc[1]
        h = bc.high if hasattr(bc, 'high') else bc[2]
        lo = bc.low if hasattr(bc, 'low') else bc[3]
        cl = bc.close if hasattr(bc, 'close') else bc[4]
        vol = bc.volume if hasattr(bc, 'volume') else (bc[5] if len(bc) > 5 else 0)

        eth_cl = ec.close if hasattr(ec, 'close') else ec[4]

        bars.append(ArbBar(
            timestamp=ts,
            perp_open=o, perp_high=h, perp_low=lo, perp_close=cl,
            perp_volume=vol,
            sec_close=eth_cl,
        ))
    return bars


def run_pairs_trading(
    bars: List[ArbBar],
    config: PairsConfig = None,
    arb_config: ArbConfig = None,
) -> ArbState:
    """
    Run BTC vs ETH pairs trading backtest.

    Strategy:
      - Compute log_spread = log(BTC) - β * log(ETH), rolling β
      - Z-score of spread over zscore_window
      - z > entry_z → SHORT spread (short BTC, long ETH)
      - z < -entry_z → LONG spread (long BTC, short ETH)
      - Exit when |z| < exit_z or sign flips
    """
    if config is None:
        config = PairsConfig()
    if arb_config is None:
        arb_config = ArbConfig()

    state = ArbState(capital=arb_config.initial_capital)
    state.equity_curve.append(state.capital)
    cooldown_until = 0

    # Pre-compute log prices
    log_btc = [math.log(b.perp_close) if b.perp_close > 0 else 0 for b in bars]
    log_eth = [math.log(b.sec_close) if b.sec_close > 0 else 0 for b in bars]

    # Rolling beta
    betas = _rolling_ols_beta(log_eth, log_btc, config.beta_window)

    # Log spread: log(BTC) - β * log(ETH)
    log_spread = []
    for i in range(len(bars)):
        if betas[i] is not None:
            log_spread.append(log_btc[i] - betas[i] * log_eth[i])
        else:
            log_spread.append(0.0)

    # Z-score
    zscores = _rolling_zscore(log_spread, config.zscore_window)

    start_bar = config.warmup_bars

    for i in range(start_bar, len(bars)):
        bar = bars[i]
        z = zscores[i - 1]  # No look-ahead
        if z is None:
            state.equity_curve.append(state.capital)
            continue

        current_z = zscores[i] if zscores[i] is not None else z

        # ── Check exits ──
        if state.open_position is not None:
            pos = state.open_position
            holding = i - pos.entry_bar

            should_exit = False
            if abs(current_z) < config.exit_z:
                should_exit = True
            elif holding >= config.max_holding_bars:
                should_exit = True
            # Z-score moved further against (divergence expanding)
            elif abs(current_z) > config.stop_loss_z:
                should_exit = True
            # Z-score flipped sign
            elif pos.direction == "short_spread" and current_z < 0:
                should_exit = True
            elif pos.direction == "long_spread" and current_z > 0:
                should_exit = True

            if should_exit:
                # Dollar-neutral PnL: half notional per leg
                half_notional = pos.notional / 2
                entry_btc = bars[pos.entry_bar].perp_close
                exit_btc = bar.perp_close
                entry_eth = bars[pos.entry_bar].sec_close
                exit_eth = bar.sec_close

                btc_ret = (exit_btc - entry_btc) / entry_btc if entry_btc > 0 else 0
                eth_ret = (exit_eth - entry_eth) / entry_eth if entry_eth > 0 else 0

                if pos.direction == "short_spread":
                    # Short BTC, long ETH
                    pos.price_pnl = (-btc_ret + eth_ret) * half_notional
                else:
                    # Long BTC, short ETH
                    pos.price_pnl = (btc_ret - eth_ret) * half_notional

                pos.carry_pnl = 0.0  # No carry in pairs trading

                legs = 2  # Always two-legged for pairs
                entry_fee = arb_config.maker_fee_rate * pos.notional * legs
                exit_fee = arb_config.taker_fee_rate * pos.notional * legs
                pos.total_fees = entry_fee + exit_fee

                pos.slippage_cost = (
                    arb_config.entry_slippage_bps + arb_config.exit_slippage_bps
                ) / 10000 * pos.notional * legs

                pos.net_pnl = pos.price_pnl + pos.carry_pnl - pos.total_fees - pos.slippage_cost
                pos.exit_bar = i
                pos.exit_timestamp = bar.timestamp
                pos.exit_price = bar.perp_close
                pos.exit_spread = current_z
                pos.holding_bars = holding

                state.capital += pos.net_pnl
                state.trades.append(pos)
                state.open_position = None
                cooldown_until = i + config.cooldown_bars

        # ── Check entries ──
        elif i >= cooldown_until and z is not None:
            direction = None
            if z > config.entry_z:
                direction = "short_spread"  # Short BTC, long ETH
            elif z < -config.entry_z:
                direction = "long_spread"   # Long BTC, short ETH

            if direction is not None:
                notional = state.capital * config.position_pct * config.leverage
                if notional <= 0:
                    state.equity_curve.append(state.capital)
                    continue

                trade = ArbTrade(
                    entry_bar=i,
                    entry_timestamp=bar.timestamp,
                    strategy="pairs",
                    direction=direction,
                    entry_price=bar.perp_open,
                    entry_spread=z,
                    notional=notional,
                )
                state.open_position = trade

        state.equity_curve.append(state.capital)

    # Force close
    if state.open_position is not None:
        pos = state.open_position
        last = bars[-1]
        last_z = zscores[-1] if zscores[-1] is not None else 0

        half_notional = pos.notional / 2
        entry_btc = bars[pos.entry_bar].perp_close
        exit_btc = last.perp_close
        entry_eth = bars[pos.entry_bar].sec_close
        exit_eth = last.sec_close

        btc_ret = (exit_btc - entry_btc) / entry_btc if entry_btc > 0 else 0
        eth_ret = (exit_eth - entry_eth) / entry_eth if entry_eth > 0 else 0

        if pos.direction == "short_spread":
            pos.price_pnl = (-btc_ret + eth_ret) * half_notional
        else:
            pos.price_pnl = (btc_ret - eth_ret) * half_notional

        pos.carry_pnl = 0.0

        legs = 2
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


def _estimate_spread_std(
    log_spread: List[float],
    idx: int,
    window: int,
) -> float:
    """Estimate std of log-spread over window."""
    start = max(0, idx - window + 1)
    win = log_spread[start:idx + 1]
    if len(win) < 2:
        return 0.001
    mean = sum(win) / len(win)
    var = sum((v - mean) ** 2 for v in win) / len(win)
    return math.sqrt(var) if var > 0 else 0.001
