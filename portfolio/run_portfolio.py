"""
Portfolio Pipeline Runner
═════════════════════════
Orchestrates: data download → features → signals → backtest → verdict.
Runs multiple configurations for robustness check.
"""

from __future__ import annotations

import sys
import time

from portfolio.universe import DEFAULT_UNIVERSE, CORE_ETFS, FULL_UNIVERSE
from portfolio.data_loader import fetch_prices
from portfolio.backtest import run_backtest, print_comparison, BacktestConfig
from portfolio.allocator import AllocatorConfig, WeightMethod
from portfolio.features import FeatureConfig
from portfolio.signals import SignalConfig
from portfolio.risk import RiskConfig


# ── Configurations to test ───────────────────────────────────────

CONFIGS = {
    "momentum_score_top10_monthly": BacktestConfig(
        period="5y",
        allocator_config=AllocatorConfig(top_n=10, max_weight=0.20, method=WeightMethod.SCORE_WEIGHTED),
        rebal_freq="M",
        cost_bps=5.0,
    ),
    "momentum_equal_top10_monthly": BacktestConfig(
        period="5y",
        allocator_config=AllocatorConfig(top_n=10, max_weight=0.20, method=WeightMethod.EQUAL),
        rebal_freq="M",
        cost_bps=5.0,
    ),
    "momentum_invvol_top10_monthly": BacktestConfig(
        period="5y",
        allocator_config=AllocatorConfig(top_n=10, max_weight=0.20, method=WeightMethod.INVERSE_VOL),
        rebal_freq="M",
        cost_bps=5.0,
    ),
    "momentum_score_top15_weekly": BacktestConfig(
        period="5y",
        allocator_config=AllocatorConfig(top_n=15, max_weight=0.15, method=WeightMethod.SCORE_WEIGHTED),
        rebal_freq="W",
        cost_bps=5.0,
    ),
    "momentum_score_top5_monthly": BacktestConfig(
        period="5y",
        allocator_config=AllocatorConfig(top_n=5, max_weight=0.30, method=WeightMethod.SCORE_WEIGHTED),
        rebal_freq="M",
        cost_bps=5.0,
    ),
    "low_vol_invvol_top10_monthly": BacktestConfig(
        period="5y",
        signal_config=SignalConfig(weights={
            "mom_21":       0.05,
            "mom_63":       0.10,
            "mom_126":      0.10,
            "volatility":  -0.35,
            "drawdown":    -0.20,
            "trend_r2":     0.10,
            "rel_strength": 0.10,
        }),
        allocator_config=AllocatorConfig(top_n=10, max_weight=0.20, method=WeightMethod.INVERSE_VOL),
        rebal_freq="M",
        cost_bps=5.0,
    ),
    "no_risk_overlay_score_top10": BacktestConfig(
        period="5y",
        allocator_config=AllocatorConfig(top_n=10, max_weight=0.20, method=WeightMethod.SCORE_WEIGHTED),
        risk_config=RiskConfig(max_drawdown=1.0, vol_target=1.0),  # Effectively disabled
        rebal_freq="M",
        cost_bps=5.0,
    ),
}


def main():
    print("=" * 60)
    print("  PORTFOLIO ALLOCATION ENGINE — BACKTEST PIPELINE")
    print("=" * 60)
    print()

    # ── Step 1: Download data ─────────────────────────────────
    universe = DEFAULT_UNIVERSE
    print(f"Universe: {len(universe)} tickers")
    print(f"Tickers: {', '.join(universe[:10])}...")
    print()

    print("Downloading price data (5y)...")
    t0 = time.time()
    prices = fetch_prices(universe, period="5y", cache=True)
    print(f"  → {prices.shape[0]} days × {prices.shape[1]} tickers ({time.time()-t0:.1f}s)")
    print(f"  → Date range: {prices.index[0].date()} to {prices.index[-1].date()}")
    print()

    # ── Step 2: Run all configurations ────────────────────────
    results = {}
    robust_count = 0

    for name, config in CONFIGS.items():
        print(f"Running: {name}...")
        t0 = time.time()
        try:
            result = run_backtest(prices, config)
            results[name] = result
            report = print_comparison(result)
            print(report)

            s = result.strategy_metrics
            b = result.benchmark_metrics
            is_robust = (
                s.sharpe > b.sharpe
                and s.cagr > b.cagr * 0.9
                and s.max_drawdown < b.max_drawdown * 1.2
                and s.sharpe > 0.5
            )
            if is_robust:
                robust_count += 1

        except Exception as e:
            print(f"  ✗ FAILED: {e}")

        print(f"  ({time.time()-t0:.1f}s)")
        print()

    # ── Step 3: Final verdict ─────────────────────────────────
    total = len(CONFIGS)
    print()
    print("=" * 60)
    print("  FINAL VERDICT")
    print("=" * 60)
    print(f"  Configs tested  : {total}")
    print(f"  Robust configs  : {robust_count}")
    print(f"  Robust rate     : {robust_count/total:.0%}")
    print()

    if robust_count >= total * 0.5:
        print("  ██████████████████████████████████████")
        print("  ██         ROBUST ✓               ██")
        print("  ██████████████████████████████████████")
        print("  → Majority of configurations beat benchmark")
    else:
        print("  ██████████████████████████████████████")
        print("  ██       NOT ROBUST ✗              ██")
        print("  ██████████████████████████████████████")
        print("  → Insufficient edge across configurations")

    print()
    return robust_count, total


if __name__ == "__main__":
    robust, total = main()
    sys.exit(0 if robust >= total * 0.5 else 1)
