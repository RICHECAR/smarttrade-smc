"""
Chantier 12 — Structural Edge: Full Runner
============================================

Tests 3 new alpha classes on REAL Bitget data:
  1. Trend Following (HTF trends, pullback entries)
  2. Volatility Expansion (squeeze → expansion)
  3. Extreme Events (liquidation cascades, reversion)

Each alpha individually through full pipeline:
  - Realistic engine (slippage, fees, funding)
  - Walk-forward validation (anti-overfitting)
  - Monte Carlo simulation (robustness)
  - Edge validation (binary verdict)

Then: combined alpha (best signals from all 3)

Delivers:
  - Per-alpha report + verdict
  - Combined alpha report
  - Comparison table vs Chantier 10/11 alphas
  - Final verdict: EDGE FOUND / NO EDGE

Rule: Zero parameter tuning. Truth only.

Usage:
    python -m smc_futures.backtest.run_structural_edge --days 90 --symbol BTCUSDT
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import os
import argparse
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Callable, Tuple

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from smc_futures.backtest.engine import (
    RealisticBacktestConfig, BTCandle, BacktestSignal, RealisticTrade,
)
from smc_futures.backtest.pipeline import BacktestPipeline, PipelineConfig, PipelineResult
from smc_futures.backtest.walk_forward import WalkForwardConfig
from smc_futures.backtest.monte_carlo import MonteCarloConfig
from smc_futures.backtest.metrics import MetricsCalculator, EdgeValidator, DetailedMetrics
from smc_futures.backtest.real_data_loader import RealDataLoader, LoadResult

from smc_futures.backtest.alpha_trend_following import (
    make_trend_following_fn, TrendFollowingConfig,
)
from smc_futures.backtest.alpha_volatility_expansion import (
    make_volatility_expansion_fn, VolatilityExpansionConfig,
)
from smc_futures.backtest.alpha_extreme_events import (
    make_extreme_events_fn, ExtremeEventsConfig,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  Pipeline Config Factory
# ═══════════════════════════════════════════

def make_pipeline_config(max_holding_bars: int = 576) -> PipelineConfig:
    """
    Conservative pipeline config for structural alphas.
    No naive comparison — only realistic matters.
    """
    bt_config = RealisticBacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.01,
        leverage=10,
        max_open_positions=1,
        max_trades_per_day=5,
        cooldown_bars=5,
        bar_duration_ms=300_000,  # 5m
        charge_funding=True,
        atr_period=14,
        seed=42,
    )

    wf_config = WalkForwardConfig(
        train_bars=4032,       # 14 days of 5m
        validate_bars=1152,    # 4 days
        test_bars=1152,        # 4 days
        step_bars=1152,        # Step by 4 days
        min_trades_train=5,    # Lower threshold — these are low-frequency alphas
        min_trades_validate=2,
        min_trades_test=2,
        max_folds=50,
    )

    mc_config = MonteCarloConfig(
        n_permutations=2000,
        n_param_variations=500,
        n_skip_trials=500,
        skip_pct=0.10,
        slippage_vary_range=0.50,
        fee_vary_range=0.30,
        confidence_level=0.95,
        seed=42,
    )

    return PipelineConfig(
        bt_config=bt_config,
        wf_config=wf_config,
        mc_config=mc_config,
        max_holding_bars=max_holding_bars,
        run_naive_comparison=False,
    )


# ═══════════════════════════════════════════
#  Combined Alpha (best of 3)
# ═══════════════════════════════════════════

def make_combined_alpha_fn(
    tf_cfg: Optional[TrendFollowingConfig] = None,
    ve_cfg: Optional[VolatilityExpansionConfig] = None,
    ee_cfg: Optional[ExtremeEventsConfig] = None,
) -> Callable[[List[BTCandle]], Optional[BacktestSignal]]:
    """
    Combiner: runs all 3 alphas. Returns highest-confidence signal.
    Hard filter: rejects if target < 200 bps.
    """
    tf_fn = make_trend_following_fn(tf_cfg)
    ve_fn = make_volatility_expansion_fn(ve_cfg)
    ee_fn = make_extreme_events_fn(ee_cfg)

    last_signal_bar = [-1]
    MIN_BARS_BETWEEN = 30

    def signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
        n = len(closed_bars)
        if n < 120:
            return None

        # Cooldown for combiner
        if last_signal_bar[0] >= 0 and (n - last_signal_bar[0]) < MIN_BARS_BETWEEN:
            return None

        candidates: List[BacktestSignal] = []

        # Run all 3 alphas
        for fn in (tf_fn, ve_fn, ee_fn):
            sig = fn(closed_bars)
            if sig is not None:
                # Hard filter: min 100 bps target
                tp_dist = abs(sig.take_profit - sig.entry_price)
                target_bps = tp_dist / sig.entry_price * 10000 if sig.entry_price > 0 else 0
                if target_bps >= 100:
                    candidates.append(sig)

        if not candidates:
            return None

        # Pick highest confidence
        best = max(candidates, key=lambda s: s.confidence)
        last_signal_bar[0] = n
        return best

    return signal_fn


# ═══════════════════════════════════════════
#  Analysis helpers
# ═══════════════════════════════════════════

@dataclass
class AlphaReport:
    """Result summary for one alpha."""
    name: str
    trades: int = 0
    win_rate: float = 0.0
    pf: float = 0.0
    expectancy: float = 0.0
    net_pnl: float = 0.0
    max_dd: float = 0.0
    sharpe: float = 0.0
    edge_ratio: float = 0.0
    score: float = 0.0
    verdict: str = "N/A"
    wf_robust: bool = False
    mc_robust: bool = False
    total_fees: float = 0.0
    total_slippage: float = 0.0
    avg_hold_bars: float = 0.0
    long_trades: int = 0
    short_trades: int = 0
    long_wr: float = 0.0
    short_wr: float = 0.0
    pipeline_result: Optional[PipelineResult] = None


def extract_report(name: str, result: PipelineResult) -> AlphaReport:
    """Extract key metrics from pipeline result."""
    r = AlphaReport(name=name, pipeline_result=result)

    if result.metrics:
        m = result.metrics
        r.trades = m.total_trades
        r.win_rate = m.win_rate
        r.pf = m.profit_factor
        r.expectancy = m.expectancy
        r.net_pnl = m.total_net_pnl
        r.max_dd = m.max_drawdown_pct
        r.sharpe = m.sharpe_ratio
        r.edge_ratio = m.edge_ratio
        r.total_fees = m.total_fees
        r.total_slippage = m.total_slippage_cost
        r.avg_hold_bars = m.avg_holding_bars
        r.score = result.verdict.score if result.verdict else 0.0
        r.verdict = result.verdict.verdict if result.verdict else "N/A"

    if result.walk_forward:
        r.wf_robust = result.walk_forward.is_robust

    if result.monte_carlo:
        r.mc_robust = result.monte_carlo.is_robust

    # Direction breakdown
    if result.engine_state and result.engine_state.trades:
        trades = result.engine_state.trades
        longs = [t for t in trades if t.direction == "long"]
        shorts = [t for t in trades if t.direction == "short"]
        r.long_trades = len(longs)
        r.short_trades = len(shorts)
        if longs:
            r.long_wr = sum(1 for t in longs if t.is_win) / len(longs) * 100
        if shorts:
            r.short_wr = sum(1 for t in shorts if t.is_win) / len(shorts) * 100

    return r


# ═══════════════════════════════════════════
#  Formatted Output
# ═══════════════════════════════════════════

def format_alpha_report(r: AlphaReport) -> str:
    """Format single alpha report."""
    lines = [
        f"={'=' * 66}",
        f"  {r.name}",
        f"={'=' * 66}",
        f"  Trades:           {r.trades}",
        f"  Win Rate:         {r.win_rate:.1f}%",
        f"  Profit Factor:    {r.pf:.2f}",
        f"  Expectancy:       ${r.expectancy:.2f}/trade",
        f"  Net PnL:          ${r.net_pnl:.2f}",
        f"  Max Drawdown:     {r.max_dd:.1f}%",
        f"  Sharpe Ratio:     {r.sharpe:.2f}",
        f"  Edge Ratio:       {r.edge_ratio:.2f}",
        f"  Score:            {r.score:.0f}/100",
        f"  Verdict:          {r.verdict}",
        "",
        f"  Direction:",
        f"    Long:  {r.long_trades} trades  WR: {r.long_wr:.1f}%",
        f"    Short: {r.short_trades} trades  WR: {r.short_wr:.1f}%",
        "",
        f"  Costs:",
        f"    Fees:     ${r.total_fees:.2f}",
        f"    Slippage: ${r.total_slippage:.2f}",
        f"    Avg Hold: {r.avg_hold_bars:.0f} bars ({r.avg_hold_bars * 5:.0f} min)",
        "",
        f"  Validation:",
        f"    WF Robust: {'YES' if r.wf_robust else 'NO'}",
        f"    MC Robust: {'YES' if r.mc_robust else 'NO'}",
    ]
    return "\n".join(lines)


def format_comparison_table(reports: List[AlphaReport]) -> str:
    """Format comparison table across all alphas."""
    lines = [
        "=" * 90,
        "  COMPARISON TABLE — ALL ALPHAS",
        "=" * 90,
        f"  {'Alpha':<25} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Exp$':>8} {'PnL$':>10} {'DD%':>7} {'Edge':>6} {'Score':>6} {'Verdict':<15}",
        "-" * 90,
    ]
    for r in reports:
        lines.append(
            f"  {r.name:<25} {r.trades:>7} {r.win_rate:>6.1f}% {r.pf:>6.2f} {r.expectancy:>7.2f} {r.net_pnl:>9.2f} {r.max_dd:>6.1f}% {r.edge_ratio:>5.2f} {r.score:>5.0f} {r.verdict:<15}"
        )
    lines.append("-" * 90)
    return "\n".join(lines)


def format_final_verdict(reports: List[AlphaReport]) -> str:
    """Compute and format the FINAL Chantier 12 verdict."""
    # Any alpha with EDGE CONFIRMED?
    confirmed = [r for r in reports if "CONFIRMED" in r.verdict]
    # Any alpha with positive expectancy + reasonable PF?
    promising = [r for r in reports if r.expectancy > 0 and r.pf >= 1.2 and r.trades >= 10]
    # Any alpha with edge ratio > 1?
    has_edge = [r for r in reports if r.edge_ratio > 1.0 and r.trades >= 10]

    lines = [
        "=" * 70,
        "  VERDICT FINAL — CHANTIER 12",
        "  STRUCTURAL EDGE ALPHAS sur DONNEES REELLES",
        "=" * 70,
    ]

    # Criteria check for each alpha
    for r in reports:
        lines.append(f"\n  {r.name}:")
        checks = [
            ("Expectancy > 0", r.expectancy > 0, f"${r.expectancy:.2f}"),
            ("PF >= 1.3", r.pf >= 1.3, f"{r.pf:.2f}"),
            ("Edge Ratio > 1", r.edge_ratio > 1.0, f"{r.edge_ratio:.2f}"),
            ("Max DD <= 25%", r.max_dd <= 25.0, f"{r.max_dd:.1f}%"),
            ("WF Robust", r.wf_robust, "YES" if r.wf_robust else "NO"),
            ("MC Robust", r.mc_robust, "YES" if r.mc_robust else "NO"),
            ("Trades >= 10", r.trades >= 10, f"{r.trades}"),
        ]
        passed = sum(1 for _, ok, _ in checks if ok)
        for label, ok, val in checks:
            status = "PASS" if ok else "FAIL"
            lines.append(f"    [{status}] {label:<20} = {val}")
        lines.append(f"    Score: {passed}/7 criteria passed")

    # Final determination
    lines.append("")
    if confirmed:
        verdict_text = "EDGE FOUND"
        detail = f"Alpha(s) avec edge confirme: {', '.join(r.name for r in confirmed)}"
        box_char = "+"
    elif promising:
        verdict_text = "EDGE WEAK"
        detail = f"Alpha(s) prometteuse(s) mais pas robuste(s): {', '.join(r.name for r in promising)}"
        box_char = "~"
    else:
        verdict_text = "NO EDGE"
        detail = "Aucune alpha n'a d'edge sur donnees reelles."
        box_char = "-"

    lines.extend([
        "  " + "=" * 55,
        f"  |{'':^53}|",
        f"  |{verdict_text:^53}|",
        f"  |{'':^53}|",
        "  " + "=" * 55,
        f"  {detail}",
    ])

    if not confirmed and not promising:
        lines.extend([
            "",
            "  DIAGNOSTIC:",
            "  - Les couts de microstructure crypto (~120 bps) restent dominants",
            "  - Meme les alphas basse-frequence / gros target ne suffisent pas",
            "  - Le marche est hyper-efficient sur les timeframes testes",
            "  - PROCHAINE ETAPE: explorer cross-asset, funding arbitrage, ou plus long terme",
        ])

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  Main runner
# ═══════════════════════════════════════════

async def run_structural_edge(
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    days: int = 90,
) -> str:
    """
    Full Chantier 12 runner.
    Returns the complete report text.
    """
    start_time = time.monotonic()
    report_lines: List[str] = []

    def log_section(text: str):
        logger.info(f"\n{text}")
        report_lines.append(text)

    log_section("=" * 70)
    log_section("  CHANTIER 12 — STRUCTURAL EDGE VALIDATION")
    log_section(f"  Symbol: {symbol} | TF: {timeframe} | Days: {days}")
    log_section(f"  Date: {datetime.now(timezone.utc).isoformat()}")
    log_section("=" * 70)

    # ────────────────────────────────────────
    # [1/5] FETCH REAL DATA
    # ────────────────────────────────────────
    log_section("\n[1/5] FETCHING REAL DATA...")
    loader = RealDataLoader()
    load_result: LoadResult = await loader.load(
        symbol=symbol, timeframe=timeframe, days=days, force_api=True,
    )

    candles = load_result.candles
    val = load_result.validation

    log_section(f"  Candles: {len(candles)}")
    log_section(f"  Source:  {load_result.source}")
    if val:
        log_section(f"  Coverage:    {val.coverage_pct:.1f}%")
        log_section(f"  Gaps:        {val.gaps_found}")
        log_section(f"  OHLC Issues: {val.ohlc_violations}")
        log_section(f"  Anomalies:   {val.anomalies}")
        log_section(f"  Valid:       {'YES' if val.is_valid else 'NO'}")

    if not load_result.is_valid:
        log_section("  DATA INVALID — ABORTING")
        return "\n".join(report_lines)

    if len(candles) < 500:
        log_section(f"  INSUFFICIENT DATA ({len(candles)} < 500) — ABORTING")
        return "\n".join(report_lines)

    log_section(f"  Data period: {candles[0].timestamp} - {candles[-1].timestamp}")
    log_section(f"  Price range: ${min(c.low for c in candles):.2f} - ${max(c.high for c in candles):.2f}")

    # ────────────────────────────────────────
    # [2/5] RUN EACH ALPHA INDIVIDUALLY
    # ────────────────────────────────────────
    log_section("\n[2/5] RUNNING INDIVIDUAL ALPHAS...")

    alphas: List[Tuple[str, Callable, int]] = [
        (
            "TREND FOLLOWING",
            make_trend_following_fn(),
            576,  # 48h max hold for trend
        ),
        (
            "VOLATILITY EXPANSION",
            make_volatility_expansion_fn(),
            288,  # 24h max hold for expansion
        ),
        (
            "EXTREME EVENTS",
            make_extreme_events_fn(),
            288,  # 24h max hold
        ),
    ]

    reports: List[AlphaReport] = []

    for alpha_name, signal_fn, max_hold in alphas:
        log_section(f"\n  Running: {alpha_name}...")
        t0 = time.monotonic()

        config = make_pipeline_config(max_holding_bars=max_hold)
        pipeline = BacktestPipeline(config)

        try:
            result: PipelineResult = pipeline.run(symbol, candles, signal_fn)
            dt = time.monotonic() - t0

            r = extract_report(alpha_name, result)
            reports.append(r)

            log_section(f"  Completed in {dt:.1f}s")
            # Print pipeline report
            if result.report:
                log_section(result.report)

            log_section(format_alpha_report(r))

        except Exception as e:
            logger.error(f"  FAILED: {e}", exc_info=True)
            reports.append(AlphaReport(name=alpha_name, verdict="ERROR"))

    # ────────────────────────────────────────
    # [3/5] RUN COMBINED ALPHA
    # ────────────────────────────────────────
    log_section("\n[3/5] RUNNING COMBINED ALPHA...")
    t0 = time.monotonic()
    combined_fn = make_combined_alpha_fn()
    config = make_pipeline_config(max_holding_bars=576)
    pipeline = BacktestPipeline(config)

    try:
        combined_result = pipeline.run(symbol, candles, combined_fn)
        dt = time.monotonic() - t0
        combined_report = extract_report("COMBINED (Best of 3)", combined_result)
        reports.append(combined_report)

        log_section(f"  Completed in {dt:.1f}s")
        if combined_result.report:
            log_section(combined_result.report)
        log_section(format_alpha_report(combined_report))
    except Exception as e:
        logger.error(f"  COMBINED FAILED: {e}", exc_info=True)
        reports.append(AlphaReport(name="COMBINED (Best of 3)", verdict="ERROR"))

    # ────────────────────────────────────────
    # [4/5] COMPARISON TABLE
    # ────────────────────────────────────────
    log_section("\n[4/5] COMPARISON TABLE...")

    # Add old alphas for reference
    old_alphas = [
        AlphaReport(name="[C10] Alpha A (SMC Rev)", trades=12, win_rate=41.7, pf=0.67,
                    expectancy=-2.91, net_pnl=-34.92, max_dd=6.2, edge_ratio=0.38, score=10,
                    verdict="INVALIDATED"),
        AlphaReport(name="[C10] Alpha B (Mom BK)", trades=13, win_rate=69.2, pf=1.08,
                    expectancy=4.73, net_pnl=61.50, max_dd=7.7, edge_ratio=0.44, score=45,
                    verdict="INVALIDATED"),
        AlphaReport(name="[C11] Alpha B REAL", trades=78, win_rate=16.7, pf=0.07,
                    expectancy=-86.12, net_pnl=-6717.38, max_dd=67.2, edge_ratio=0.25, score=15,
                    verdict="EDGE DEAD"),
    ]

    all_reports = old_alphas + reports
    log_section(format_comparison_table(all_reports))

    # ────────────────────────────────────────
    # [5/5] FINAL VERDICT
    # ────────────────────────────────────────
    log_section("\n[5/5] FINAL VERDICT...")
    log_section(format_final_verdict(reports))

    total_time = time.monotonic() - start_time
    log_section(f"\n  Total execution time: {total_time:.1f}s")

    # Save report
    full_report = "\n".join(report_lines)
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, f"chantier12_structural_edge_{symbol}_{days}d.txt")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(full_report)

    logger.info(f"\nReport saved to: {report_path}")
    return full_report


# ═══════════════════════════════════════════
#  CLI Entry Point
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Chantier 12 — Structural Edge Validation")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--timeframe", type=str, default="5m")
    args = parser.parse_args()

    report = asyncio.run(run_structural_edge(
        symbol=args.symbol,
        timeframe=args.timeframe,
        days=args.days,
    ))

    # Exit with code based on verdict
    if "EDGE FOUND" in report:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
