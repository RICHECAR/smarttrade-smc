"""
Stress Tests — Sub-period, universe, cost & parameter robustness
════════════════════════════════════════════════════════════════
Tests whether edge survives variations in every dimension.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import random
from dataclasses import dataclass, replace
from typing import Optional

from portfolio.backtest import run_backtest, BacktestConfig, BacktestResult
from portfolio.allocator import AllocatorConfig, WeightMethod
from portfolio.signals import SignalConfig
from portfolio.risk import RiskConfig
from portfolio.features import FeatureConfig
from portfolio.metrics import MetricResult


@dataclass
class StressResult:
    name: str
    config_label: str
    cagr: float
    sharpe: float
    max_dd: float
    total_return: float
    bench_cagr: float
    bench_sharpe: float
    beats_bench: bool


def _run_one(prices: pd.DataFrame, config: BacktestConfig, name: str, label: str) -> StressResult:
    """Run a single backtest and capture key metrics."""
    try:
        result = run_backtest(prices, config)
        s = result.strategy_metrics
        b = result.benchmark_metrics
        beats = s.sharpe > b.sharpe and s.cagr > b.cagr * 0.9 and s.sharpe > 0.3
        return StressResult(
            name=name, config_label=label,
            cagr=s.cagr, sharpe=s.sharpe, max_dd=s.max_drawdown,
            total_return=s.total_return,
            bench_cagr=b.cagr, bench_sharpe=b.sharpe,
            beats_bench=beats,
        )
    except Exception as e:
        return StressResult(
            name=name, config_label=label,
            cagr=0.0, sharpe=0.0, max_dd=1.0, total_return=-1.0,
            bench_cagr=0.0, bench_sharpe=0.0, beats_bench=False,
        )


# ── Named configs to stress-test ────────────────────────────────

def _config_score_top5() -> BacktestConfig:
    return BacktestConfig(
        allocator_config=AllocatorConfig(top_n=5, max_weight=0.30, method=WeightMethod.SCORE_WEIGHTED),
        rebal_freq="M", cost_bps=5.0, warmup=252,
    )

def _config_no_risk_top10() -> BacktestConfig:
    return BacktestConfig(
        allocator_config=AllocatorConfig(top_n=10, max_weight=0.20, method=WeightMethod.SCORE_WEIGHTED),
        risk_config=RiskConfig(max_drawdown=1.0, vol_target=1.0),
        rebal_freq="M", cost_bps=5.0, warmup=252,
    )

def _config_score_top10() -> BacktestConfig:
    return BacktestConfig(
        allocator_config=AllocatorConfig(top_n=10, max_weight=0.20, method=WeightMethod.SCORE_WEIGHTED),
        rebal_freq="M", cost_bps=5.0, warmup=252,
    )

NAMED_CONFIGS = {
    "score_top5_monthly": _config_score_top5,
    "no_risk_overlay_top10": _config_no_risk_top10,
    "score_top10_monthly": _config_score_top10,
}


# ══════════════════════════════════════════════════════════════════
# 1. SUB-PERIOD TESTS
# ══════════════════════════════════════════════════════════════════

def stress_sub_periods(
    prices: pd.DataFrame,
    config_fn,
    config_label: str,
) -> list[StressResult]:
    """Test on rolling 1-year sub-periods."""
    results = []
    years = sorted(prices.index.year.unique())

    for i in range(len(years) - 1):
        y_start, y_end = years[i], years[i + 1]
        mask = (prices.index.year >= y_start) & (prices.index.year <= y_end)
        sub = prices.loc[mask]
        if len(sub) < 300:
            continue

        cfg = config_fn()
        cfg.warmup = min(126, len(sub) // 3)
        name = f"period_{y_start}-{y_end}"
        results.append(_run_one(sub, cfg, name, config_label))

    return results


# ══════════════════════════════════════════════════════════════════
# 2. UNIVERSE VARIATION
# ══════════════════════════════════════════════════════════════════

def stress_universe_dropout(
    prices: pd.DataFrame,
    config_fn,
    config_label: str,
    n_trials: int = 5,
    dropout_pct: float = 0.15,
    seed: int = 42,
) -> list[StressResult]:
    """Drop 15% of tickers randomly and re-test."""
    results = []
    rng = random.Random(seed)
    tickers = [c for c in prices.columns if c != "SPY"]

    for trial in range(n_trials):
        n_drop = max(1, int(len(tickers) * dropout_pct))
        dropped = rng.sample(tickers, n_drop)
        keep = [c for c in prices.columns if c not in dropped]
        sub = prices[keep]

        cfg = config_fn()
        name = f"dropout_{trial}_drop{n_drop}"
        results.append(_run_one(sub, cfg, name, config_label))

    return results


def stress_universe_reduced(
    prices: pd.DataFrame,
    config_fn,
    config_label: str,
) -> list[StressResult]:
    """Test with ETFs-only, stocks-only, and full universe."""
    from portfolio.universe import ASSET_TYPE
    results = []

    # ETFs only
    etf_cols = [c for c in prices.columns if ASSET_TYPE.get(c) == "ETF"]
    if len(etf_cols) > 5:
        cfg = config_fn()
        cfg.warmup = min(cfg.warmup, 200)
        results.append(_run_one(prices[etf_cols], cfg, "etfs_only", config_label))

    # Stocks only
    stock_cols = [c for c in prices.columns if ASSET_TYPE.get(c) == "STOCK"]
    if "SPY" not in stock_cols:
        stock_cols = ["SPY"] + stock_cols
    if len(stock_cols) > 5:
        cfg = config_fn()
        cfg.warmup = min(cfg.warmup, 200)
        results.append(_run_one(prices[stock_cols], cfg, "stocks_only", config_label))

    return results


# ══════════════════════════════════════════════════════════════════
# 3. COST VARIATION
# ══════════════════════════════════════════════════════════════════

def stress_costs(
    prices: pd.DataFrame,
    config_fn,
    config_label: str,
) -> list[StressResult]:
    """Test at different cost levels: 0, 5, 10, 20, 50 bps."""
    results = []
    for cost in [0, 5, 10, 20, 50]:
        cfg = config_fn()
        cfg.cost_bps = float(cost)
        name = f"cost_{cost}bps"
        results.append(_run_one(prices, cfg, name, config_label))
    return results


# ══════════════════════════════════════════════════════════════════
# 4. TOP-N VARIATION
# ══════════════════════════════════════════════════════════════════

def stress_top_n(
    prices: pd.DataFrame,
    config_fn,
    config_label: str,
) -> list[StressResult]:
    """Vary N from 3 to 15."""
    results = []
    for n in [3, 5, 7, 10, 15]:
        cfg = config_fn()
        cfg.allocator_config = replace(cfg.allocator_config, top_n=n)
        max_w = min(0.40, 1.0 / n + 0.05)
        cfg.allocator_config = replace(cfg.allocator_config, max_weight=max_w)
        name = f"top_{n}"
        results.append(_run_one(prices, cfg, name, config_label))
    return results


# ══════════════════════════════════════════════════════════════════
# 5. REBALANCE FREQUENCY VARIATION
# ══════════════════════════════════════════════════════════════════

def stress_rebal_freq(
    prices: pd.DataFrame,
    config_fn,
    config_label: str,
) -> list[StressResult]:
    """Test weekly vs monthly."""
    results = []
    for freq in ["W", "M"]:
        cfg = config_fn()
        cfg.rebal_freq = freq
        name = f"rebal_{freq}"
        results.append(_run_one(prices, cfg, name, config_label))
    return results


# ══════════════════════════════════════════════════════════════════
# 6. WEIGHT VARIATION
# ══════════════════════════════════════════════════════════════════

def stress_weight_variations(
    prices: pd.DataFrame,
    config_fn,
    config_label: str,
) -> list[StressResult]:
    """Test different signal weight profiles."""
    profiles = {
        "pure_momentum": {
            "mom_21": 0.15, "mom_63": 0.40, "mom_126": 0.30,
            "volatility": -0.05, "drawdown": -0.05,
            "trend_r2": 0.03, "rel_strength": 0.02,
        },
        "pure_low_vol": {
            "mom_21": 0.05, "mom_63": 0.10, "mom_126": 0.05,
            "volatility": -0.40, "drawdown": -0.25,
            "trend_r2": 0.10, "rel_strength": 0.05,
        },
        "balanced": {
            "mom_21": 0.10, "mom_63": 0.20, "mom_126": 0.15,
            "volatility": -0.15, "drawdown": -0.15,
            "trend_r2": 0.15, "rel_strength": 0.10,
        },
        "trend_focused": {
            "mom_21": 0.05, "mom_63": 0.15, "mom_126": 0.15,
            "volatility": -0.10, "drawdown": -0.05,
            "trend_r2": 0.35, "rel_strength": 0.15,
        },
        "no_short_momentum": {
            "mom_21": 0.00, "mom_63": 0.30, "mom_126": 0.30,
            "volatility": -0.15, "drawdown": -0.10,
            "trend_r2": 0.10, "rel_strength": 0.05,
        },
    }
    results = []
    for name, weights in profiles.items():
        cfg = config_fn()
        cfg.signal_config = SignalConfig(weights=weights)
        results.append(_run_one(prices, cfg, f"weights_{name}", config_label))
    return results


# ══════════════════════════════════════════════════════════════════
# FULL STRESS TEST RUNNER
# ══════════════════════════════════════════════════════════════════

def run_all_stress_tests(prices: pd.DataFrame) -> list[StressResult]:
    """Run complete stress test suite across the 3 target configs."""
    all_results = []

    for label, fn in NAMED_CONFIGS.items():
        print(f"\n{'='*60}")
        print(f"  STRESS TESTING: {label}")
        print(f"{'='*60}")

        print("  Sub-periods...")
        all_results.extend(stress_sub_periods(prices, fn, label))

        print("  Universe dropout...")
        all_results.extend(stress_universe_dropout(prices, fn, label))

        print("  Universe reduced...")
        all_results.extend(stress_universe_reduced(prices, fn, label))

        print("  Cost variation...")
        all_results.extend(stress_costs(prices, fn, label))

        print("  Top-N variation...")
        all_results.extend(stress_top_n(prices, fn, label))

        print("  Rebal frequency...")
        all_results.extend(stress_rebal_freq(prices, fn, label))

        print("  Weight profiles...")
        all_results.extend(stress_weight_variations(prices, fn, label))

    return all_results


def format_stress_results(results: list[StressResult]) -> str:
    """Format stress test results as a readable table."""
    lines = [
        "",
        "╔══════════════════════════════════════════════════════════════════════════════╗",
        "║                        STRESS TEST RESULTS                                  ║",
        "╠══════════════════════════════════════════════════════════════════════════════╣",
        f"║ {'Config':<25} {'Test':<25} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>7} {'Beat':>5} ║",
        "╠══════════════════════════════════════════════════════════════════════════════╣",
    ]

    for r in results:
        beat = "✓" if r.beats_bench else "✗"
        lines.append(
            f"║ {r.config_label:<25} {r.name:<25} {r.cagr:>6.1%} {r.sharpe:>7.2f} {r.max_dd:>6.1%} {beat:>5} ║"
        )

    # Summary
    total = len(results)
    wins = sum(1 for r in results if r.beats_bench)
    lines.append("╠══════════════════════════════════════════════════════════════════════════════╣")
    lines.append(f"║  Win rate: {wins}/{total} = {wins/total:.0%}{'':>57} ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════════════╝")

    return "\n".join(lines)
