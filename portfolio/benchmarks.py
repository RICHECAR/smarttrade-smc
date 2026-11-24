"""
Extended Benchmarks — Compare strategy against tougher baselines
═══════════════════════════════════════════════════════════════
SPY, QQQ, Equal Weight Universe, Naïve Top10, 60/40, Dual Momentum.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from portfolio.metrics import compute_all_metrics, MetricResult


def _equity_from_prices(prices_series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Returns (equity_curve, daily_returns) from a price series."""
    rets = prices_series.pct_change().fillna(0.0)
    eq = (1 + rets).cumprod()
    return eq, rets


def benchmark_buy_hold(prices: pd.DataFrame, ticker: str) -> MetricResult | None:
    """Buy & hold a single ticker."""
    if ticker not in prices.columns:
        return None
    eq, rets = _equity_from_prices(prices[ticker])
    return compute_all_metrics(eq, rets)


def benchmark_equal_weight_universe(prices: pd.DataFrame) -> MetricResult:
    """Equal weight all tickers in universe, rebalanced daily (theoretical best EW)."""
    rets = prices.pct_change().fillna(0.0)
    port_ret = rets.mean(axis=1)
    eq = (1 + port_ret).cumprod()
    return compute_all_metrics(eq, port_ret)


def benchmark_top10_naive(prices: pd.DataFrame, lookback: int = 126) -> MetricResult:
    """
    Naïve momentum top-10: each month, pick top 10 by 6M return, equal weight.
    No signal engine, no scoring — just raw momentum.
    """
    rets = prices.pct_change().fillna(0.0)
    mom = prices.pct_change(lookback)

    monthly_dates = prices.index.to_series().groupby(prices.index.to_period("M"))
    rebal_dates = [g.iloc[-1] for _, g in monthly_dates]

    current_weights = {}
    port_rets = []
    port_dates = []

    warmup = lookback + 10
    for date in prices.index[warmup:]:
        # Daily return
        daily_ret = sum(
            w * rets.loc[date, t] for t, w in current_weights.items() if t in rets.columns
        )
        port_rets.append(daily_ret)
        port_dates.append(date)

        if date in rebal_dates and date in mom.index:
            scores = mom.loc[date].dropna().sort_values(ascending=False)
            top10 = scores.index[:10].tolist()
            w = 1.0 / len(top10) if top10 else 0.0
            current_weights = {t: w for t in top10}

    if not port_rets:
        return MetricResult(0, 0, 0, 0, 0, 0, 0, 0)

    port_ret_series = pd.Series(port_rets, index=port_dates)
    eq = (1 + port_ret_series).cumprod()
    return compute_all_metrics(eq, port_ret_series)


def benchmark_60_40(prices: pd.DataFrame) -> MetricResult | None:
    """60% SPY / 40% BND (or TLT), rebalanced monthly."""
    equity_ticker = "SPY"
    bond_ticker = "BND" if "BND" in prices.columns else ("TLT" if "TLT" in prices.columns else None)

    if equity_ticker not in prices.columns or bond_ticker is None:
        return None

    rets = prices[[equity_ticker, bond_ticker]].pct_change().fillna(0.0)
    port_ret = 0.60 * rets[equity_ticker] + 0.40 * rets[bond_ticker]
    eq = (1 + port_ret).cumprod()
    return compute_all_metrics(eq, port_ret)


def benchmark_dual_momentum(
    prices: pd.DataFrame,
    lookback: int = 126,
) -> MetricResult:
    """
    Simple Dual Momentum (Antonacci):
    - If SPY > BND over lookback: 100% SPY
    - Else: 100% BND
    Monthly rebalance.
    """
    spy = "SPY"
    bnd = "BND" if "BND" in prices.columns else ("TLT" if "TLT" in prices.columns else None)

    if spy not in prices.columns or bnd is None:
        return MetricResult(0, 0, 0, 0, 0, 0, 0, 0)

    rets = prices[[spy, bnd]].pct_change().fillna(0.0)
    mom_spy = prices[spy].pct_change(lookback)
    mom_bnd = prices[bnd].pct_change(lookback)

    monthly = prices.index.to_series().groupby(prices.index.to_period("M"))
    rebal_dates = set(g.iloc[-1] for _, g in monthly)

    current_asset = spy
    port_rets = []
    port_dates = []
    warmup = lookback + 10

    for date in prices.index[warmup:]:
        port_rets.append(rets.loc[date, current_asset])
        port_dates.append(date)

        if date in rebal_dates:
            s_mom = mom_spy.get(date, 0.0)
            b_mom = mom_bnd.get(date, 0.0)
            if not np.isnan(s_mom) and not np.isnan(b_mom):
                current_asset = spy if s_mom > b_mom else bnd

    port_ret_series = pd.Series(port_rets, index=port_dates)
    eq = (1 + port_ret_series).cumprod()
    return compute_all_metrics(eq, port_ret_series)


def run_all_benchmarks(prices: pd.DataFrame) -> dict[str, MetricResult | None]:
    """Run all benchmark strategies."""
    return {
        "SPY Buy&Hold": benchmark_buy_hold(prices, "SPY"),
        "QQQ Buy&Hold": benchmark_buy_hold(prices, "QQQ"),
        "EW Universe": benchmark_equal_weight_universe(prices),
        "Naive Top10 Mom": benchmark_top10_naive(prices),
        "60/40 SPY/BND": benchmark_60_40(prices),
        "Dual Momentum": benchmark_dual_momentum(prices),
    }


def format_benchmarks(
    benchmarks: dict[str, MetricResult | None],
    strategy_metrics: MetricResult,
) -> str:
    """Format benchmark comparison table."""
    lines = [
        "",
        "╔══════════════════════════════════════════════════════════════════════╗",
        "║              BENCHMARK COMPARISON                                   ║",
        "╠══════════════════════════════════════════════════════════════════════╣",
        f"║  {'Name':<22} {'CAGR':>7} {'Sharpe':>7} {'Sortino':>8} {'MaxDD':>7} {'TotRet':>8} ║",
        "╠══════════════════════════════════════════════════════════════════════╣",
    ]

    # Strategy first
    s = strategy_metrics
    lines.append(
        f"║  {'>>> STRATEGY <<<':<22} {s.cagr:>6.1%} {s.sharpe:>7.2f} {s.sortino:>8.2f} {s.max_drawdown:>6.1%} {s.total_return:>7.1%} ║"
    )
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")

    beaten = 0
    total = 0
    for name, m in benchmarks.items():
        if m is None:
            lines.append(f"║  {name:<22} {'N/A':>7} {'N/A':>7} {'N/A':>8} {'N/A':>7} {'N/A':>8} ║")
            continue
        total += 1
        beat = "✓" if s.sharpe > m.sharpe else "✗"
        if s.sharpe > m.sharpe:
            beaten += 1
        lines.append(
            f"║  {name:<22} {m.cagr:>6.1%} {m.sharpe:>7.2f} {m.sortino:>8.2f} {m.max_drawdown:>6.1%} {m.total_return:>7.1%} {beat} ║"
        )

    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append(f"║  Strategy beats {beaten}/{total} benchmarks{'':>40} ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════╝")

    return "\n".join(lines)
