"""
Chantier 13 — REPOSITIONNEMENT COMPLET
========================================

4 axes de recherche sur nouveau terrain:
  AXE 1: HTF Trend Following (4H, 1D) — moves ≥ 500 bps
  AXE 2: Long-Only Bias — bull regime filter only
  AXE 3: Funding/Carry — counter-crowd + regime aligned
  AXE 4: Market Selection — ETH, SOL (less efficient)

Validation: données réelles Bitget + pipeline réaliste complète

Usage:
    python -m smc_futures.backtest.run_chantier13 [--axes 1,2,3,4]
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import os
import argparse
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Callable, Tuple

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from smc_futures.backtest.engine import (
    RealisticBacktestConfig, BTCandle, BacktestSignal,
)
from smc_futures.backtest.pipeline import BacktestPipeline, PipelineConfig, PipelineResult
from smc_futures.backtest.walk_forward import WalkForwardConfig
from smc_futures.backtest.monte_carlo import MonteCarloConfig
from smc_futures.backtest.metrics import MetricsCalculator, EdgeValidator, DetailedMetrics
from smc_futures.backtest.real_data_loader import RealDataLoader, LoadResult

from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn, HTFTrendConfig
from smc_futures.backtest.alpha_long_only import make_long_only_fn, LongOnlyConfig
from smc_futures.backtest.alpha_funding_arb import make_funding_arb_fn, FundingConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  Pipeline Config Factory
# ═══════════════════════════════════════════

def make_pipeline_config_htf(
    timeframe: str = "4h",
    max_holding_bars: int = 60,
) -> PipelineConfig:
    """Pipeline config adapted for HTF (4H or 1D)."""
    # Bars per day: 4H = 6, 1D = 1
    bars_per_day = 6 if timeframe == "4h" else 1
    bar_duration_ms = 14_400_000 if timeframe == "4h" else 86_400_000

    bt_config = RealisticBacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.01,
        leverage=10,
        max_open_positions=1,
        max_trades_per_day=3,
        cooldown_bars=2,
        bar_duration_ms=bar_duration_ms,
        charge_funding=True,
        atr_period=14,
        seed=42,
    )

    # WF: ~14 days train, ~5 days validate, ~5 days test
    train_bars = max(14 * bars_per_day, 30)
    val_bars = max(5 * bars_per_day, 10)
    test_bars = max(5 * bars_per_day, 10)
    step_bars = max(5 * bars_per_day, 10)

    wf_config = WalkForwardConfig(
        train_bars=train_bars,
        validate_bars=val_bars,
        test_bars=test_bars,
        step_bars=step_bars,
        min_trades_train=3,
        min_trades_validate=1,
        min_trades_test=1,
        max_folds=50,
    )

    mc_config = MonteCarloConfig(
        n_permutations=1000,
        n_param_variations=300,
        n_skip_trials=300,
        skip_pct=0.15,
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
#  Result Tracking
# ═══════════════════════════════════════════

@dataclass
class AlphaResult:
    axe: int
    name: str
    symbol: str
    timeframe: str
    trades: int = 0
    win_rate: float = 0.0
    pf: float = 0.0
    expectancy: float = 0.0
    net_pnl: float = 0.0
    max_dd: float = 0.0
    sharpe: float = 0.0
    edge_ratio: float = 0.0
    score: int = 0
    verdict: str = "N/A"
    long_trades: int = 0
    long_wr: float = 0.0
    short_trades: int = 0
    short_wr: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    wf_robust: bool = False
    mc_robust: bool = False
    duration_s: float = 0.0


def extract_result(
    axe: int, name: str, symbol: str, timeframe: str,
    pipeline_result: PipelineResult, duration: float,
) -> AlphaResult:
    """Extract reporting fields from pipeline result."""
    r = AlphaResult(axe=axe, name=name, symbol=symbol, timeframe=timeframe)
    r.duration_s = duration

    m = pipeline_result.metrics
    if m is None:
        return r

    r.trades = m.total_trades
    r.win_rate = m.win_rate  # Already a percentage from MetricsCalculator
    r.pf = m.profit_factor
    r.expectancy = m.expectancy
    r.net_pnl = m.total_net_pnl
    r.max_dd = m.max_drawdown_pct
    r.sharpe = m.sharpe_ratio
    r.edge_ratio = m.edge_ratio

    if pipeline_result.verdict:
        r.score = pipeline_result.verdict.score
        r.verdict = pipeline_result.verdict.verdict

    # Direction breakdown
    if pipeline_result.engine_state:
        trades = pipeline_result.engine_state.trades
        longs = [t for t in trades if t.direction == "long"]
        shorts = [t for t in trades if t.direction == "short"]
        r.long_trades = len(longs)
        r.long_wr = (sum(1 for t in longs if t.is_win) / len(longs) * 100) if longs else 0
        r.short_trades = len(shorts)
        r.short_wr = (sum(1 for t in shorts if t.is_win) / len(shorts) * 100) if shorts else 0

    r.fees = m.total_fees
    r.slippage = m.total_slippage_cost

    if pipeline_result.walk_forward:
        r.wf_robust = pipeline_result.walk_forward.is_robust
    if pipeline_result.monte_carlo:
        r.mc_robust = pipeline_result.monte_carlo.is_robust

    return r


# ═══════════════════════════════════════════
#  Data Loading
# ═══════════════════════════════════════════

async def load_data(
    symbol: str, timeframe: str, days: int,
) -> Optional[List[BTCandle]]:
    """Load real candles from Bitget."""
    loader = RealDataLoader()
    logger.info(f"Fetching {symbol} {timeframe} candles ({days} days)...")

    result: LoadResult = await loader.load(
        symbol=symbol,
        timeframe=timeframe,
        days=days,
        force_api=True,
    )

    candles = result.candles
    if not candles:
        logger.error(f"No data for {symbol} {timeframe}")
        return None

    logger.info(f"  Candles: {len(candles)}")
    logger.info(f"  Source:  {result.source}")
    if result.validation:
        logger.info(f"  Coverage:    {result.validation.coverage_pct:.1f}%")
        logger.info(f"  Gaps:        {result.validation.gaps_found}")
        logger.info(f"  Valid:       {'YES' if result.validation.is_valid else 'NO'}")
    logger.info(f"  Price range: ${min(c.close for c in candles):.2f} - ${max(c.close for c in candles):.2f}")

    return candles


# ═══════════════════════════════════════════
#  Run a single alpha test
# ═══════════════════════════════════════════

def run_alpha(
    name: str,
    signal_fn: Callable,
    candles: List[BTCandle],
    symbol: str,
    pipeline_config: PipelineConfig,
) -> PipelineResult:
    """Run full pipeline on one alpha."""
    pipeline = BacktestPipeline(pipeline_config)
    return pipeline.run(symbol, candles, signal_fn)


# ═══════════════════════════════════════════
#  Report
# ═══════════════════════════════════════════

def format_comparison_table(results: List[AlphaResult]) -> str:
    lines = []
    lines.append("=" * 110)
    lines.append("  COMPARISON TABLE — CHANTIER 13 ALL AXES")
    lines.append("=" * 110)
    header = f"{'Axe':<5} {'Alpha':<28} {'Sym':<8} {'TF':<4} {'Trades':>6} {'WR%':>6} {'PF':>6} {'Exp$':>8} {'PnL$':>10} {'DD%':>6} {'Edge':>6} {'Score':>5} {'Verdict':<14}"
    lines.append(header)
    lines.append("-" * 110)

    for r in results:
        line = f"  {r.axe:<3} {r.name:<28} {r.symbol:<8} {r.timeframe:<4} {r.trades:>6} {r.win_rate:>5.1f}% {r.pf:>5.2f} {r.expectancy:>7.2f} {r.net_pnl:>9.2f} {r.max_dd:>5.1f}% {r.edge_ratio:>5.2f} {r.score:>5} {r.verdict:<14}"
        lines.append(line)

    lines.append("-" * 110)
    return "\n".join(lines)


def format_verdict(results: List[AlphaResult]) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 70)
    lines.append("  VERDICT FINAL — CHANTIER 13")
    lines.append("  REPOSITIONNEMENT: HTF / LONG-ONLY / FUNDING / MULTI-MARKET")
    lines.append("=" * 70)

    any_edge = False
    any_promising = False

    for r in results:
        lines.append(f"\n  [{r.axe}] {r.name} ({r.symbol} {r.timeframe}):")
        checks = [
            ("Expectancy > 0", r.expectancy > 0, f"${r.expectancy:.2f}"),
            ("PF >= 1.3", r.pf >= 1.3, f"{r.pf:.2f}"),
            ("Edge Ratio > 1", r.edge_ratio > 1.0, f"{r.edge_ratio:.2f}"),
            ("Max DD <= 25%", r.max_dd <= 25.0, f"{r.max_dd:.1f}%"),
            ("WF Robust", r.wf_robust, "YES" if r.wf_robust else "NO"),
            ("MC Robust", r.mc_robust, "YES" if r.mc_robust else "NO"),
            ("Trades >= 10", r.trades >= 10, str(r.trades)),
        ]
        passed = sum(1 for _, ok, _ in checks if ok)
        for label, ok, val in checks:
            mark = "[PASS]" if ok else "[FAIL]"
            lines.append(f"    {mark} {label:<20} = {val}")
        lines.append(f"    Score: {passed}/7 criteria passed")

        if "CONFIRMED" in r.verdict:
            any_edge = True
        if r.pf > 1.0 and r.expectancy > 0:
            any_promising = True

    lines.append("")
    if any_edge:
        lines.append("  " + "=" * 55)
        lines.append("  |                                                     |")
        lines.append("  |                    EDGE FOUND                       |")
        lines.append("  |                                                     |")
        lines.append("  " + "=" * 55)
    elif any_promising:
        lines.append("  " + "=" * 55)
        lines.append("  |                                                     |")
        lines.append("  |                    EDGE WEAK                        |")
        lines.append("  |          (prometteuse mais pas robuste)             |")
        lines.append("  |                                                     |")
        lines.append("  " + "=" * 55)
    else:
        lines.append("  " + "=" * 55)
        lines.append("  |                                                     |")
        lines.append("  |                      NO EDGE                        |")
        lines.append("  |                                                     |")
        lines.append("  " + "=" * 55)
        lines.append("  Aucune alpha n'a d'edge sur donnees reelles.")

    # Diagnostic
    lines.append("")
    lines.append("  DIAGNOSTIC:")
    best = max(results, key=lambda r: r.pf) if results else None
    if best:
        lines.append(f"  - Meilleure alpha: {best.name} ({best.symbol} {best.timeframe})")
        lines.append(f"    PF={best.pf:.2f}, WR={best.win_rate:.1f}%, Exp=${best.expectancy:.2f}")
        if best.pf < 1.0:
            lines.append(f"    Meme la meilleure perd de l'argent (PF < 1)")
        if best.slippage > abs(best.net_pnl) * 0.5:
            lines.append(f"    Slippage domine: ${best.slippage:.0f} vs PnL ${best.net_pnl:.0f}")

    # Best axis
    for axe_num in range(1, 5):
        axe_results = [r for r in results if r.axe == axe_num]
        if axe_results:
            best_axe = max(axe_results, key=lambda r: r.pf)
            axe_names = {1: "HTF TREND", 2: "LONG-ONLY", 3: "FUNDING", 4: "MULTI-MARKET"}
            lines.append(f"  - AXE {axe_num} ({axe_names.get(axe_num, '?')}): best PF={best_axe.pf:.2f}, {best_axe.trades} trades")

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  Main Runner
# ═══════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--axes", type=str, default="1,2,3,4", help="Axes to run: 1,2,3,4")
    args = parser.parse_args()

    axes_to_run = [int(x.strip()) for x in args.axes.split(",")]
    days = args.days

    logger.info("")
    logger.info("=" * 70)
    logger.info("  CHANTIER 13 — REPOSITIONNEMENT COMPLET")
    logger.info(f"  Axes: {axes_to_run} | Days: {days}")
    logger.info(f"  Date: {datetime.now(timezone.utc).isoformat()}")
    logger.info("=" * 70)

    all_results: List[AlphaResult] = []
    total_start = time.time()

    # ══════════════════════════════════════════
    #  AXE 1 — HTF TREND (4H + 1D)
    # ══════════════════════════════════════════
    if 1 in axes_to_run:
        logger.info("\n" + "=" * 70)
        logger.info("  AXE 1 — HIGH TIMEFRAME TREND FOLLOWING")
        logger.info("=" * 70)

        for tf, tf_bars_per_day, hold_bars in [("4h", 6, 60), ("1d", 1, 20)]:
            symbol = "BTCUSDT"
            logger.info(f"\n  Testing {symbol} {tf}...")

            candles = await load_data(symbol, tf, days)
            if not candles or len(candles) < 50:
                logger.warning(f"  Insufficient data for {symbol} {tf}")
                continue

            cfg = HTFTrendConfig()
            if tf == "1d":
                # Adjust config for daily
                cfg.ema_fast = 5
                cfg.ema_mid = 13
                cfg.ema_slow = 34
                cfg.ema_filter = 55
                cfg.pullback_lookback = 5
                cfg.min_bars_between = 3
                cfg.max_holding_bars = 20
                cfg.min_target_bps = 400.0  # Higher targets on daily

            signal_fn = make_htf_trend_fn(cfg)
            pipe_cfg = make_pipeline_config_htf(tf, hold_bars)

            t0 = time.time()
            result = run_alpha(f"HTF_TREND_{tf.upper()}", signal_fn, candles, symbol, pipe_cfg)
            dur = time.time() - t0

            alpha_r = extract_result(1, f"HTF Trend {tf.upper()}", symbol, tf, result, dur)
            all_results.append(alpha_r)

            logger.info(f"  {alpha_r.name}: {alpha_r.trades} trades, PF={alpha_r.pf:.2f}, "
                        f"Exp=${alpha_r.expectancy:.2f}, DD={alpha_r.max_dd:.1f}%, "
                        f"Score={alpha_r.score} — {alpha_r.verdict} ({dur:.1f}s)")

    # ══════════════════════════════════════════
    #  AXE 2 — LONG-ONLY BIAS (4H)
    # ══════════════════════════════════════════
    if 2 in axes_to_run:
        logger.info("\n" + "=" * 70)
        logger.info("  AXE 2 — LONG-ONLY BIAS (Bull Regime)")
        logger.info("=" * 70)

        for symbol in ["BTCUSDT"]:
            tf = "4h"
            logger.info(f"\n  Testing {symbol} {tf} LONG-ONLY...")

            candles = await load_data(symbol, tf, days)
            if not candles or len(candles) < 50:
                continue

            signal_fn = make_long_only_fn()
            pipe_cfg = make_pipeline_config_htf(tf, 60)

            t0 = time.time()
            result = run_alpha("LONG_ONLY_4H", signal_fn, candles, symbol, pipe_cfg)
            dur = time.time() - t0

            alpha_r = extract_result(2, f"Long-Only {tf.upper()}", symbol, tf, result, dur)
            all_results.append(alpha_r)

            logger.info(f"  {alpha_r.name}: {alpha_r.trades} trades, PF={alpha_r.pf:.2f}, "
                        f"Exp=${alpha_r.expectancy:.2f}, {alpha_r.verdict} ({dur:.1f}s)")

    # ══════════════════════════════════════════
    #  AXE 3 — FUNDING ARBITRAGE (4H)
    # ══════════════════════════════════════════
    if 3 in axes_to_run:
        logger.info("\n" + "=" * 70)
        logger.info("  AXE 3 — FUNDING / CARRY STRATEGIES")
        logger.info("=" * 70)

        for symbol in ["BTCUSDT"]:
            tf = "4h"

            candles = await load_data(symbol, tf, days)
            if not candles or len(candles) < 50:
                continue

            # Mode A: Counter-crowd
            logger.info(f"\n  Testing {symbol} {tf} FUNDING COUNTER-CROWD...")
            cfg_cc = FundingConfig(mode="counter_crowd")
            signal_fn = make_funding_arb_fn(cfg_cc)
            pipe_cfg = make_pipeline_config_htf(tf, 42)

            t0 = time.time()
            result = run_alpha("FUNDING_CC", signal_fn, candles, symbol, pipe_cfg)
            dur = time.time() - t0

            alpha_r = extract_result(3, "Funding Counter-Crowd", symbol, tf, result, dur)
            all_results.append(alpha_r)
            logger.info(f"  {alpha_r.name}: {alpha_r.trades} trades, PF={alpha_r.pf:.2f}, "
                        f"Exp=${alpha_r.expectancy:.2f}, {alpha_r.verdict} ({dur:.1f}s)")

            # Mode B: Trend-aligned
            logger.info(f"\n  Testing {symbol} {tf} FUNDING TREND-ALIGNED...")
            cfg_ta = FundingConfig(mode="trend_aligned", use_trend_filter=True)
            signal_fn = make_funding_arb_fn(cfg_ta)

            t0 = time.time()
            result = run_alpha("FUNDING_TA", signal_fn, candles, symbol, pipe_cfg)
            dur = time.time() - t0

            alpha_r = extract_result(3, "Funding Trend-Aligned", symbol, tf, result, dur)
            all_results.append(alpha_r)
            logger.info(f"  {alpha_r.name}: {alpha_r.trades} trades, PF={alpha_r.pf:.2f}, "
                        f"Exp=${alpha_r.expectancy:.2f}, {alpha_r.verdict} ({dur:.1f}s)")

    # ══════════════════════════════════════════
    #  AXE 4 — MARKET SELECTION (ETH, SOL on 4H)
    # ══════════════════════════════════════════
    if 4 in axes_to_run:
        logger.info("\n" + "=" * 70)
        logger.info("  AXE 4 — MARKET SELECTION (Altcoins)")
        logger.info("=" * 70)

        for symbol in ["ETHUSDT", "SOLUSDT"]:
            tf = "4h"
            logger.info(f"\n  Testing {symbol} {tf} HTF TREND...")

            candles = await load_data(symbol, tf, days)
            if not candles or len(candles) < 50:
                logger.warning(f"  Insufficient data for {symbol}")
                continue

            # Use HTF trend on altcoins
            cfg = HTFTrendConfig()
            cfg.min_target_bps = 300.0  # Altcoins can be more volatile

            signal_fn = make_htf_trend_fn(cfg)
            pipe_cfg = make_pipeline_config_htf(tf, 60)

            t0 = time.time()
            result = run_alpha(f"HTF_TREND_{symbol}", signal_fn, candles, symbol, pipe_cfg)
            dur = time.time() - t0

            alpha_r = extract_result(4, f"HTF Trend {symbol[:3]}", symbol, tf, result, dur)
            all_results.append(alpha_r)

            logger.info(f"  {alpha_r.name}: {alpha_r.trades} trades, PF={alpha_r.pf:.2f}, "
                        f"Exp=${alpha_r.expectancy:.2f}, {alpha_r.verdict} ({dur:.1f}s)")

            # Also test Long-Only on altcoins
            logger.info(f"\n  Testing {symbol} {tf} LONG-ONLY...")
            signal_fn2 = make_long_only_fn()

            t0 = time.time()
            result2 = run_alpha(f"LONG_ONLY_{symbol}", signal_fn2, candles, symbol, pipe_cfg)
            dur2 = time.time() - t0

            alpha_r2 = extract_result(4, f"Long-Only {symbol[:3]}", symbol, tf, result2, dur2)
            all_results.append(alpha_r2)

            logger.info(f"  {alpha_r2.name}: {alpha_r2.trades} trades, PF={alpha_r2.pf:.2f}, "
                        f"Exp=${alpha_r2.expectancy:.2f}, {alpha_r2.verdict} ({dur2:.1f}s)")

    # ══════════════════════════════════════════
    #  FINAL REPORT
    # ══════════════════════════════════════════
    total_dur = time.time() - total_start

    logger.info("\n" + format_comparison_table(all_results))
    logger.info(format_verdict(all_results))
    logger.info(f"\n  Total execution time: {total_dur:.1f}s")

    # Save report
    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "reports",
        f"chantier13_repositionnement_{days}d.txt",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    report_lines = []
    report_lines.append("=" * 70)
    report_lines.append("  CHANTIER 13 — REPOSITIONNEMENT COMPLET")
    report_lines.append(f"  Axes: {axes_to_run} | Days: {days}")
    report_lines.append(f"  Date: {datetime.now(timezone.utc).isoformat()}")
    report_lines.append("=" * 70)

    for r in all_results:
        report_lines.append("")
        report_lines.append(f"  [{r.axe}] {r.name} ({r.symbol} {r.timeframe})")
        report_lines.append(f"    Trades: {r.trades}")
        report_lines.append(f"    Win Rate: {r.win_rate:.1f}%")
        report_lines.append(f"    Profit Factor: {r.pf:.2f}")
        report_lines.append(f"    Expectancy: ${r.expectancy:.2f}/trade")
        report_lines.append(f"    Net PnL: ${r.net_pnl:.2f}")
        report_lines.append(f"    Max DD: {r.max_dd:.1f}%")
        report_lines.append(f"    Sharpe: {r.sharpe:.2f}")
        report_lines.append(f"    Edge Ratio: {r.edge_ratio:.2f}")
        report_lines.append(f"    Score: {r.score}/100")
        report_lines.append(f"    Verdict: {r.verdict}")
        report_lines.append(f"    L/S: {r.long_trades}L ({r.long_wr:.1f}%WR) / {r.short_trades}S ({r.short_wr:.1f}%WR)")
        report_lines.append(f"    Fees: ${r.fees:.2f} | Slippage: ${r.slippage:.2f}")
        report_lines.append(f"    WF: {'ROBUST' if r.wf_robust else 'NOT ROBUST'} | MC: {'ROBUST' if r.mc_robust else 'NOT ROBUST'}")
        report_lines.append(f"    Duration: {r.duration_s:.1f}s")

    report_lines.append("")
    report_lines.append(format_comparison_table(all_results))
    report_lines.append(format_verdict(all_results))
    report_lines.append(f"\n  Total execution time: {total_dur:.1f}s")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    logger.info(f"\nReport saved to: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
