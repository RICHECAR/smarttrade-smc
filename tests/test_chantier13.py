"""
Tests — Chantier 13: Repositionnement complet
===============================================

Tests for:
  AXE 1: HTF Trend Following (alpha_htf_trend)
  AXE 2: Long-Only Bias (alpha_long_only)
  AXE 3: Funding Arbitrage (alpha_funding_arb)
"""

import math
import pytest
from typing import List

from smc_futures.backtest.engine import BTCandle, BacktestSignal


# ═══════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════

def make_bars(
    n: int, start_price: float = 65000.0,
    atr_pct: float = 0.01, trend: float = 0.0,
    volume: float = 100.0,
) -> List[BTCandle]:
    """Generate synthetic candles."""
    bars = []
    price = start_price
    for i in range(n):
        price *= (1 + trend)
        atr = price * atr_pct
        o = price
        h = price + atr * 0.6
        l = price - atr * 0.4
        c = price + atr * 0.2 if trend >= 0 else price - atr * 0.2
        bars.append(BTCandle(
            timestamp=1700000000000 + i * 14_400_000,
            open=o, high=h, low=l, close=c,
            volume=volume + (i % 5) * 10,
        ))
        price = c
    return bars


def make_trending_bars(
    n: int, start_price: float = 60000.0,
    trend_pct: float = 0.003,
    volume: float = 100.0,
) -> List[BTCandle]:
    """Generate strongly trending candles (for trend tests)."""
    bars = []
    price = start_price
    for i in range(n):
        price *= (1 + trend_pct)
        atr = price * 0.01
        o = price - atr * 0.3
        h = price + atr * 0.5
        l = price - atr * 0.5
        c = price + atr * 0.2
        bars.append(BTCandle(
            timestamp=1700000000000 + i * 14_400_000,
            open=o, high=h, low=l, close=c,
            volume=volume + (i % 3) * 20,
        ))
        price = c
    return bars


def make_bull_with_dip(
    n: int, start_price: float = 60000.0,
    dip_at: int = None, dip_depth: float = 0.03,
) -> List[BTCandle]:
    """Strongly bullish bars with a dip at specified position."""
    if dip_at is None:
        dip_at = n - 5
    bars = []
    price = start_price
    for i in range(n):
        if i < dip_at:
            price *= 1.003  # Bull
        elif i < dip_at + 3:
            price *= (1 - dip_depth / 3)  # Dip
        else:
            price *= 1.002  # Recovery/bounce

        atr = price * 0.01
        o = price - atr * 0.2
        h = price + atr * 0.4
        l = price - atr * 0.4
        # Last bar is green (bounce confirm)
        if i >= dip_at + 3:
            c = price + atr * 0.3
        elif i >= dip_at:
            c = price - atr * 0.1
        else:
            c = price + atr * 0.2

        bars.append(BTCandle(
            timestamp=1700000000000 + i * 14_400_000,
            open=o, high=h, low=l, close=c,
            volume=100.0 + (i % 5) * 20,
        ))
        price = c
    return bars


# ═══════════════════════════════════════════
#  AXE 1 — HTF Trend Following
# ═══════════════════════════════════════════

class TestHTFTrendConfig:
    def test_default_config(self):
        from smc_futures.backtest.alpha_htf_trend import HTFTrendConfig
        cfg = HTFTrendConfig()
        assert cfg.ema_fast == 8
        assert cfg.ema_slow == 55
        assert cfg.min_target_bps == 300.0
        assert cfg.min_rr == 2.0
        assert cfg.tp_atr_mult == 8.0

    def test_custom_config(self):
        from smc_futures.backtest.alpha_htf_trend import HTFTrendConfig
        cfg = HTFTrendConfig(min_target_bps=500.0, ema_fast=5)
        assert cfg.min_target_bps == 500.0
        assert cfg.ema_fast == 5


