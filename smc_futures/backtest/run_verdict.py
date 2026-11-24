"""
Run Realistic Backtest — Verdict: EDGE CONFIRMED ou EDGE INVALIDATED
═════════════════════════════════════════════════════════════════════
Usage:
    python -m smc_futures.backtest.run_verdict [--bars 5000] [--symbol BTCUSDT]

Generates synthetic data and runs the full pipeline to demonstrate
the framework. For real data, integrate with MySQL via scripts/run_backtest.py.
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
        # Micro-trend with mean reversion
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
    """
    SMC-inspired signal function for demonstration.
    Detects 2-bar reversal patterns with mean-reversion logic.
    """
    if len(closed_bars) < 15:
        return None

    last4 = closed_bars[-4:]

    # Long: 2 bearish bars → bullish bar with higher close
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
            symbol="BTCUSDT",
            direction="long",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="reversal_long",
            confidence=0.6,
        )

    # Short: 2 bullish bars → bearish bar
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
            symbol="BTCUSDT",
            direction="short",
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            setup_type="reversal_short",
            confidence=0.6,
        )

    return None


def main():
    parser = argparse.ArgumentParser(description="Run realistic backtest verdict")
    parser.add_argument("--bars", type=int, default=5000, help="Number of synthetic bars")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol")
    parser.add_argument("--capital", type=float, default=10000, help="Initial capital")
    args = parser.parse_args()

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
            train_bars=1000,
            validate_bars=300,
            test_bars=300,
            step_bars=300,
            min_trades_train=5,
            min_trades_validate=1,
            min_trades_test=1,
        ),
        mc_config=MonteCarloConfig(
            n_permutations=1000,
            n_param_variations=300,
            n_skip_trials=300,
            seed=42,
        ),
        max_holding_bars=288,  # 24h
        run_naive_comparison=True,
    )

    pipeline = BacktestPipeline(config)
    result = pipeline.run(args.symbol, candles, smc_like_signal_fn)

    print("\n" + result.report)
    return 0 if result.verdict and result.verdict.verdict == "EDGE CONFIRMED" else 1


if __name__ == "__main__":
    sys.exit(main())
