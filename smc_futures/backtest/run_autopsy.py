"""
Run Edge Autopsy — Complete Post-Mortem CLI
════════════════════════════════════════════
Usage:
    python -m smc_futures.backtest.run_autopsy [--bars 5000] [--symbol BTCUSDT]

Runs the full pipeline then performs a cold autopsy:
  1. PnL decomposition (waterfall)
  2. Segmentation (10 dimensions)
  3. Survival filtering (10 filters)
  4. False edge detection (7 root causes)
  5. Decision matrix (KILL / SALVAGE / RESEARCH / PROMISING)
  6. Action plan (binary verdict)
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

from smc_futures.backtest.autopsy import (
    PnLDecomposer,
    FalseEdgeDetector,
    DecisionMatrix,
    AutopsyResult,
    format_autopsy_report,
    run_autopsy,
)
from smc_futures.backtest.segmentation import SegmentationEngine
from smc_futures.backtest.survival_filter import SurvivalFilterEngine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def make_synthetic_candles(n=5000, start_price=60000.0):
    """Generate realistic synthetic BTC candles with trend + mean-reversion."""
    import random
    rng = random.Random(42)
    candles = []
    price = start_price
    ts = 1_700_000_000_000

    for i in range(n):
        trend = rng.gauss(0, 0.001)
        vol = abs(rng.gauss(0.002, 0.001))
        change = trend + rng.gauss(0, vol)

        open_ = price
        close = price * (1 + change)
        wick_up = abs(rng.gauss(0, vol * 0.5))
        wick_dn = abs(rng.gauss(0, vol * 0.5))
        high = max(open_, close) * (1 + wick_up)
        low = min(open_, close) * (1 - wick_dn)

        candles.append(BTCandle(
            timestamp=ts + i * 300_000,
            open=round(open_, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close, 2),
            volume=round(rng.uniform(50, 500), 2),
        ))
        price = close

    return candles


def smc_like_signal_fn(closed_bars):
    """SMC-inspired 2-bar reversal signal for demo."""
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
            setup_type="reversal_long", confidence=0.6,
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
            setup_type="reversal_short", confidence=0.6,
        )

    return None


def main():
    parser = argparse.ArgumentParser(description="Run Edge Autopsy")
    parser.add_argument("--bars", type=int, default=5000, help="Synthetic bars")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol")
    parser.add_argument("--capital", type=float, default=10000, help="Initial capital")
    args = parser.parse_args()

    SEP = "═" * 72

    # ── Step 1: Run pipeline ──
    logger.info(f"Generating {args.bars} synthetic candles...")
    candles = make_synthetic_candles(args.bars)

    config = PipelineConfig(
        bt_config=RealisticBacktestConfig(
            initial_capital=args.capital,
            risk_per_trade=0.01,
            leverage=10,
            seed=42,
        ),
        wf_config=WalkForwardConfig(
            train_bars=1000, validate_bars=300, test_bars=300,
            step_bars=300, min_trades_train=5,
            min_trades_validate=1, min_trades_test=1,
        ),
        mc_config=MonteCarloConfig(
            n_permutations=1000, n_param_variations=300,
            n_skip_trials=300, seed=42,
        ),
        max_holding_bars=288,
        run_naive_comparison=True,
    )

    pipeline = BacktestPipeline(config)
    result = pipeline.run(args.symbol, candles, smc_like_signal_fn)

    # Print pipeline report
    print("\n" + result.report)

    trades = result.engine_state.trades if result.engine_state else []
    if not trades:
        logger.info("No trades generated — nothing to autopsy.")
        return 1

    # ── Step 2: Segmentation ──
    logger.info(f"\n[AUTOPSY] Step 2: Segmentation ({len(trades)} trades, 10 dimensions)...")
    seg_result = SegmentationEngine.analyze(trades)
    print("\n" + seg_result.report)

    # ── Step 3: Survival Filtering ──
    logger.info("[AUTOPSY] Step 3: Survival Filtering (10 filters)...")
    filter_result = SurvivalFilterEngine.analyze(trades)
    print("\n" + filter_result.report)

    # ── Step 4: Full Autopsy (PnL decomposition + False Edge + Decision) ──
    logger.info("[AUTOPSY] Step 4: PnL Decomposition + False Edge + Decision Matrix...")
    autopsy = run_autopsy(
        trades=trades,
        realistic_metrics=result.metrics,
        naive_metrics=result.naive_metrics,
        segment_survivors=seg_result.profitable_segments,
        filter_survivors=filter_result.total_survivors,
    )
    print("\n" + autopsy.report)

    # ── Final Truth ──
    print(f"\n{SEP}")
    print("  ABSOLUTE TRUTH — CHANTIER 9 FINAL VERDICT")
    print(SEP)
    verdict = autopsy.decision.verdict
    ca = autopsy.cost_attribution
    fe = autopsy.false_edge

    print(f"  Verdict       : {verdict}")
    print(f"  Ideal Alpha   : ${ca.total_ideal_pnl:+.2f}")
    print(f"  Total Costs   : ${ca.total_cost:.2f}")
    print(f"  Net PnL       : ${ca.total_net_pnl:+.2f}")
    print(f"  Cost/Alpha    : {ca.cost_to_alpha_ratio:.2f}x")
    print(f"  #1 Killer     : {ca.kill_ranking[0][0]} (${ca.kill_ranking[0][1]:.2f})")
    print(f"  Primary Cause : {fe.primary_cause}")
    print(f"  Segments KEEP : {seg_result.profitable_segments}/{seg_result.total_segments}")
    print(f"  Filters Impr  : {filter_result.total_survivors}/{len(filter_result.filters)}")
    if filter_result.combined:
        c = filter_result.combined
        final_exp = "PROFITABLE" if c.expectancy_after > 0 else "STILL NEGATIVE"
        print(f"  Best Combined : exp=${c.expectancy_after:+.2f} → {final_exp}")
    print("")
    print(f"  \"Ne protège pas l'ego. Protège le capital.\"")
    print(SEP)

    return 0 if verdict in ("PROMISING", "RESEARCH") else 1


if __name__ == "__main__":
    sys.exit(main())