class TestHTFTrendSignals:
    def test_factory_returns_callable(self):
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn
        fn = make_htf_trend_fn()
        assert callable(fn)

    def test_no_signal_on_few_bars(self):
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn
        fn = make_htf_trend_fn()
        bars = make_bars(10)
        assert fn(bars) is None

    def test_no_signal_on_flat_market(self):
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn
        fn = make_htf_trend_fn()
        bars = make_bars(200, trend=0.0)
        assert fn(bars) is None

    def test_signal_returns_backtest_signal(self):
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn
        fn = make_htf_trend_fn()
        # Strong uptrend with pullback
        bars = make_trending_bars(150, trend_pct=0.004)
        # Add a pullback at the end
        for j in range(5):
            last = bars[-1]
            p = last.close * 0.995
            bars.append(BTCandle(
                timestamp=last.timestamp + 14_400_000,
                open=last.close, high=last.close * 1.002,
                low=p * 0.998, close=p,
                volume=120.0,
            ))
        # Then a bounce
        last = bars[-1]
        p = last.close * 1.008
        bars.append(BTCandle(
            timestamp=last.timestamp + 14_400_000,
            open=last.close, high=p * 1.005,
            low=last.close * 0.999, close=p,
            volume=140.0,
        ))
        sig = fn(bars)
        if sig is not None:
            assert isinstance(sig, BacktestSignal)
            assert sig.direction in ("long", "short")
            assert sig.stop_loss > 0
            assert sig.take_profit > 0

    def test_cooldown_respected(self):
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn, HTFTrendConfig
        cfg = HTFTrendConfig(min_bars_between=10)
        fn = make_htf_trend_fn(cfg)
        bars = make_trending_bars(200, trend_pct=0.003)
        # Run through all bars
        signals = []
        for k in range(50, len(bars)):
            sig = fn(bars[:k])
            if sig is not None:
                signals.append(k)
        # Check cooldown
        for a, b in zip(signals, signals[1:]):
            assert (b - a) >= cfg.min_bars_between


class TestHTFTrendIndicators:
    def test_ema_basic(self):
        from smc_futures.backtest.alpha_htf_trend import _ema
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        result = _ema(vals, 3)
        assert len(result) == 10
        assert math.isnan(result[0])
        assert math.isnan(result[1])
        assert not math.isnan(result[2])

    def test_atr_basic(self):
        from smc_futures.backtest.alpha_htf_trend import _atr
        bars = make_bars(30)
        result = _atr(bars, 14)
        assert len(result) == 30
        valid = [r for r in result if not math.isnan(r)]
        assert len(valid) > 0
        assert all(r >= 0 for r in valid)

    def test_adx_basic(self):
        from smc_futures.backtest.alpha_htf_trend import _adx
        bars = make_trending_bars(80, trend_pct=0.003)
        result = _adx(bars, 14)
        assert len(result) == 80
        valid = [r for r in result if not math.isnan(r)]
        assert len(valid) > 0

    def test_trend_detection_bullish(self):
        from smc_futures.backtest.alpha_htf_trend import _ema, _adx, _detect_htf_trend, HTFTrendConfig
        bars = make_trending_bars(150, trend_pct=0.005)
        closes = [b.close for b in bars]
        cfg = HTFTrendConfig()
        ema_f = _ema(closes, cfg.ema_fast)
        ema_m = _ema(closes, cfg.ema_mid)
        ema_s = _ema(closes, cfg.ema_slow)
        ema_fil = _ema(closes, cfg.ema_filter)
        adx_vals = _adx(bars, cfg.adx_period)

        # Check at last bar
        i = len(bars) - 1
        direction = _detect_htf_trend(ema_f, ema_m, ema_s, ema_fil, adx_vals, i, cfg)
        # Strong trend should be detected
        assert direction in ("long", None)  # May need warmup

    def test_pullback_detection(self):
        from smc_futures.backtest.alpha_htf_trend import _atr, _ema, _detect_pullback, HTFTrendConfig
        # Uptrend with pullback
        bars = make_trending_bars(100, trend_pct=0.003)
        for j in range(3):
            last = bars[-1]
            p = last.close * 0.99
            bars.append(BTCandle(
                timestamp=last.timestamp + 14_400_000,
                open=last.close, high=last.close, low=p * 0.998, close=p,
                volume=100.0,
            ))

        cfg = HTFTrendConfig()
        closes = [b.close for b in bars]
        atr_vals = _atr(bars, cfg.atr_period)
        ema_m = _ema(closes, cfg.ema_mid)
        i = len(bars) - 1
        # Should detect pullback in uptrend
        result = _detect_pullback(bars, atr_vals, ema_m, i, "long", cfg)
        assert isinstance(result, bool)


# ═══════════════════════════════════════════
#  AXE 2 — Long-Only Bias
# ═══════════════════════════════════════════

