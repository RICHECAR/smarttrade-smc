"""
Count signals for each Chantier 13 alpha on real 4H data.
Quick pre-validation before running the expensive full pipeline.
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(__file__))

from smc_futures.backtest.real_data_loader import RealDataLoader
from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn, HTFTrendConfig
from smc_futures.backtest.alpha_long_only import make_long_only_fn, LongOnlyConfig
from smc_futures.backtest.alpha_funding_arb import make_funding_arb_fn, FundingConfig


def count_signals(fn, bars, label):
    n = 0
    longs = 0
    shorts = 0
    for k in range(50, len(bars)):
        sig = fn(bars[:k])
        if sig is not None:
            n += 1
            if sig.direction == "long":
                longs += 1
            else:
                shorts += 1
    rate = n / max(len(bars) - 50, 1) * 100
    print(f"  {label:45s} | {n:4d} signals ({longs}L / {shorts}S) | rate={rate:.1f}%")
    return n


async def main():
    loader = RealDataLoader()
    days = 90

    # Load real 4H data
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        print(f"\n{'='*70}")
        print(f"  {symbol} — 4H — {days} days")
        print(f"{'='*70}")
        try:
            result = await loader.load(symbol, "4h", days)
            bars = result.candles
            print(f"  Loaded {len(bars)} bars")
        except Exception as e:
            print(f"  SKIP: {e}")
            continue

        if len(bars) < 100:
            print(f"  Not enough bars ({len(bars)}), skipping")
            continue

        # AXE 1: HTF Trend
        fn1 = make_htf_trend_fn(HTFTrendConfig())
        count_signals(fn1, bars, "AXE 1: HTF Trend (default)")

        # AXE 2: Long-Only
        fn2 = make_long_only_fn(LongOnlyConfig())
        count_signals(fn2, bars, "AXE 2: Long-Only Bias")

        # AXE 3a: Funding Counter-Crowd
        fn3a = make_funding_arb_fn(FundingConfig(mode="counter_crowd"))
        count_signals(fn3a, bars, "AXE 3a: Funding Counter-Crowd")

        # AXE 3b: Funding Trend-Aligned
        fn3b = make_funding_arb_fn(FundingConfig(mode="trend_aligned"))
        count_signals(fn3b, bars, "AXE 3b: Funding Trend-Aligned")

    # Also check 1D for BTC
    print(f"\n{'='*70}")
    print(f"  BTCUSDT — 1D — {days} days")
    print(f"{'='*70}")
    try:
        result = await loader.load("BTCUSDT", "1d", days)
        bars = result.candles
        print(f"  Loaded {len(bars)} bars")
        cfg_1d = HTFTrendConfig(
            ema_fast=5, ema_mid=13, ema_slow=34, ema_filter=55,
            min_target_bps=400.0, max_holding_bars=30,
        )
        fn_1d = make_htf_trend_fn(cfg_1d)
        count_signals(fn_1d, bars, "AXE 1: HTF Trend 1D (adjusted)")
    except Exception as e:
        print(f"  SKIP: {e}")


if __name__ == "__main__":
    asyncio.run(main())
