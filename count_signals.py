"""Quick signal count: run each alpha's signal function across all bars."""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_futures.backtest.real_data_loader import RealDataLoader
from smc_futures.backtest.alpha_trend_following import make_trend_following_fn
from smc_futures.backtest.alpha_volatility_expansion import make_volatility_expansion_fn
from smc_futures.backtest.alpha_extreme_events import make_extreme_events_fn


async def main():
    loader = RealDataLoader()
    result = await loader.load(symbol="BTCUSDT", timeframe="5m", days=90, force_api=False)
    bars = result.candles
    print(f"Bars: {len(bars)}")

    for name, make_fn in [
        ("TREND FOLLOWING", make_trend_following_fn),
        ("VOLATILITY EXPANSION", make_volatility_expansion_fn),
        ("EXTREME EVENTS", make_extreme_events_fn),
    ]:
        fn = make_fn()
        signals = []
        for i in range(50, len(bars)):
            sig = fn(bars[:i+1])
            if sig is not None:
                signals.append((i, sig))
        
        print(f"\n{name}: {len(signals)} signals")
        for idx, sig in signals[:5]:
            tp_dist = abs(sig.take_profit - sig.entry_price)
            sl_dist = abs(sig.entry_price - sig.stop_loss)
            target_bps = tp_dist / sig.entry_price * 10000
            print(f"  Bar {idx}: {sig.direction} @ ${sig.entry_price:.0f}  SL=${sig.stop_loss:.0f}  TP=${sig.take_profit:.0f}  target={target_bps:.0f}bps  RR={sig.risk_reward:.1f}")
        if len(signals) > 5:
            print(f"  ... and {len(signals) - 5} more")


if __name__ == "__main__":
    asyncio.run(main())
