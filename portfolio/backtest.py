"""
Backtest Engine — Walk-forward portfolio simulation
════════════════════════════════════════════════════
Simulates the full portfolio allocation strategy on historical data.
Rebalances at configurable frequency (weekly/monthly).
Tracks equity, positions, turnover, costs.
Compares vs buy-and-hold benchmark (SPY).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

from portfolio.features import compute_all_features, FeatureConfig
from portfolio.signals import compute_composite_score, select_top_n, SignalConfig
from portfolio.allocator import compute_allocation, AllocatorConfig, WeightMethod
from portfolio.risk import apply_risk_overlay, RiskConfig
from portfolio.metrics import compute_all_metrics, format_metrics, MetricResult


@dataclass
class BacktestConfig:
    # Data
    period: str = "5y"

    # Feature / Signal
    feature_config: FeatureConfig = field(default_factory=FeatureConfig)
    signal_config: SignalConfig = field(default_factory=SignalConfig)

    # Allocation
    allocator_config: AllocatorConfig = field(default_factory=AllocatorConfig)

    # Risk
    risk_config: RiskConfig = field(default_factory=RiskConfig)

    # Rebalance
    rebal_freq: str = "M"   # "W" = weekly (Friday), "M" = monthly (last business day)

    # Costs
    cost_bps: float = 5.0   # 5 bps round-trip spread cost (realistic for liquid equities)

    # Benchmark
    benchmark: str = "SPY"

    # Warmup (trading days before first trade)
    warmup: int = 252  # 1 year of feature warmup


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    returns: pd.Series
    benchmark_equity: pd.Series
    benchmark_returns: pd.Series
    turnover_series: pd.Series
    positions_history: pd.DataFrame   # dates × tickers → weight
    strategy_metrics: MetricResult
    benchmark_metrics: MetricResult
    cost_drag: float   # Total costs as fraction of final equity
    in_cash_pct: float  # % of time in cash due to risk overlay


def _get_rebal_dates(dates: pd.DatetimeIndex, freq: str) -> list[pd.Timestamp]:
    """
    Get rebalancing dates from price index.
    W = last trading day of each week, M = last trading day of each month.
    """
    if freq == "W":
        s = dates.to_series()
        iso = dates.isocalendar()
        week_key = iso["year"].astype(str) + "-" + iso["week"].astype(str)
        groups = s.groupby(week_key.values)
        return [g.iloc[-1] for _, g in groups]
    elif freq == "M":
        groups = dates.to_series().groupby(dates.to_period("M"))
        return [g.iloc[-1] for _, g in groups]
    else:
        raise ValueError(f"Unknown rebal freq: {freq}")


def run_backtest(
    prices: pd.DataFrame,
    config: BacktestConfig = None,
) -> BacktestResult:
    """
    Run the full backtest.

    Parameters:
        prices: DataFrame with DatetimeIndex and ticker columns (adjusted close)
        config: BacktestConfig

    Returns:
        BacktestResult with equity curves, metrics, comparison
    """
    if config is None:
        config = BacktestConfig()

    dates = prices.index
    tickers = prices.columns.tolist()

    # ── 1. Compute features ───────────────────────────────────────
    features = compute_all_features(prices, config.feature_config)

    # ── 2. Compute composite scores ───────────────────────────────
    composite = compute_composite_score(features, config.signal_config)

    # ── 3. Get volatility for inverse-vol weighting ──────────────
    volatility_df = features.get("volatility", pd.DataFrame())

    # ── 4. Determine rebalance dates ─────────────────────────────
    tradeable_dates = dates[config.warmup:]
    if len(tradeable_dates) == 0:
        raise ValueError("Not enough data after warmup period.")

    rebal_dates = _get_rebal_dates(tradeable_dates, config.rebal_freq)

    # ── 5. Walk forward ──────────────────────────────────────────
    daily_returns = prices.pct_change().fillna(0.0)

    # Portfolio state
    current_weights: dict[str, float] = {}
    equity = 1.0
    in_cash = False

    # Tracking lists
    equity_list = []
    return_list = []
    turnover_list = []
    position_records = []
    date_list = []
    total_cost = 0.0
    cash_days = 0

    rebal_set = set(rebal_dates)

    for i, date in enumerate(tradeable_dates):
        # Daily return from existing positions
        daily_ret = 0.0
        for t, w in current_weights.items():
            if t in daily_returns.columns:
                daily_ret += w * daily_returns.loc[date, t]

        # Cash portion earns nothing (simplified)
        cash_weight = 1.0 - sum(current_weights.values())
        # daily_ret already accounts for invested portion only

        equity *= (1.0 + daily_ret)

        # Collect for risk overlay
        equity_list.append(equity)
        return_list.append(daily_ret)
        date_list.append(date)

        if in_cash:
            cash_days += 1

        # Record positions
        pos = {t: current_weights.get(t, 0.0) for t in tickers}
        position_records.append(pos)

        # ── Rebalance? ──
        if date in rebal_set and date in composite.index:
            scores = composite.loc[date].dropna()

            if len(scores) == 0:
                turnover_list.append(0.0)
                continue

            vol_series = None
            if not volatility_df.empty and date in volatility_df.index:
                vol_series = volatility_df.loc[date]

            # Compute new weights
            new_weights = compute_allocation(
                scores, vol_series, config.allocator_config,
            )

            # Risk overlay
            eq_series = pd.Series(equity_list, index=date_list)
            ret_series = pd.Series(return_list, index=date_list)
            new_weights, in_cash = apply_risk_overlay(
                new_weights, ret_series, eq_series, in_cash, config.risk_config,
            )

            # Turnover and costs
            all_tickers = set(current_weights.keys()) | set(new_weights.keys())
            turnover = sum(
                abs(new_weights.get(t, 0.0) - current_weights.get(t, 0.0))
                for t in all_tickers
            ) / 2.0  # one-way
            cost = turnover * 2 * config.cost_bps / 10_000  # round-trip
            equity *= (1.0 - cost)
            total_cost += cost * equity
            equity_list[-1] = equity

            turnover_list.append(turnover)
            current_weights = new_weights
        else:
            turnover_list.append(0.0)

    # ── 6. Build results ─────────────────────────────────────────
    eq = pd.Series(equity_list, index=date_list, name="portfolio")
    rets = pd.Series(return_list, index=date_list, name="returns")
    turnover_s = pd.Series(turnover_list, index=date_list, name="turnover")
    positions_df = pd.DataFrame(position_records, index=date_list)

    # ── 7. Benchmark ─────────────────────────────────────────────
    bench = config.benchmark
    if bench in prices.columns:
        bench_prices = prices.loc[tradeable_dates, bench]
        bench_eq = bench_prices / bench_prices.iloc[0]
        bench_rets = bench_prices.pct_change().fillna(0.0)
    else:
        bench_eq = pd.Series(1.0, index=tradeable_dates)
        bench_rets = pd.Series(0.0, index=tradeable_dates)

    # Only keep rebalance turnover for averaging
    rebal_turnover = turnover_s[turnover_s > 0]

    strat_metrics = compute_all_metrics(eq, rets, rebal_turnover)
    bench_metrics = compute_all_metrics(bench_eq, bench_rets)

    return BacktestResult(
        equity_curve=eq,
        returns=rets,
        benchmark_equity=bench_eq,
        benchmark_returns=bench_rets,
        turnover_series=turnover_s,
        positions_history=positions_df,
        strategy_metrics=strat_metrics,
        benchmark_metrics=bench_metrics,
        cost_drag=total_cost,
        in_cash_pct=cash_days / max(len(tradeable_dates), 1),
    )


def print_comparison(result: BacktestResult) -> str:
    """Print side-by-side strategy vs benchmark comparison."""
    s = result.strategy_metrics
    b = result.benchmark_metrics

    lines = [
        "",
        "╔══════════════════════════════════════════════════════════╗",
        "║     PORTFOLIO BACKTEST — STRATEGY vs BUY & HOLD (SPY)  ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  {'Metric':<20} {'Strategy':>12} {'Benchmark':>12}    ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  {'CAGR':<20} {s.cagr:>11.2%} {b.cagr:>12.2%}    ║",
        f"║  {'Sharpe':<20} {s.sharpe:>12.2f} {b.sharpe:>12.2f}    ║",
        f"║  {'Sortino':<20} {s.sortino:>12.2f} {b.sortino:>12.2f}    ║",
        f"║  {'Max Drawdown':<20} {s.max_drawdown:>11.2%} {b.max_drawdown:>12.2%}    ║",
        f"║  {'Calmar':<20} {s.calmar:>12.2f} {b.calmar:>12.2f}    ║",
        f"║  {'Annual Vol':<20} {s.annual_vol:>11.2%} {b.annual_vol:>12.2%}    ║",
        f"║  {'Total Return':<20} {s.total_return:>11.2%} {b.total_return:>12.2%}    ║",
        f"║  {'Avg Turnover/Reb':<20} {s.avg_turnover:>11.2%} {'N/A':>12}    ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Cost Drag (total)  : {result.cost_drag:>8.4f}                       ║",
        f"║  Time in Cash       : {result.in_cash_pct:>8.2%}                       ║",
        "╠══════════════════════════════════════════════════════════╣",
    ]

    # Verdict
    robust = (
        s.sharpe > b.sharpe
        and s.cagr > b.cagr * 0.9   # Allow 10% CAGR slack
        and s.max_drawdown < b.max_drawdown * 1.2  # Allow 20% DD slack
        and s.sharpe > 0.5
    )

    verdict = "ROBUST ✓" if robust else "NOT ROBUST ✗"
    color_line = f"║  VERDICT: {verdict:<46} ║"
    lines.append(color_line)
    lines.append("╚══════════════════════════════════════════════════════════╝")
    lines.append("")

    return "\n".join(lines)
