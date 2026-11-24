"""
Chantier 16 Runner — Robustesse Avancée + Pré-Production
═════════════════════════════════════════════════════════
Orchestrates: stress tests, sensitivity, walk-forward, benchmarks,
turnover analysis, safeguards validation, reporting.
"""

from __future__ import annotations

import sys
import time

from portfolio.universe import DEFAULT_UNIVERSE
from portfolio.data_loader import fetch_prices
from portfolio.backtest import run_backtest, BacktestConfig
from portfolio.allocator import AllocatorConfig, WeightMethod
from portfolio.risk import RiskConfig

from portfolio.stress_test import run_all_stress_tests, format_stress_results
from portfolio.sensitivity import run_sensitivity_analysis
from portfolio.walk_forward import run_walk_forward, format_walk_forward
from portfolio.benchmarks import run_all_benchmarks, format_benchmarks
from portfolio.turnover_analysis import format_turnover_analysis
from portfolio.safeguards import run_all_safeguards, SafeguardConfig
from portfolio.reporting import generate_institutional_report
from portfolio.live_readiness import PortfolioState, compute_rebalance_plan, generate_order_report


def main():
    print("=" * 70)
    print("  CHANTIER 16 — ROBUSTESSE AVANCÉE + PRÉ-PRODUCTION")
    print("=" * 70)
    print()

    # ── Data ──────────────────────────────────────────────────────
    universe = DEFAULT_UNIVERSE
    print(f"Universe: {len(universe)} tickers")
    print("Downloading price data (5y)...")
    t0 = time.time()
    prices = fetch_prices(universe, period="5y", cache=True)
    print(f"  → {prices.shape[0]} days × {prices.shape[1]} tickers ({time.time()-t0:.1f}s)")
    print(f"  → {prices.index[0].date()} to {prices.index[-1].date()}")
    print()

    # ══════════════════════════════════════════════════════════════
    # 1. STRESS TESTS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 1: STRESS TESTS")
    print("=" * 70)
    t0 = time.time()
    stress_results = run_all_stress_tests(prices)
    report_stress = format_stress_results(stress_results)
    print(report_stress)
    print(f"  ({time.time()-t0:.0f}s)")

    # Count wins
    total_stress = len(stress_results)
    wins_stress = sum(1 for r in stress_results if r.beats_bench)

    # ══════════════════════════════════════════════════════════════
    # 2. SENSITIVITY ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 2: SENSITIVITY ANALYSIS")
    print("=" * 70)
    t0 = time.time()
    report_sensitivity = run_sensitivity_analysis(prices)
    print(report_sensitivity)
    print(f"  ({time.time()-t0:.0f}s)")

    # ══════════════════════════════════════════════════════════════
    # 3. WALK-FORWARD
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 3: WALK-FORWARD OOS")
    print("=" * 70)
    t0 = time.time()
    wf = run_walk_forward(prices, train_days=504, test_days=252, step_days=126, warmup=126)
    report_wf = format_walk_forward(wf)
    print(report_wf)
    print(f"  ({time.time()-t0:.0f}s)")

    # ══════════════════════════════════════════════════════════════
    # 4. RUN BEST CONFIG FOR DETAILED ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  RUNNING BEST CONFIG (score_top5_monthly) FOR DETAILED ANALYSIS")
    print("=" * 70)
    best_config = BacktestConfig(
        allocator_config=AllocatorConfig(top_n=5, max_weight=0.30, method=WeightMethod.SCORE_WEIGHTED),
        rebal_freq="M", cost_bps=5.0,
    )
    best_result = run_backtest(prices, best_config)

    # ══════════════════════════════════════════════════════════════
    # 5. BENCHMARKS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 4: BENCHMARK COMPARISON")
    print("=" * 70)
    t0 = time.time()
    benchmarks = run_all_benchmarks(prices)
    report_bench = format_benchmarks(benchmarks, best_result.strategy_metrics)
    print(report_bench)
    print(f"  ({time.time()-t0:.0f}s)")

    # Count benchmark wins
    bench_wins = sum(
        1 for m in benchmarks.values()
        if m is not None and best_result.strategy_metrics.sharpe > m.sharpe
    )
    bench_total = sum(1 for m in benchmarks.values() if m is not None)

    # ══════════════════════════════════════════════════════════════
    # 6. TURNOVER & COST ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 5: TURNOVER & COST ANALYSIS")
    print("=" * 70)
    report_turnover = format_turnover_analysis(best_result)
    print(report_turnover)

    # ══════════════════════════════════════════════════════════════
    # 7. SAFEGUARDS VALIDATION
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 6: SAFEGUARDS VALIDATION")
    print("=" * 70)

    from portfolio.features import compute_all_features
    from portfolio.signals import compute_composite_score

    features = compute_all_features(prices)
    composite = compute_composite_score(features)
    last_scores = composite.iloc[-1]

    # Get last weights from backtest
    last_weights = {}
    if not best_result.positions_history.empty:
        last_pos = best_result.positions_history.iloc[-1]
        last_weights = {t: w for t, w in last_pos.items() if w > 0.01}

    sg = run_all_safeguards(
        prices=prices,
        scores=last_scores,
        weights=last_weights,
        reference_date=prices.index[-1],
    )

    print(f"  Safeguards passed: {sg.passed}")
    for name, passed in sg.checks.items():
        status = "✓" if passed else "✗"
        print(f"    {status} {name}")
    for msg in sg.blockers:
        print(f"    ⚠ {msg}")

    # ══════════════════════════════════════════════════════════════
    # 8. LIVE READINESS DEMO
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 7: LIVE READINESS DEMO")
    print("=" * 70)

    # Simulate: current state is empty, target is backtest's last weights
    current = PortfolioState.from_weights({}, total_value=100_000)
    target_weights = last_weights
    plan = compute_rebalance_plan(current, target_weights, total_value=100_000)
    plan.safeguard_passed = sg.passed
    plan.safeguard_messages = sg.blockers
    report_orders = generate_order_report(plan)
    print(report_orders)

    # ══════════════════════════════════════════════════════════════
    # 9. INSTITUTIONAL REPORT
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  SECTION 8: INSTITUTIONAL REPORT")
    print("=" * 70)
    report_inst = generate_institutional_report(best_result, prices)
    print(report_inst)

    # ══════════════════════════════════════════════════════════════
    # FINAL VERDICT
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  FINAL VERDICT — CHANTIER 16")
    print("=" * 70)

    stress_pass = wins_stress / max(total_stress, 1) >= 0.55
    oos_pass = wf["avg_oos_sharpe"] > 0.2
    bench_pass = bench_wins >= bench_total * 0.5
    safeguard_pass = sg.passed
    sharpe_pass = best_result.strategy_metrics.sharpe > 0.6

    checks = {
        "Stress tests (>55% win)": stress_pass,
        "Walk-forward OOS (Sharpe > 0.2)": oos_pass,
        "Beats majority benchmarks": bench_pass,
        "Safeguards pass": safeguard_pass,
        "Strategy Sharpe > 0.6": sharpe_pass,
    }

    print()
    for name, passed in checks.items():
        icon = "✓" if passed else "✗"
        print(f"  {icon} {name}")

    n_pass = sum(checks.values())
    n_total = len(checks)
    print(f"\n  Score: {n_pass}/{n_total}")

    if n_pass == n_total:
        verdict = "PRE-PROD READY"
        print("\n  ██████████████████████████████████████████")
        print("  ██       PRE-PROD READY ✓              ██")
        print("  ██████████████████████████████████████████")
    elif n_pass >= n_total - 1:
        verdict = "ROBUST BUT NEEDS HARDENING"
        print("\n  ██████████████████████████████████████████")
        print("  ██  ROBUST BUT NEEDS HARDENING ⚠       ██")
        print("  ██████████████████████████████████████████")
    else:
        verdict = "NOT ROBUST"
        print("\n  ██████████████████████████████████████████")
        print("  ██       NOT ROBUST ✗                  ██")
        print("  ██████████████████████████████████████████")

    # Residual risks
    print("\n  RISQUES RÉSIDUELS:")
    print("  - Backtesté sur période haussière US (2021-2026)")
    print("  - Survivorship bias possible sur universe statique")
    print("  - Pas de test sur marchés baissiers prolongés (>2y)")
    print("  - Données yfinance adj close: splits/dividends potentiellement imprécis")
    print("  - Pas de modélisation du slippage sur small caps")
    print()

    return verdict


if __name__ == "__main__":
    verdict = main()
    sys.exit(0 if verdict != "NOT ROBUST" else 1)