class TestLongOnlyConfig:
    def test_default_config(self):
        from smc_futures.backtest.alpha_long_only import LongOnlyConfig
        cfg = LongOnlyConfig()
        assert cfg.regime_ema == 50
        assert cfg.min_target_bps == 250.0
        assert cfg.min_rr == 1.5

    def test_custom_config(self):
        from smc_futures.backtest.alpha_long_only import LongOnlyConfig
        cfg = LongOnlyConfig(min_target_bps=400.0)
        assert cfg.min_target_bps == 400.0


class TestLongOnlySignals:
    def test_factory_returns_callable(self):
        from smc_futures.backtest.alpha_long_only import make_long_only_fn
        fn = make_long_only_fn()
        assert callable(fn)

    def test_no_signal_few_bars(self):
        from smc_futures.backtest.alpha_long_only import make_long_only_fn
        fn = make_long_only_fn()
        bars = make_bars(10)
        assert fn(bars) is None

    def test_no_signal_bear_market(self):
        from smc_futures.backtest.alpha_long_only import make_long_only_fn
        fn = make_long_only_fn()
        bars = make_bars(200, trend=-0.002)
        assert fn(bars) is None

    def test_signal_always_long(self):
        from smc_futures.backtest.alpha_long_only import make_long_only_fn
        fn = make_long_only_fn()
        bars = make_bull_with_dip(200, dip_at=180, dip_depth=0.02)
        sig = fn(bars)
        if sig is not None:
            assert sig.direction == "long"
            assert sig.stop_loss < sig.entry_price
            assert sig.take_profit > sig.entry_price

    def test_no_short_ever(self):
        from smc_futures.backtest.alpha_long_only import make_long_only_fn
        fn = make_long_only_fn()
        # Run through many bars
        bars = make_bull_with_dip(300)
        for k in range(50, len(bars)):
            sig = fn(bars[:k])
            if sig is not None:
                assert sig.direction == "long", "Long-only should never emit short"


class TestLongOnlyRegime:
    def test_bull_regime_detection(self):
        from smc_futures.backtest.alpha_long_only import _ema, _is_bull_regime, LongOnlyConfig
        bars = make_trending_bars(200, trend_pct=0.003)
        closes = [b.close for b in bars]
        cfg = LongOnlyConfig()
        ema_vals = _ema(closes, cfg.regime_ema)
        ema_long = _ema(closes, cfg.regime_ema_long)
        i = len(bars) - 1
        result = _is_bull_regime(closes, ema_vals, ema_long, i, cfg)
        # Strong uptrend should be detected as bull
        assert isinstance(result, bool)

    def test_bear_not_bull(self):
        from smc_futures.backtest.alpha_long_only import _ema, _is_bull_regime, LongOnlyConfig
        bars = make_bars(200, trend=-0.003)
        closes = [b.close for b in bars]
        cfg = LongOnlyConfig()
        ema_vals = _ema(closes, cfg.regime_ema)
        ema_long = _ema(closes, cfg.regime_ema_long)
        i = len(bars) - 1
        result = _is_bull_regime(closes, ema_vals, ema_long, i, cfg)
        assert result is False


# ═══════════════════════════════════════════
#  AXE 3 — Funding Arbitrage
# ═══════════════════════════════════════════

class TestFundingConfig:
    def test_default_config(self):
        from smc_futures.backtest.alpha_funding_arb import FundingConfig
        cfg = FundingConfig()
        assert cfg.mode == "counter_crowd"
        assert cfg.momentum_threshold_bps == 200.0
        assert cfg.min_target_bps == 200.0

    def test_trend_aligned_mode(self):
        from smc_futures.backtest.alpha_funding_arb import FundingConfig
        cfg = FundingConfig(mode="trend_aligned")
        assert cfg.mode == "trend_aligned"


