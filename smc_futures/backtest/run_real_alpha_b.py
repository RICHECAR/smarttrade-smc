"""
Chantier 11 — Real Data Alpha B Validation
════════════════════════════════════════════
Fetches REAL Bitget BTCUSDT 5m candles, validates the data,
then runs Alpha B (Momentum Breakout) through the complete
realistic pipeline with enhanced microstructure model.

Delivers:
  1. Data quality report
  2. Alpha B backtest on REAL data (engine + slippage + WF + MC)
  3. Detailed analysis (expectancy, distribution, regime, session)
  4. Synthetic vs Real comparison
  5. Final verdict: EDGE CONFIRMED / EDGE WEAK / EDGE DEAD

Rule: Ne jamais ajuster les paramètres pour forcer un résultat.

Usage:
    python -m smc_futures.backtest.run_real_alpha_b [--days 90] [--symbol BTCUSDT]
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
from typing import List, Optional, Dict

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
from smc_futures.backtest.alpha_momentum_breakout import (
    make_momentum_breakout_fn, MomentumBreakoutConfig,
)
from smc_futures.backtest.real_data_loader import (
    RealDataLoader, DataValidator, LoadResult,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  ALPHA B DETAILED ANALYSIS
# ═══════════════════════════════════════════

@dataclass
class AlphaBAnalysis:
    """Detailed analysis of Alpha B trades on real data."""
    # Per-direction stats
    long_trades: int = 0
    short_trades: int = 0
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    long_pf: float = 0.0
    short_pf: float = 0.0

    # Distribution
    pnl_percentiles: Dict[str, float] = field(default_factory=dict)
    best_trade: float = 0.0
    worst_trade: float = 0.0
    pnl_skew: float = 0.0

    # By regime
    regime_stats: Dict[str, Dict] = field(default_factory=dict)

    # By session (UTC hour ranges)
    session_stats: Dict[str, Dict] = field(default_factory=dict)

    # Hold time distribution
    avg_hold_bars: float = 0.0
    median_hold_bars: float = 0.0
    hold_bars_winners: float = 0.0
    hold_bars_losers: float = 0.0

    # Edge ratio
    avg_mae_bps: float = 0.0
    avg_mfe_bps: float = 0.0
    edge_ratio: float = 0.0

    # Cost analysis
    avg_entry_slippage_bps: float = 0.0
    avg_exit_slippage_bps: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0
    total_slippage_cost: float = 0.0
    cost_per_trade: float = 0.0


def analyze_alpha_b_trades(trades: List[RealisticTrade]) -> AlphaBAnalysis:
    """Deep analysis of Alpha B trade characteristics."""
    a = AlphaBAnalysis()
    if not trades:
        return a

    # Direction analysis
    longs = [t for t in trades if t.direction == "long"]
    shorts = [t for t in trades if t.direction == "short"]
    a.long_trades = len(longs)
    a.short_trades = len(shorts)

    if longs:
        long_wins = sum(1 for t in longs if t.is_win)
        a.long_win_rate = long_wins / len(longs) * 100
        long_gross = sum(t.net_pnl for t in longs if t.is_win)
        long_loss = abs(sum(t.net_pnl for t in longs if not t.is_win))
        a.long_pf = long_gross / long_loss if long_loss > 0 else float("inf") if long_gross > 0 else 0.0

    if shorts:
        short_wins = sum(1 for t in shorts if t.is_win)
        a.short_win_rate = short_wins / len(shorts) * 100
        short_gross = sum(t.net_pnl for t in shorts if t.is_win)
        short_loss = abs(sum(t.net_pnl for t in shorts if not t.is_win))
        a.short_pf = short_gross / short_loss if short_loss > 0 else float("inf") if short_gross > 0 else 0.0

    # PnL distribution
    pnls = sorted([t.net_pnl for t in trades])
    a.best_trade = max(pnls)
    a.worst_trade = min(pnls)

    def _pct(data, p):
        idx = int(len(data) * p / 100)
        idx = max(0, min(idx, len(data) - 1))
        return data[idx]

    a.pnl_percentiles = {
        "5th": _pct(pnls, 5),
        "25th": _pct(pnls, 25),
        "50th": _pct(pnls, 50),
        "75th": _pct(pnls, 75),
        "95th": _pct(pnls, 95),
    }

    # Skewness
    mean = sum(pnls) / len(pnls)
    std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls)) if len(pnls) > 1 else 0
    if std > 0 and len(pnls) > 2:
        a.pnl_skew = sum((p - mean) ** 3 for p in pnls) / (len(pnls) * std ** 3)

    # By volatility regime
    for regime in ["low", "normal", "high", "extreme"]:
        r_trades = [t for t in trades if t.volatility_regime == regime]
        if r_trades:
            r_wins = sum(1 for t in r_trades if t.is_win)
            r_pnl = sum(t.net_pnl for t in r_trades)
            a.regime_stats[regime] = {
                "trades": len(r_trades),
                "win_rate": r_wins / len(r_trades) * 100,
                "net_pnl": r_pnl,
                "avg_pnl": r_pnl / len(r_trades),
            }

    # By session (UTC hour blocks)
    sessions = {
        "Asia (00-08)": (0, 8),
        "London (08-14)": (8, 14),
        "NY (14-21)": (14, 21),
        "Off-hours (21-00)": (21, 24),
    }
    for name, (h_start, h_end) in sessions.items():
        s_trades = [
            t for t in trades
            if h_start <= datetime.fromtimestamp(
                t.entry_timestamp / 1000, tz=timezone.utc
            ).hour < h_end
        ]
        if s_trades:
            s_wins = sum(1 for t in s_trades if t.is_win)
            s_pnl = sum(t.net_pnl for t in s_trades)
            a.session_stats[name] = {
                "trades": len(s_trades),
                "win_rate": s_wins / len(s_trades) * 100,
                "net_pnl": s_pnl,
                "avg_pnl": s_pnl / len(s_trades),
            }

    # Hold time
    hold_bars = [t.holding_bars for t in trades]
    a.avg_hold_bars = sum(hold_bars) / len(hold_bars)
    sorted_bars = sorted(hold_bars)
    a.median_hold_bars = sorted_bars[len(sorted_bars) // 2]

    winners = [t for t in trades if t.is_win]
    losers = [t for t in trades if not t.is_win]
    if winners:
        a.hold_bars_winners = sum(t.holding_bars for t in winners) / len(winners)
    if losers:
        a.hold_bars_losers = sum(t.holding_bars for t in losers) / len(losers)

    # Edge ratio (MAE / MFE)
    if trades[0].actual_entry > 0:  # Check if MAE/MFE populated
        maes = [abs(t.mae) for t in trades if t.mae != 0]
        mfes = [abs(t.mfe) for t in trades if t.mfe != 0]
        if maes:
            avg_mae = sum(maes) / len(maes)
            a.avg_mae_bps = avg_mae / trades[0].actual_entry * 10000 if trades[0].actual_entry > 0 else 0
        if mfes:
            avg_mfe = sum(mfes) / len(mfes)
            a.avg_mfe_bps = avg_mfe / trades[0].actual_entry * 10000 if trades[0].actual_entry > 0 else 0
        if a.avg_mae_bps > 0:
            a.edge_ratio = a.avg_mfe_bps / a.avg_mae_bps

    # Cost analysis
    a.total_fees = sum(t.total_fees for t in trades)
    a.total_funding = sum(t.funding_cost for t in trades)
    for t in trades:
        slip = abs(t.actual_entry - t.intended_entry) * t.qty if t.intended_entry > 0 else 0
        a.total_slippage_cost += slip
        a.avg_entry_slippage_bps += t.entry_slippage_bps

    a.avg_entry_slippage_bps /= len(trades)
    total_cost = a.total_fees + a.total_funding + a.total_slippage_cost
    a.cost_per_trade = total_cost / len(trades)

    return a


def format_alpha_b_analysis(a: AlphaBAnalysis) -> str:
    """Format detailed Alpha B analysis report."""
    lines = [
        "",
        "=" * 70,
        "  ALPHA B — DETAILED ANALYSIS (MOMENTUM BREAKOUT)",
        "=" * 70,
        "",
        "  DIRECTION BREAKDOWN:",
        f"    Long Trades:  {a.long_trades}  WR: {a.long_win_rate:.1f}%  PF: {a.long_pf:.2f}",
        f"    Short Trades: {a.short_trades}  WR: {a.short_win_rate:.1f}%  PF: {a.short_pf:.2f}",
        "",
        "  PnL DISTRIBUTION:",
        f"    Best Trade:   ${a.best_trade:.2f}",
        f"    Worst Trade:  ${a.worst_trade:.2f}",
        f"    5th pctile:   ${a.pnl_percentiles.get('5th', 0):.2f}",
        f"    25th pctile:  ${a.pnl_percentiles.get('25th', 0):.2f}",
        f"    Median:       ${a.pnl_percentiles.get('50th', 0):.2f}",
        f"    75th pctile:  ${a.pnl_percentiles.get('75th', 0):.2f}",
        f"    95th pctile:  ${a.pnl_percentiles.get('95th', 0):.2f}",
        f"    Skewness:     {a.pnl_skew:.2f}  {'(+skew=good)' if a.pnl_skew > 0 else '(-skew=bad)'}",
        "",
        "  VOLATILITY REGIME:",
    ]

    for regime, stats in a.regime_stats.items():
        lines.append(
            f"    {regime:10s}: {stats['trades']:>3} trades  "
            f"WR: {stats['win_rate']:>5.1f}%  "
            f"Net: ${stats['net_pnl']:>8.2f}  "
            f"Avg: ${stats['avg_pnl']:>6.2f}"
        )

    if not a.regime_stats:
        lines.append("    No regime data available")

    lines.extend([
        "",
        "  SESSION ANALYSIS (UTC):",
    ])
    for name, stats in a.session_stats.items():
        lines.append(
            f"    {name:20s}: {stats['trades']:>3} trades  "
            f"WR: {stats['win_rate']:>5.1f}%  "
            f"Net: ${stats['net_pnl']:>8.2f}"
        )

    if not a.session_stats:
        lines.append("    No session data available")

    lines.extend([
        "",
        "  HOLD TIME:",
        f"    Average:  {a.avg_hold_bars:.1f} bars  ({a.avg_hold_bars * 5:.0f} min)",
        f"    Median:   {a.median_hold_bars:.0f} bars  ({a.median_hold_bars * 5:.0f} min)",
        f"    Winners:  {a.hold_bars_winners:.1f} bars",
        f"    Losers:   {a.hold_bars_losers:.1f} bars",
        "",
        "  EDGE RATIO (MFE/MAE):",
        f"    Avg MAE:     {a.avg_mae_bps:.1f} bps",
        f"    Avg MFE:     {a.avg_mfe_bps:.1f} bps",
        f"    Edge Ratio:  {a.edge_ratio:.2f}  {'(>1 = edge exists)' if a.edge_ratio > 1 else '(<1 = no edge)'}",
        "",
        "  COST BREAKDOWN:",
        f"    Fees:              ${a.total_fees:.2f}",
        f"    Funding:           ${a.total_funding:.2f}",
        f"    Slippage Est.:     ${a.total_slippage_cost:.2f}",
        f"    Avg Entry Slip:    {a.avg_entry_slippage_bps:.1f} bps",
        f"    Cost per Trade:    ${a.cost_per_trade:.2f}",
    ])

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  SYNTHETIC vs REAL COMPARISON
# ═══════════════════════════════════════════

def format_synthetic_vs_real(
    synthetic_result: Optional[PipelineResult],
    real_result: PipelineResult,
) -> str:
    """Compare synthetic and real data results side by side."""
    lines = [
        "",
        "=" * 70,
        "  SYNTHETIC vs REAL DATA COMPARISON",
        "=" * 70,
    ]

    if not synthetic_result or not synthetic_result.metrics:
        lines.append("  (No synthetic comparison available)")
        return "\n".join(lines)

    s = synthetic_result.metrics
    r = real_result.metrics

    if not r:
        lines.append("  (No real data metrics available)")
        return "\n".join(lines)

    lines.extend([
        f"  {'Metric':<25} {'Synthetic':>12} {'REAL':>12} {'Delta':>12}",
        f"  {'─' * 25} {'─' * 12} {'─' * 12} {'─' * 12}",
    ])

    def _row(label, sv, rv, fmt=".2f"):
        sf = f"{sv:{fmt}}"
        rf = f"{rv:{fmt}}"
        if isinstance(sv, (int, float)) and isinstance(rv, (int, float)):
            delta = rv - sv
            return f"  {label:<25} {sf:>12} {rf:>12} {delta:>+12{fmt}}"
        return f"  {label:<25} {sf:>12} {rf:>12} {'N/A':>12}"

    lines.append(_row("Trades", s.total_trades, r.total_trades, "d"))
    lines.append(_row("Win Rate (%)", s.win_rate, r.win_rate, ".1f"))
    lines.append(_row("Profit Factor", s.profit_factor, r.profit_factor))
    lines.append(_row("Expectancy ($)", s.expectancy, r.expectancy))
    lines.append(_row("Net PnL ($)", s.total_net_pnl, r.total_net_pnl))
    lines.append(_row("Max DD (%)", s.max_drawdown_pct, r.max_drawdown_pct, ".1f"))
    lines.append(_row("Sharpe", s.sharpe_ratio, r.sharpe_ratio))
    lines.append(_row("Avg Hold (bars)", s.avg_holding_bars, r.avg_holding_bars, ".0f"))

    lines.extend([
        "",
        "  INTERPRETATION:",
    ])

    # Key comparison insights
    if r.total_trades < 10:
        lines.append("  WARNING: Too few trades on real data for statistical significance")
    if r.total_trades > s.total_trades * 2:
        lines.append("  Real data generates MORE signals — synthetic was too conservative")
    elif r.total_trades < s.total_trades * 0.5:
        lines.append("  Real data generates FEWER signals — patterns rarer than synthetic")

    if r.expectancy > 0 and s.expectancy > 0:
        lines.append("  Both positive — edge direction confirmed across data types")
    elif r.expectancy <= 0 and s.expectancy > 0:
        lines.append("  DEGRADATION: Edge disappears on real data — SYNTHETIC WAS MISLEADING")
    elif r.expectancy > 0 and s.expectancy <= 0:
        lines.append("  IMPROVEMENT: Real data shows edge not visible in synthetic")

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  FINAL VERDICT
# ═══════════════════════════════════════════

def compute_final_verdict(
    real_result: PipelineResult,
    analysis: AlphaBAnalysis,
) -> str:
    """
    Compute the final Chantier 11 verdict for Alpha B.
    
    EDGE CONFIRMED: exp > 0, PF >= 1.2, MC robust, multi-period
    EDGE WEAK: some positive signals but fails robustness
    EDGE DEAD: no edge on real data
    """
    lines = [
        "",
        "=" * 70,
        "  VERDICT FINAL — CHANTIER 11",
        "  ALPHA B (Momentum Breakout) sur DONNEES REELLES",
        "=" * 70,
        "",
    ]

    m = real_result.metrics
    wf = real_result.walk_forward
    mc = real_result.monte_carlo

    # Criteria checks
    checks = []

    # 1. Profitability
    if m and m.expectancy > 0:
        checks.append(("Expectancy > 0", True, f"${m.expectancy:.2f}"))
    else:
        exp_val = f"${m.expectancy:.2f}" if m else "N/A"
        checks.append(("Expectancy > 0", False, exp_val))

    # 2. Profit Factor
    if m and m.profit_factor >= 1.2:
        checks.append(("PF >= 1.2", True, f"{m.profit_factor:.2f}"))
    else:
        pf_val = f"{m.profit_factor:.2f}" if m else "N/A"
        checks.append(("PF >= 1.2", False, pf_val))

    # 3. Sample size
    if m and m.total_trades >= 30:
        checks.append(("Trades >= 30", True, f"{m.total_trades}"))
    else:
        t_val = f"{m.total_trades}" if m else "0"
        checks.append(("Trades >= 30", False, t_val))

    # 4. Drawdown
    if m and m.max_drawdown_pct <= 25.0:
        checks.append(("Max DD <= 25%", True, f"{m.max_drawdown_pct:.1f}%"))
    else:
        dd_val = f"{m.max_drawdown_pct:.1f}%" if m else "N/A"
        checks.append(("Max DD <= 25%", False, dd_val))

    # 5. Walk-forward
    if wf and wf.is_robust:
        checks.append(("WF Robust", True, f"OOS PF={wf.oos_profit_factor:.2f}"))
    elif wf:
        checks.append(("WF Robust", False, f"OOS PF={wf.oos_profit_factor:.2f}"))
    else:
        checks.append(("WF Robust", False, "SKIPPED (not enough data)"))

    # 6. Monte Carlo
    if mc and mc.is_robust:
        checks.append(("MC Robust", True, f"score={mc.robustness_score:.2f}"))
    elif mc:
        checks.append(("MC Robust", False, f"score={mc.robustness_score:.2f}"))
    else:
        checks.append(("MC Robust", False, "SKIPPED (too few trades)"))

    # 7. Edge ratio
    if analysis.edge_ratio > 1.0:
        checks.append(("Edge Ratio > 1", True, f"{analysis.edge_ratio:.2f}"))
    else:
        checks.append(("Edge Ratio > 1", False, f"{analysis.edge_ratio:.2f}"))

    # Display checks
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)

    for name, ok, val in checks:
        icon = "PASS" if ok else "FAIL"
        lines.append(f"  [{icon}] {name:<20s} = {val}")

    lines.append(f"\n  Score: {passed}/{total} criteria passed")

    # Determine verdict
    critical_pass = all(ok for name, ok, _ in checks if name in [
        "Expectancy > 0", "PF >= 1.2", "Trades >= 30"
    ])

    if passed >= 6 and critical_pass:
        verdict = "EDGE CONFIRMED"
        explanation = (
            "Alpha B montre un edge statistiquement significatif sur donnees reelles.\n"
            "  Deploiement en paper trading recommande pour validation live."
        )
    elif passed >= 4 and m and m.expectancy > 0:
        verdict = "EDGE WEAK"
        explanation = (
            "Alpha B montre des signaux positifs mais manque de robustesse.\n"
            "  Edge fragile — necessite plus de donnees ou amelioration."
        )
    else:
        verdict = "EDGE DEAD"
        explanation = (
            "Alpha B n'a PAS d'edge sur donnees reelles.\n"
            "  L'edge synthetique etait un artefact — RETOUR A LA RECHERCHE."
        )

    lines.extend([
        "",
        f"  ╔{'═' * 50}╗",
        f"  ║  {verdict:^48s}  ║",
        f"  ╚{'═' * 50}╝",
        "",
        f"  {explanation}",
    ])

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  PIPELINE CONFIG
# ═══════════════════════════════════════════

def make_real_pipeline_config(n_bars: int) -> PipelineConfig:
    """Pipeline config for real data — same parameters as Chantier 10."""
    # Walk-forward needs minimum data
    wf_config = None
    if n_bars >= 2000:
        train = max(800, n_bars // 4)
        validate = max(300, n_bars // 8)
        test = max(300, n_bars // 8)
        wf_config = WalkForwardConfig(
            train_bars=train,
            validate_bars=validate,
            test_bars=test,
            step_bars=max(100, test // 2),
        )

    mc_config = MonteCarloConfig(
        n_permutations=500,
        n_param_variations=200,
        confidence_level=0.95,
    )

    return PipelineConfig(
        bt_config=RealisticBacktestConfig(
            initial_capital=10_000.0,
            risk_per_trade=0.01,
            leverage=10,
            max_open_positions=1,
            max_trades_per_day=5,
            cooldown_bars=5,
        ),
        wf_config=wf_config,
        mc_config=mc_config,
        max_holding_bars=288,   # 24h on 5m bars
        run_naive_comparison=False,  # Realistic ONLY
    )


# ═══════════════════════════════════════════
#  SYNTHETIC REFERENCE RUN (for comparison)
# ═══════════════════════════════════════════

def run_synthetic_reference(symbol: str, n_bars: int = 5000, seed: int = 42) -> PipelineResult:
    """Run Alpha B on synthetic data for comparison."""
    from smc_futures.backtest.run_new_alphas import make_synthetic_candles

    logger.info("Running synthetic reference (Chantier 10 equivalent)...")
    candles = make_synthetic_candles(n_bars, seed=seed)
    cfg = make_real_pipeline_config(n_bars)
    pipeline = BacktestPipeline(cfg)
    fn = make_momentum_breakout_fn()
    result = pipeline.run(symbol, candles, fn)
    logger.info(f"Synthetic: {result.metrics.total_trades if result.metrics else 0} trades")
    return result


# ═══════════════════════════════════════════
#  MAIN RUNNER
# ═══════════════════════════════════════════

async def run_real_alpha_b(
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    days: int = 90,
    run_comparison: bool = True,
) -> str:
    """
    Main entry: fetch real data → validate → backtest Alpha B → verdict.
    Returns the full report as a string.
    """
    report_lines = []

    def log_section(text: str):
        report_lines.append(text)
        logger.info(text)

    log_section("")
    log_section("=" * 70)
    log_section("  CHANTIER 11 — ALPHA B VALIDATION SUR DONNEES REELLES")
    log_section(f"  Symbol: {symbol}  TF: {timeframe}  Jours: {days}")
    log_section("  Regle: Ne jamais ajuster les parametres pour forcer un resultat")
    log_section("=" * 70)

    # ── Step 1: Fetch real data ──
    log_section("\n[1/6] FETCHING REAL DATA FROM BITGET...")
    loader = RealDataLoader()
    data = await loader.load(symbol, timeframe, days)

    if not data.candles:
        log_section("ERREUR CRITIQUE: Aucune donnee recuperee. Abandon.")
        return "\n".join(report_lines)

    # ── Step 2: Validate ──
    log_section(f"\n[2/6] DATA VALIDATION ({len(data.candles)} candles from {data.source})...")
    validation_report = DataValidator.format_report(data.validation)
    log_section(validation_report)

    if not data.is_valid:
        log_section("\nDATA REJECTED — Cannot proceed with invalid data.")
        log_section("Fix data issues before re-running.")
        return "\n".join(report_lines)

    candles = data.candles

    # ── Step 3: Run Alpha B on real data ──
    log_section(f"\n[3/6] RUNNING ALPHA B (Momentum Breakout) on {len(candles)} REAL candles...")
    pipeline_cfg = make_real_pipeline_config(len(candles))
    pipeline = BacktestPipeline(pipeline_cfg)
    fn_b = make_momentum_breakout_fn()  # Default config — NO parameter tuning

    real_result = pipeline.run(symbol, candles, fn_b)
    log_section(real_result.report)

    # ── Step 4: Detailed Alpha B Analysis ──
    log_section("\n[4/6] DETAILED ALPHA B ANALYSIS...")
    trades = real_result.engine_state.trades if real_result.engine_state else []
    analysis = analyze_alpha_b_trades(trades)
    analysis_report = format_alpha_b_analysis(analysis)
    log_section(analysis_report)

    # ── Step 5: Synthetic vs Real comparison ──
    synthetic_result = None
    if run_comparison:
        log_section("\n[5/6] SYNTHETIC vs REAL COMPARISON...")
        synthetic_result = run_synthetic_reference(symbol, n_bars=5000)
        comparison = format_synthetic_vs_real(synthetic_result, real_result)
        log_section(comparison)
    else:
        log_section("\n[5/6] Synthetic comparison SKIPPED")

    # ── Step 6: Final Verdict ──
    log_section("\n[6/6] COMPUTING FINAL VERDICT...")
    verdict = compute_final_verdict(real_result, analysis)
    log_section(verdict)

    full_report = "\n".join(report_lines)

    # Save report to file
    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "reports",
        f"chantier11_alpha_b_real_{symbol}_{days}d.txt",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(full_report)
    logger.info(f"\nReport saved to: {report_path}")

    return full_report


def main():
    parser = argparse.ArgumentParser(
        description="Chantier 11: Alpha B validation on REAL Bitget data"
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--timeframe", default="5m", help="Candle timeframe")
    parser.add_argument("--days", type=int, default=90, help="Days of history")
    parser.add_argument("--no-comparison", action="store_true", help="Skip synthetic comparison")
    args = parser.parse_args()

    report = asyncio.run(run_real_alpha_b(
        symbol=args.symbol,
        timeframe=args.timeframe,
        days=args.days,
        run_comparison=not args.no_comparison,
    ))


if __name__ == "__main__":
    main()
