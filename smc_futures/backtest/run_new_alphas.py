"""
Run New Alphas — Chantier 10: Full Realistic Backtest of 3 Alpha Classes
═══════════════════════════════════════════════════════════════════════════
Runs each alpha individually + combined through the complete pipeline:
  1. Realistic backtest engine (slippage, fees, funding, latency)
  2. Walk-forward validation
  3. Monte Carlo simulation
  4. Edge verdict

Compares against old system (2-bar reversal from Chantier 8).

Usage:
    python -m smc_futures.backtest.run_new_alphas [--bars 5000] [--symbol BTCUSDT]

Verdict: EDGE FOUND or NO EDGE for each alpha + combined.
"""

import sys
import logging
import argparse

from smc_futures.backtest.pipeline import BacktestPipeline, PipelineConfig
from smc_futures.backtest.engine import (
    RealisticBacktestConfig, BTCandle, BacktestSignal,
)
from smc_futures.backtest.walk_forward import WalkForwardConfig
from smc_futures.backtest.monte_carlo import MonteCarloConfig

from smc_futures.backtest.alpha_contextual_reversal import (
    make_contextual_reversal_fn,
    ContextualReversalConfig,
)
from smc_futures.backtest.alpha_momentum_breakout import (
    make_momentum_breakout_fn,
    MomentumBreakoutConfig,
)
from smc_futures.backtest.alpha_liquidity_smc import (
    make_liquidity_smc_fn,
    LiquiditySMCConfig,
)
from smc_futures.backtest.alpha_combiner import (
    make_combined_signal_fn,
    make_reversal_only_fn,
    make_breakout_only_fn,
    make_smc_only_fn,
    CombinerConfig,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


# ─── Synthetic Data Generator ─────────────

def make_synthetic_candles(n=5000, start_price=60000.0, seed=42):
    """
    Generate realistic synthetic BTC 5-min candles.
    Includes: trends, mean-reversion, volume spikes, sweep wicks,
    consolidation zones, and breakouts.
    """
    import random
    rng = random.Random(seed)
    candles = []
    price = start_price
    ts = 1_700_000_000_000
    trend = 0.0
    vol_base = 0.002

    for i in range(n):
        # Regime cycling: every ~200-400 bars, shift regime
        if i % rng.randint(200, 400) == 0:
            trend = rng.gauss(0, 0.0005)
        if i % rng.randint(50, 100) == 0:
            vol_base = max(0.001, min(0.005, vol_base + rng.gauss(0, 0.001)))

        vol = abs(rng.gauss(vol_base, vol_base * 0.4))
        change = trend + rng.gauss(0, vol)

        # Occasional large moves (stop hunts / liquidation cascades)
        if rng.random() < 0.005:  # ~0.5% of bars
            change += rng.choice([-1, 1]) * rng.uniform(0.003, 0.008)

        open_ = price
        close = price * (1 + change)

        # Wick generation
        wick_up = abs(rng.gauss(0, vol * 0.6))
        wick_dn = abs(rng.gauss(0, vol * 0.6))

        # Occasional extended wicks (sweep/hunt candles)
        if rng.random() < 0.02:
            wick_dn *= rng.uniform(2.0, 4.0)
        if rng.random() < 0.02:
            wick_up *= rng.uniform(2.0, 4.0)

        high = max(open_, close) * (1 + wick_up)
        low = min(open_, close) * (1 - wick_dn)

        # Volume: base + surge around big moves
        base_vol = rng.uniform(80, 400)
        if abs(change) > vol * 2:
            base_vol *= rng.uniform(1.5, 3.0)

        candles.append(BTCandle(
            timestamp=ts + i * 300_000,  # 5-min bars
            open=round(open_, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close, 2),
            volume=round(base_vol, 2),
        ))
        price = close

    return candles


# ─── Old System Reference ─────────────────

def old_smc_signal_fn(closed_bars):
    """Old 2-bar reversal (Chantier 8) for comparison."""
    if len(closed_bars) < 15:
        return None

    last4 = closed_bars[-4:]

    bearish_run = all(c.close < c.open for c in last4[:2])
    reversal = last4[2].close > last4[2].open

    if bearish_run and reversal:
        entry = closed_bars[-1].close
        recent_low = min(c.low for c in closed_bars[-6:])
        sl = recent_low * 0.998
        risk = entry - sl
        if risk <= 0 or risk / entry < 0.0005:
            return None
        tp = entry + risk * 2.0
        return BacktestSignal(
            symbol="BTCUSDT", direction="long",
            entry_price=entry, stop_loss=sl, take_profit=tp,
            setup_type="old_reversal_long", confidence=0.6,
        )

    bullish_run = all(c.close > c.open for c in last4[:2])
    reversal_short = last4[2].close < last4[2].open

    if bullish_run and reversal_short:
        entry = closed_bars[-1].close
        recent_high = max(c.high for c in closed_bars[-6:])
        sl = recent_high * 1.002
        risk = sl - entry
        if risk <= 0 or risk / entry < 0.0005:
            return None
        tp = entry - risk * 2.0
        return BacktestSignal(
            symbol="BTCUSDT", direction="short",
            entry_price=entry, stop_loss=sl, take_profit=tp,
            setup_type="old_reversal_short", confidence=0.6,
        )
    return None


# ─── Pipeline Config ──────────────────────

def make_pipeline_config(n_bars: int) -> PipelineConfig:
    """Build pipeline config adapted to data size."""
    # Walk-forward: need enough data for at least 2 folds
    min_wf_data = 1500
    wf_config = None
    if n_bars >= min_wf_data:
        # Proportional windows
        train = max(500, n_bars // 5)
        validate = max(200, n_bars // 10)
        test = max(200, n_bars // 10)
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
        max_holding_bars=288,  # 24h on 5m bars
        run_naive_comparison=False,  # NO naive — realistic only
    )


# ─── Report Formatting ───────────────────

def format_alpha_report(name: str, result) -> str:
    """Format single alpha result."""
    lines = [
        f"\n{'═' * 60}",
        f"  {name}",
        f"{'═' * 60}",
    ]

    if result.metrics:
        m = result.metrics
        lines.extend([
            f"  Trades:       {m.total_trades}",
            f"  Win Rate:     {m.win_rate:.1%}",
            f"  Profit Factor:{m.profit_factor:.2f}",
            f"  Expectancy:   ${m.expectancy:.2f}",
            f"  Net PnL:      ${m.total_net_pnl:.2f}",
            f"  Max DD:       {m.max_drawdown_pct:.1f}%",
            f"  Sharpe:       {m.sharpe_ratio:.2f}",
            f"  Avg Hold:     {m.avg_holding_bars:.0f} bars",
        ])
    else:
        lines.append("  No trades generated")

    if result.walk_forward:
        wf = result.walk_forward
        lines.extend([
            f"  ── Walk-Forward ──",
            f"  Valid Folds:  {wf.valid_folds}/{wf.total_folds}",
            f"  OOS PF:       {wf.oos_profit_factor:.2f}",
            f"  Consistency:  {wf.oos_consistency_ratio:.0%}",
        ])

    if result.monte_carlo:
        mc = result.monte_carlo
        lines.extend([
            f"  ── Monte Carlo ──",
            f"  Robustness:   {mc.robustness_score:.2f}",
            f"  95th DD:      {mc.perm_max_dd_95th:.1f}%",
            f"  P(unprofit):  {mc.param_unprofitable_pct:.0f}%",
        ])

    if result.verdict:
        v = result.verdict
        lines.extend([
            f"  ── VERDICT ──",
            f"  {v.verdict}",
            f"  Score: {v.score:.1f}/100",
        ])

    return "\n".join(lines)


def format_comparison_table(results: dict) -> str:
    """Format comparison table across all alphas."""
    lines = [
        f"\n{'═' * 80}",
        f"  COMPARISON TABLE — CHANTIER 10: NEW ALPHA GENERATION",
        f"{'═' * 80}",
        f"  {'Alpha':<25} {'Trades':>7} {'WR':>7} {'PF':>7} {'Exp$':>8} {'NetPnL':>10} {'DD':>7} {'Verdict':>15}",
        f"  {'─' * 25} {'─' * 7} {'─' * 7} {'─' * 7} {'─' * 8} {'─' * 10} {'─' * 7} {'─' * 15}",
    ]

    for name, result in results.items():
        if result.metrics and result.metrics.total_trades > 0:
            m = result.metrics
            verdict = result.verdict.verdict if result.verdict else "N/A"
            lines.append(
                f"  {name:<25} {m.total_trades:>7} "
                f"{m.win_rate:>6.1%} {m.profit_factor:>7.2f} "
                f"{m.expectancy:>8.2f} {m.total_net_pnl:>10.2f} "
                f"{m.max_drawdown_pct:>6.1f}% {verdict:>15}"
            )
        else:
            lines.append(f"  {name:<25} {'0':>7} {'N/A':>7} {'N/A':>7} {'N/A':>8} {'N/A':>10} {'N/A':>7} {'NO TRADES':>15}")

    lines.append(f"  {'─' * 78}")
    return "\n".join(lines)


# ─── Main Runner ──────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run Chantier 10: New Alpha Generation")
    parser.add_argument("--bars", type=int, default=5000, help="Number of synthetic candles")
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    logger.info("╔══════════════════════════════════════════════════════╗")
    logger.info("║  CHANTIER 10 — NEW ALPHA GENERATION                 ║")
    logger.info("║  3 Alpha Classes × Realistic Pipeline               ║")
    logger.info("║  Verdict: EDGE FOUND or NO EDGE                     ║")
    logger.info("╚══════════════════════════════════════════════════════╝")
    logger.info(f"\nGenerating {args.bars} synthetic {args.symbol} 5m candles (seed={args.seed})...")

    candles = make_synthetic_candles(args.bars, seed=args.seed)
    pipeline_cfg = make_pipeline_config(args.bars)
    results = {}

    # ── 1. Old System (reference) ──
    logger.info("\n── [0/4] OLD SYSTEM (2-bar reversal — reference) ──")
    pipeline = BacktestPipeline(pipeline_cfg)
    results["OLD: 2-bar reversal"] = pipeline.run(args.symbol, candles, old_smc_signal_fn)
    logger.info(format_alpha_report("OLD: 2-bar reversal", results["OLD: 2-bar reversal"]))

    # ── 2. Alpha A: Contextual Reversal ──
    logger.info("\n── [1/4] ALPHA A: Contextual Reversal ──")
    pipeline = BacktestPipeline(pipeline_cfg)
    fn_a = make_contextual_reversal_fn()
    results["A: Ctx Reversal"] = pipeline.run(args.symbol, candles, fn_a)
    logger.info(format_alpha_report("A: Ctx Reversal", results["A: Ctx Reversal"]))

    # ── 3. Alpha B: Momentum Breakout ──
    logger.info("\n── [2/4] ALPHA B: Momentum Breakout ──")
    pipeline = BacktestPipeline(pipeline_cfg)
    fn_b = make_momentum_breakout_fn()
    results["B: Mom Breakout"] = pipeline.run(args.symbol, candles, fn_b)
    logger.info(format_alpha_report("B: Mom Breakout", results["B: Mom Breakout"]))

    # ── 4. Alpha C: Liquidity/SMC ──
    logger.info("\n── [3/4] ALPHA C: Liquidity/SMC Events ──")
    pipeline = BacktestPipeline(pipeline_cfg)
    fn_c = make_liquidity_smc_fn()
    results["C: Liquidity/SMC"] = pipeline.run(args.symbol, candles, fn_c)
    logger.info(format_alpha_report("C: Liquidity/SMC", results["C: Liquidity/SMC"]))

    # ── 5. Combined (all 3) ──
    logger.info("\n── [4/4] COMBINED: All 3 Alphas ──")
    pipeline = BacktestPipeline(pipeline_cfg)
    fn_combined = make_combined_signal_fn()
    results["COMBINED (A+B+C)"] = pipeline.run(args.symbol, candles, fn_combined)
    logger.info(format_alpha_report("COMBINED (A+B+C)", results["COMBINED (A+B+C)"]))

    # ── Comparison Table ──
    table = format_comparison_table(results)
    logger.info(table)

    # ── Final Verdict ──
    logger.info("\n" + "═" * 60)
    logger.info("  VERDICTS FINAUX — CHANTIER 10")
    logger.info("═" * 60)

    any_edge = False
    for name, result in results.items():
        if name.startswith("OLD"):
            continue
        if result.verdict:
            is_edge = "CONFIRMED" in result.verdict.verdict or "PROMISING" in result.verdict.verdict
            symbol = "✓" if is_edge else "✗"
            logger.info(f"  {symbol} {name}: {result.verdict.verdict} (score: {result.verdict.score:.0f}/100)")
            if is_edge:
                any_edge = True
        else:
            logger.info(f"  ✗ {name}: NO VERDICT (insufficient data)")

    logger.info("\n" + "─" * 60)
    if any_edge:
        logger.info("  >>> EDGE FOUND — Au moins un alpha survit les coûts réalistes <<<")
    else:
        logger.info("  >>> NO EDGE — Aucun alpha ne survit les coûts réalistes <<<")
    logger.info("─" * 60)

    return results


if __name__ == "__main__":
    main()