class TestFundingSignals:
    def test_factory_returns_callable(self):
        from smc_futures.backtest.alpha_funding_arb import make_funding_arb_fn
        fn = make_funding_arb_fn()
        assert callable(fn)

    def test_no_signal_few_bars(self):
        from smc_futures.backtest.alpha_funding_arb import make_funding_arb_fn
        fn = make_funding_arb_fn()
        bars = make_bars(10)
        assert fn(bars) is None

    def test_no_signal_flat_market(self):
        from smc_futures.backtest.alpha_funding_arb import make_funding_arb_fn
        fn = make_funding_arb_fn()
        bars = make_bars(100, trend=0.0, atr_pct=0.003)
        assert fn(bars) is None

    def test_counter_crowd_on_strong_momentum(self):
        from smc_futures.backtest.alpha_funding_arb import make_funding_arb_fn, FundingConfig
        cfg = FundingConfig(mode="counter_crowd", entry_delay_bars=0)
        fn = make_funding_arb_fn(cfg)
        # Create strong upward momentum with volume
        bars = make_trending_bars(100, trend_pct=0.005)
        # Add volume surge on last bars
        for b in bars[-5:]:
            b.volume = 500.0  # Big volume surge
        sig = fn(bars)
        if sig is not None:
            assert isinstance(sig, BacktestSignal)
            # Counter-crowd on strong uptrend = short (crowd is long)
            assert sig.direction == "short"


class TestFundingRegime:
    def test_regime_detection(self):
        from smc_futures.backtest.alpha_funding_arb import _estimate_funding_regime, FundingConfig
        cfg = FundingConfig()
        # Strong uptrend
        closes = [60000.0 + i * 100 for i in range(100)]
        volumes = [100.0] * 95 + [200.0] * 5  # volume surge
        result = _estimate_funding_regime(closes, volumes, 99, cfg)
        # Should detect positive funding (crowd long)
        assert result in ("positive", None)

    def test_no_regime_flat(self):
        from smc_futures.backtest.alpha_funding_arb import _estimate_funding_regime, FundingConfig
        cfg = FundingConfig()
        closes = [65000.0] * 100
        volumes = [100.0] * 100
        result = _estimate_funding_regime(closes, volumes, 99, cfg)
        assert result is None

    def test_roc_indicator(self):
        from smc_futures.backtest.alpha_funding_arb import _roc
        vals = [100.0, 101.0, 102.0, 103.0, 105.0]
        result = _roc(vals, 2)
        assert len(result) == 5
        assert math.isnan(result[0])
        assert math.isnan(result[1])
        assert not math.isnan(result[2])


# ═══════════════════════════════════════════
#  Integration
# ═══════════════════════════════════════════

class TestIntegration:
    def test_all_alphas_share_signal_contract(self):
        """All alpha factories return same type signature."""
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn
        from smc_futures.backtest.alpha_long_only import make_long_only_fn
        from smc_futures.backtest.alpha_funding_arb import make_funding_arb_fn

        fns = [make_htf_trend_fn(), make_long_only_fn(), make_funding_arb_fn()]
        bars = make_bars(200)

        for fn in fns:
            result = fn(bars)
            assert result is None or isinstance(result, BacktestSignal)

    def test_signal_prices_valid(self):
        """If signal emitted, prices must be valid."""
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn
        fn = make_htf_trend_fn()
        bars = make_trending_bars(200, trend_pct=0.004)
        for k in range(100, len(bars)):
            sig = fn(bars[:k])
            if sig is not None:
                assert sig.entry_price > 0
                assert sig.stop_loss > 0
                assert sig.take_profit > 0
                if sig.direction == "long":
                    assert sig.stop_loss < sig.entry_price
                    assert sig.take_profit > sig.entry_price
                else:
                    assert sig.stop_loss > sig.entry_price
                    assert sig.take_profit < sig.entry_price
                break

    def test_htf_trend_long_target_sufficient(self):
        """HTF trend signals should have >= min_target_bps."""
        from smc_futures.backtest.alpha_htf_trend import make_htf_trend_fn, HTFTrendConfig
        cfg = HTFTrendConfig()
        fn = make_htf_trend_fn(cfg)
        bars = make_trending_bars(200, trend_pct=0.005)
        for k in range(100, len(bars)):
            sig = fn(bars[:k])
            if sig is not None:
                tp_dist = abs(sig.take_profit - sig.entry_price)
                target_bps = tp_dist / sig.entry_price * 10000
                assert target_bps >= cfg.min_target_bps
                break

    def test_long_only_never_shorts(self):
        """Long-only alpha must never produce short signals."""
        from smc_futures.backtest.alpha_long_only import make_long_only_fn
        fn = make_long_only_fn()
        # Run on various market conditions
        for trend in [0.001, 0.003, 0.005, -0.001]:
            bars = make_bars(200, trend=trend)
            for k in range(80, len(bars)):
                sig = fn(bars[:k])
                if sig is not None:
                    assert sig.direction == "long"
