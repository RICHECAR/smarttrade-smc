"""
Diagnostic: Why do the 3 structural edge alphas produce 0 trades on real data?
Checks each filter stage independently, counts how many bars pass each check.
"""
import sys, os, math, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_futures.backtest.engine import BTCandle
from smc_futures.backtest.real_data_loader import RealDataLoader

from smc_futures.backtest.alpha_trend_following import (
    TrendFollowingConfig, _ema, _atr, _adx, _detect_trend, _detect_pullback,
    _confirm_entry, _compute_sl_tp,
)
from smc_futures.backtest.alpha_volatility_expansion import (
    VolatilityExpansionConfig, _sma, _std_dev, _bollinger_bands, _keltner_channels,
    _atr as _atr_ve, _detect_squeeze, _detect_expansion,
)
from smc_futures.backtest.alpha_extreme_events import (
    ExtremeEventsConfig, _find_extreme_bar, _atr as _atr_ee,
)


async def main():
    print("Loading cached data...")
    loader = RealDataLoader()
    result = await loader.load(symbol="BTCUSDT", timeframe="5m", days=90, force_api=False)
    bars = result.candles
    print(f"Bars: {len(bars)}")
    if len(bars) < 200:
        print("ERROR: Not enough bars")
        return
    
    price_range = (min(b.low for b in bars), max(b.high for b in bars))
    print(f"Price: ${price_range[0]:.0f} - ${price_range[1]:.0f}")
    print(f"Volume sample: {[b.volume for b in bars[:5]]}")
    print(f"Timestamp sample: {[b.timestamp for b in bars[:3]]}")
    print()
    
    # ═══════════════════════════════════════
    # ALPHA 1: TREND FOLLOWING — Filter Analysis
    # ═══════════════════════════════════════
    print("=" * 60)
    print("TREND FOLLOWING — Filter Analysis")
    print("=" * 60)
    
    cfg = TrendFollowingConfig()
    closes = [b.close for b in bars]
    ema_f = _ema(closes, cfg.trend_ema_fast)
    ema_s = _ema(closes, cfg.trend_ema_slow)
    ema_filter = _ema(closes, cfg.trend_ema_filter)
    atr_vals = _atr(bars, cfg.atr_period)
    adx_vals = _adx(bars, cfg.adx_period)
    
    min_bars_tf = max(cfg.trend_ema_filter, cfg.atr_period, cfg.adx_period) + 20
    print(f"Min bars required: {min_bars_tf}")
    print(f"Evaluable bars: {len(bars) - min_bars_tf}")
    
    cnt_atr_ok = 0
    cnt_adx_ok = 0
    cnt_ema_stack = 0
    cnt_ema_sep = 0
    cnt_trend = 0
    cnt_pullback = 0
    cnt_confirm = 0
    cnt_sltp = 0
    
    for i in range(min_bars_tf, len(bars)):
        subset = bars[:i+1]
        atr = atr_vals[i]
        price = bars[i].close
        if math.isnan(atr) or atr <= 0 or price <= 0:
            continue
        
        atr_bps = atr / price * 10000
        if cfg.min_atr_bps <= atr_bps <= cfg.max_atr_bps:
            cnt_atr_ok += 1
        
        adx_val = adx_vals[i]
        if adx_val >= cfg.adx_min_trend:
            cnt_adx_ok += 1
        
        ef, es, efl = ema_f[i], ema_s[i], ema_filter[i]
        if not (math.isnan(ef) or math.isnan(es) or math.isnan(efl)):
            if ef > es > efl or ef < es < efl:
                cnt_ema_stack += 1
                mid = (ef + es) / 2
                if mid > 0:
                    sep_bps = abs(ef - es) / mid * 10000
                    if sep_bps >= cfg.min_trend_strength_bps:
                        cnt_ema_sep += 1
        
        direction = _detect_trend(subset, ema_f[:i+1], ema_s[:i+1], ema_filter[:i+1], adx_vals[:i+1], cfg)
        if direction:
            cnt_trend += 1
            pb = _detect_pullback(subset, ema_f[:i+1], atr_vals[:i+1], direction, cfg)
            if pb is not None:
                cnt_pullback += 1
                if _confirm_entry(subset, atr_vals[:i+1], direction, cfg):
                    cnt_confirm += 1
                    levels = _compute_sl_tp(subset, atr_vals[:i+1], ema_f[:i+1], ema_s[:i+1], direction, cfg)
                    if levels:
                        cnt_sltp += 1
    
    total = len(bars) - min_bars_tf
    print(f"\nFilter funnel ({total} bars evaluated):")
    print(f"  ATR in range:        {cnt_atr_ok:>5} ({cnt_atr_ok/total*100:.1f}%)")
    print(f"  ADX >= {cfg.adx_min_trend}:          {cnt_adx_ok:>5} ({cnt_adx_ok/total*100:.1f}%)")
    print(f"  EMA stack aligned:   {cnt_ema_stack:>5} ({cnt_ema_stack/total*100:.1f}%)")
    print(f"  EMA separation >= {cfg.min_trend_strength_bps}: {cnt_ema_sep:>5} ({cnt_ema_sep/total*100:.1f}%)")
    print(f"  Trend detected:      {cnt_trend:>5} ({cnt_trend/total*100:.1f}%)")
    print(f"  + Pullback found:    {cnt_pullback:>5} ({cnt_pullback/total*100:.1f}%)")
    print(f"  + Entry confirmed:   {cnt_confirm:>5} ({cnt_confirm/total*100:.1f}%)")
    print(f"  + SL/TP valid:       {cnt_sltp:>5} ({cnt_sltp/total*100:.1f}%)")
    
    # Show some ADX statistics
    valid_adx = [v for v in adx_vals if v > 0]
    if valid_adx:
        print(f"\n  ADX stats: min={min(valid_adx):.1f} max={max(valid_adx):.1f} avg={sum(valid_adx)/len(valid_adx):.1f}")
        print(f"  ADX > 20: {sum(1 for v in valid_adx if v > 20)}, ADX > 25: {sum(1 for v in valid_adx if v > 25)}, ADX > 30: {sum(1 for v in valid_adx if v > 30)}")
    
    valid_sep = []
    for i in range(min_bars_tf, len(bars)):
        ef, es = ema_f[i], ema_s[i]
        if not (math.isnan(ef) or math.isnan(es)):
            mid = (ef + es) / 2
            if mid > 0:
                valid_sep.append(abs(ef - es) / mid * 10000)
    if valid_sep:
        print(f"  EMA sep stats: min={min(valid_sep):.1f} max={max(valid_sep):.1f} avg={sum(valid_sep)/len(valid_sep):.1f}")
    
    # ═══════════════════════════════════════
    # ALPHA 2: VOLATILITY EXPANSION — Filter Analysis
    # ═══════════════════════════════════════
    print("\n" + "=" * 60)
    print("VOLATILITY EXPANSION — Filter Analysis")
    print("=" * 60)
    
    vcfg = VolatilityExpansionConfig()
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    
    atr_vals_ve = _atr_ve(bars, vcfg.atr_period)
    bb_upper, bb_mid, bb_lower, bb_width = _bollinger_bands(closes, vcfg.bb_period, vcfg.bb_std)
    kc_upper, kc_mid, kc_lower = _keltner_channels(closes, atr_vals_ve, vcfg.kc_period, vcfg.kc_atr_mult)
    
    min_bars_ve = max(vcfg.bb_period, vcfg.kc_period) + vcfg.squeeze_min_bars + 10
    print(f"Min bars required: {min_bars_ve}")
    
    cnt_squeeze_bar = 0
    cnt_squeeze_run = 0
    cnt_expansion = 0
    
    for i in range(min_bars_ve, len(bars)):
        # Is BB inside KC at this bar?
        if not (math.isnan(bb_upper[i]) or math.isnan(kc_upper[i])):
            if bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i]:
                cnt_squeeze_bar += 1
    
    # Count consecutive squeeze runs of length >= squeeze_min_bars
    run = 0
    for i in range(min_bars_ve, len(bars)):
        if not (math.isnan(bb_upper[i]) or math.isnan(kc_upper[i])):
            if bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i]:
                run += 1
                if run == vcfg.squeeze_min_bars:
                    cnt_squeeze_run += 1
            else:
                run = 0
        else:
            run = 0
    
    # Count expansion triggers
    for i in range(min_bars_ve, len(bars)):
        d = _detect_expansion(bars, kc_upper, kc_lower, atr_vals_ve, vcfg, i)
        if d is not None:
            cnt_expansion += 1
    
    total_ve = len(bars) - min_bars_ve
    print(f"  BB inside KC bars:   {cnt_squeeze_bar:>5} ({cnt_squeeze_bar/total_ve*100:.1f}%)")
    print(f"  Squeeze runs >= {vcfg.squeeze_min_bars}:  {cnt_squeeze_run:>5}")
    print(f"  Expansion triggers:  {cnt_expansion:>5} ({cnt_expansion/total_ve*100:.1f}%)")
    
    # ═══════════════════════════════════════
    # ALPHA 3: EXTREME EVENTS — Filter Analysis
    # ═══════════════════════════════════════
    print("\n" + "=" * 60)
    print("EXTREME EVENTS — Filter Analysis")
    print("=" * 60)
    
    ecfg = ExtremeEventsConfig()
    atr_vals_ee = _atr_ee(bars, ecfg.atr_period)
    
    min_bars_ee = ecfg.atr_period + 20
    cnt_extreme = 0
    
    for i in range(min_bars_ee, len(bars)):
        scan_start = max(0, i - 15)
        result = _find_extreme_bar(bars, atr_vals_ee, ecfg, scan_start, i)
        if result is not None:
            cnt_extreme += 1
    
    total_ee = len(bars) - min_bars_ee
    print(f"  Extreme bars found:  {cnt_extreme:>5} ({cnt_extreme/total_ee*100:.1f}%)")
    
    # Also check component conditions
    cnt_big_move = 0
    cnt_vol_surge = 0
    cnt_both = 0
    for i in range(min_bars_ee, len(bars)):
        atr = atr_vals_ee[i]
        if math.isnan(atr) or atr <= 0:
            continue
        bar = bars[i]
        bar_range = bar.high - bar.low
        body = abs(bar.close - bar.open)
        if bar_range > ecfg.extreme_atr_mult * atr:
            cnt_big_move += 1
        avg_vol = sum(b.volume for b in bars[max(0,i-20):i]) / min(20, i) if i > 0 else 0
        if avg_vol > 0 and bar.volume > ecfg.volume_surge_mult * avg_vol:
            cnt_vol_surge += 1
        if bar_range > ecfg.extreme_atr_mult * atr and (avg_vol > 0 and bar.volume > ecfg.volume_surge_mult * avg_vol):
            cnt_both += 1
    
    print(f"  Range > {ecfg.extreme_atr_mult} ATR:      {cnt_big_move:>5} ({cnt_big_move/total_ee*100:.1f}%)")
    print(f"  Volume > {ecfg.volume_surge_mult}x avg:    {cnt_vol_surge:>5} ({cnt_vol_surge/total_ee*100:.1f}%)")
    print(f"  Both conditions:     {cnt_both:>5} ({cnt_both/total_ee*100:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())
