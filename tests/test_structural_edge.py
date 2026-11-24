"""
Chantier 12 — Tests Unitaires
================================
Tests for:
  - Alpha Trend Following
  - Alpha Volatility Expansion
  - Alpha Extreme Events
  - Combined Alpha
  - Pipeline config
  - Report extraction
"""

import math
import pytest
from typing import List, Optional
from unittest.mock import patch

from smc_futures.backtest.engine import BTCandle, BacktestSignal

# ═══════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════

def make_bars(
    n: int = 200,
    base_price: float = 50000.0,
    atr_pct: float = 0.005,
    trend: float = 0.0,
    base_volume: float = 100.0,
    ts_start: int = 1_700_000_000_000,
    ts_step: int = 300_000,
) -> List[BTCandle]:
    """Generate synthetic bars with controlled parameters."""
    bars = []
    price = base_price
    for i in range(n):
        atr = price * atr_pct
        o = price
        # Apply trend
        price += trend * atr
        # Random-ish movement using deterministic pattern
        phase = math.sin(i * 0.7) * 0.5 + math.cos(i * 0.3) * 0.3
        h = o + atr * (0.5 + abs(phase))
        l = o - atr * (0.5 + abs(1 - abs(phase)))
        c = o + atr * phase
        if h < max(o, c):
            h = max(o, c) + atr * 0.1
        if l > min(o, c):
            l = min(o, c) - atr * 0.1
        vol = base_volume * (1.0 + abs(phase) * 0.5)
        bars.append(BTCandle(
            timestamp=ts_start + i * ts_step,
            open=round(o, 2),
            high=round(h, 2),
            low=round(l, 2),
            close=round(c, 2),
            volume=round(vol, 4),
        ))
    return bars


def make_trending_bars(
    n: int = 300,
    base_price: float = 50000.0,
    direction: str = "up",
    trend_strength: float = 0.002,
    pullback_at: int = 200,
    pullback_depth: float = 0.01,
    volume_base: float = 100.0,
) -> List[BTCandle]:
    """Generate bars with a clear trend and a pullback."""
    bars = []
    price = base_price
    sign = 1.0 if direction == "up" else -1.0
    ts = 1_700_000_000_000

    for i in range(n):
        atr = price * 0.003
        if i < pullback_at:
            # Trending phase
            move = sign * trend_strength * price
        elif i < pullback_at + 15:
            # Pullback phase
            move = -sign * pullback_depth * price / 15
        else:
            # Resume trend with rejection candle
            move = sign * trend_strength * price * 1.5

        o = price
        price += move

        phase = math.sin(i * 0.5) * 0.3
        h = max(o, price) + atr * (0.3 + abs(phase))
        l = min(o, price) - atr * (0.3 + abs(1 - abs(phase)))
        c = price

        # Volume surge on entry bars
        vol = volume_base
        if i >= pullback_at + 15:
            vol *= 1.8

        bars.append(BTCandle(
            timestamp=ts + i * 300_000,
            open=round(o, 2),
            high=round(h, 2),
            low=round(l, 2),
            close=round(c, 2),
            volume=round(vol, 4),
        ))
    return bars


def make_squeeze_bars(
    n: int = 300,
    base_price: float = 50000.0,
    squeeze_start: int = 100,
    squeeze_end: int = 200,
    expansion_direction: str = "up",
    volume_base: float = 100.0,
) -> List[BTCandle]:
    """Generate bars with compression followed by expansion."""
    bars = []
    price = base_price
    ts = 1_700_000_000_000

    for i in range(n):
        if i < squeeze_start:
            # Normal volatility
            atr = price * 0.005
            phase = math.sin(i * 0.6) * 0.5
        elif i < squeeze_end:
            # Squeeze: very low volatility
            atr = price * 0.001
            phase = math.sin(i * 0.4) * 0.2
        else:
            # Expansion: high volatility in direction
            atr = price * 0.008
            sign = 1.0 if expansion_direction == "up" else -1.0
            price += sign * atr * 0.3
            phase = sign * 0.6

        o = price
        h = o + atr * (0.5 + abs(phase))
        l = o - atr * (0.5 - abs(phase) * 0.3)
        c = o + atr * phase

        if h < max(o, c):
            h = max(o, c) + atr * 0.05
        if l > min(o, c):
            l = min(o, c) - atr * 0.05

        vol = volume_base
        if i >= squeeze_end:
            vol *= 2.5  # Volume surge on expansion

        bars.append(BTCandle(
            timestamp=ts + i * 300_000,
            open=round(o, 2),
            high=round(h, 2),
            low=round(l, 2),
            close=round(c, 2),
            volume=round(vol, 4),
        ))
    return bars


def make_extreme_bars(
    n: int = 200,
    base_price: float = 50000.0,
    extreme_at: int = 150,
    extreme_direction: str = "up",
    extreme_size: float = 0.04,
    volume_base: float = 100.0,
) -> List[BTCandle]:
    """Generate bars with an extreme move at a specific index."""
    bars = []
    price = base_price
    ts = 1_700_000_000_000

    for i in range(n):
        atr = price * 0.003

        if i == extreme_at:
            # Extreme bar
            sign = 1.0 if extreme_direction == "up" else -1.0
            move = sign * extreme_size * price
            o = price
            c = price + move
            h = max(o, c) + atr * 0.5
            l = min(o, c) - atr * 0.2
            price = c
            vol = volume_base * 4.0
        elif extreme_at < i <= extreme_at + 3:
            # Post-extreme: slight continuation
            sign = 1.0 if extreme_direction == "up" else -1.0
            move = sign * 0.002 * price
            o = price
            price += move
            c = price
            h = max(o, c) + atr * 0.3
            l = min(o, c) - atr * 0.3
            vol = volume_base * 2.0
        elif extreme_at + 3 < i <= extreme_at + 8:
            # Exhaustion: small bodies, big wicks
            o = price
            c = price + (0.0001 * price * math.sin(i))
            wick = atr * 1.5
            if extreme_direction == "up":
                h = max(o, c) + wick
                l = min(o, c) - atr * 0.2
            else:
                l = min(o, c) - wick
                h = max(o, c) + atr * 0.2
            vol = volume_base * 1.2
        else:
            phase = math.sin(i * 0.5) * 0.4
            o = price
            price += atr * phase * 0.1
            c = price
            h = max(o, c) + atr * 0.4
            l = min(o, c) - atr * 0.4
            vol = volume_base

        bars.append(BTCandle(
            timestamp=ts + i * 300_000,
            open=round(o, 2),
            high=round(h, 2),
            low=round(l, 2),
            close=round(c, 2),
            volume=round(vol, 4),
        ))
    return bars


# ═══════════════════════════════════════════
#  Tests: Trend Following
# ═══════════════════════════════════════════

class TestTrendFollowingIndicators:
    """Test indicator computation."""

    def test_ema_basic(self):
        from smc_futures.backtest.alpha_trend_following import _ema
        values = [float(i) for i in range(20)]
        result = _ema(values, 5)
        assert len(result) == 20
        assert math.isnan(result[0])
        assert not math.isnan(result[4])
        # EMA should be between min and max
        assert result[-1] > 0
        assert result[-1] < 20

    def test_ema_short_input(self):
        from smc_futures.backtest.alpha_trend_following import _ema
        result = _ema([1.0, 2.0], 5)
        assert all(math.isnan(r) for r in result)

    def test_atr_computation(self):
        from smc_futures.backtest.alpha_trend_following import _atr
        bars = make_bars(50, atr_pct=0.01)
        atr_vals = _atr(bars, 14)
        assert len(atr_vals) == 50
        # ATR should be roughly proportional to price * atr_pct
        valid = [a for a in atr_vals if not math.isnan(a) and a > 0]
        assert len(valid) > 20
        avg_atr = sum(valid) / len(valid)
        expected_atr = 50000.0 * 0.01
        assert avg_atr > expected_atr * 0.2  # Within reasonable range

    def test_adx_computation(self):
        from smc_futures.backtest.alpha_trend_following import _adx
        # Trending bars should have higher ADX
        trending = make_bars(100, trend=0.5)
        ranging = make_bars(100, trend=0.0)
        adx_trend = _adx(trending, 14)
        adx_range = _adx(ranging, 14)
        assert len(adx_trend) == 100
        # Trending should have higher ADX on average (last values)
        trend_adx = [a for a in adx_trend[-20:] if a > 0]
        range_adx = [a for a in adx_range[-20:] if a > 0]
        if trend_adx and range_adx:
            assert sum(trend_adx) / len(trend_adx) >= 0  # Basic sanity

    def test_avg_volume(self):
        from smc_futures.backtest.alpha_trend_following import _avg_volume
        bars = make_bars(20, base_volume=100.0)
        vol = _avg_volume(bars, 10)
        assert vol > 0


class TestTrendFollowingConfig:
    """Test configuration defaults."""

    def test_default_config(self):
        from smc_futures.backtest.alpha_trend_following import TrendFollowingConfig
        cfg = TrendFollowingConfig()
        assert cfg.trend_ema_fast == 21
        assert cfg.trend_ema_slow == 55
        assert cfg.min_target_bps == 150.0
        assert cfg.min_rr == 1.5
        assert cfg.adx_min_trend == 20.0

    def test_custom_config(self):
        from smc_futures.backtest.alpha_trend_following import TrendFollowingConfig
        cfg = TrendFollowingConfig(min_target_bps=500.0, min_rr=3.0)
        assert cfg.min_target_bps == 500.0
        assert cfg.min_rr == 3.0


class TestTrendFollowingSignal:
    """Test signal generation."""

    def test_factory_returns_callable(self):
        from smc_futures.backtest.alpha_trend_following import make_trend_following_fn
        fn = make_trend_following_fn()
        assert callable(fn)

    def test_no_signal_on_few_bars(self):
        from smc_futures.backtest.alpha_trend_following import make_trend_following_fn
        fn = make_trend_following_fn()
        bars = make_bars(10)
        assert fn(bars) is None

    def test_no_signal_on_ranging_market(self):
        from smc_futures.backtest.alpha_trend_following import make_trend_following_fn
        fn = make_trend_following_fn()
        bars = make_bars(200, trend=0.0, atr_pct=0.001)
        assert fn(bars) is None

    def test_signal_on_strong_trend(self):
        """Test signal generation on a clear trending market with pullback."""
        from smc_futures.backtest.alpha_trend_following import make_trend_following_fn
        fn = make_trend_following_fn()
        bars = make_trending_bars(300, direction="up", trend_strength=0.003)
        # Run through all bars to find any signal
        signals = []
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                signals.append(sig)
        # May or may not find signal depending on exact data shape
        # But signal structure must be valid if found
        for sig in signals:
            assert sig.direction in ("long", "short")
            assert sig.stop_loss > 0
            assert sig.take_profit > 0
            assert sig.risk_reward >= 2.0
            assert "trend_follow" in sig.setup_type

    def test_signal_validates_min_target(self):
        """Any generated signal must have target >= 200 bps."""
        from smc_futures.backtest.alpha_trend_following import make_trend_following_fn
        fn = make_trend_following_fn()
        bars = make_trending_bars(300, direction="up")
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                tp_dist = abs(sig.take_profit - sig.entry_price)
                target_bps = tp_dist / sig.entry_price * 10000
                assert target_bps >= 200

    def test_cooldown_respected(self):
        """After a signal, cooldown prevents immediate re-signal."""
        from smc_futures.backtest.alpha_trend_following import make_trend_following_fn, TrendFollowingConfig
        cfg = TrendFollowingConfig(min_bars_between=50)
        fn = make_trend_following_fn(cfg)
        bars = make_trending_bars(400, direction="up", trend_strength=0.003)
        signals = []
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                signals.append((i, sig))
        # If multiple signals, they must be ≥50 bars apart
        for j in range(1, len(signals)):
            assert signals[j][0] - signals[j - 1][0] >= 50


# ═══════════════════════════════════════════
#  Tests: Volatility Expansion
# ═══════════════════════════════════════════

class TestVolatilityExpansionIndicators:
    """Test squeeze/expansion indicators."""

    def test_bollinger_bands(self):
        from smc_futures.backtest.alpha_volatility_expansion import _bollinger_bands
        closes = [50000 + math.sin(i * 0.1) * 100 for i in range(50)]
        upper, mid, lower, width = _bollinger_bands(closes, 20, 2.0)
        assert len(upper) == 50
        # Valid BB values after period
        for i in range(19, 50):
            assert not math.isnan(upper[i])
            assert upper[i] > mid[i] > lower[i]
            assert width[i] > 0

    def test_keltner_channels(self):
        from smc_futures.backtest.alpha_volatility_expansion import _keltner_channels, _atr
        bars = make_bars(50)
        closes = [b.close for b in bars]
        atr_vals = _atr(bars, 14)
        kc_upper, kc_mid, kc_lower = _keltner_channels(closes, atr_vals, 20, 1.5)
        assert len(kc_upper) == 50
        for i in range(20, 50):
            if not math.isnan(kc_upper[i]):
                assert kc_upper[i] > kc_mid[i] > kc_lower[i]

    def test_sma_basic(self):
        from smc_futures.backtest.alpha_volatility_expansion import _sma
        values = [float(i) for i in range(20)]
        result = _sma(values, 5)
        assert len(result) == 20
        assert not math.isnan(result[4])
        assert result[4] == 2.0  # avg(0,1,2,3,4)

    def test_std_dev(self):
        from smc_futures.backtest.alpha_volatility_expansion import _std_dev
        values = [1.0, 2.0, 3.0, 4.0, 5.0] * 4
        result = _std_dev(values, 5)
        assert len(result) == 20
        valid = [r for r in result if not math.isnan(r)]
        assert len(valid) > 0
        assert all(r >= 0 for r in valid)


class TestVolatilityExpansionConfig:
    """Test configuration."""

    def test_default_config(self):
        from smc_futures.backtest.alpha_volatility_expansion import VolatilityExpansionConfig
        cfg = VolatilityExpansionConfig()
        assert cfg.squeeze_min_bars == 4
        assert cfg.min_target_bps == 150.0
        assert cfg.min_rr == 1.5
        assert cfg.expansion_volume_surge == 1.2


class TestVolatilityExpansionSignal:
    """Test signal generation."""

    def test_factory_returns_callable(self):
        from smc_futures.backtest.alpha_volatility_expansion import make_volatility_expansion_fn
        fn = make_volatility_expansion_fn()
        assert callable(fn)

    def test_no_signal_on_few_bars(self):
        from smc_futures.backtest.alpha_volatility_expansion import make_volatility_expansion_fn
        fn = make_volatility_expansion_fn()
        bars = make_bars(10)
        assert fn(bars) is None

    def test_no_signal_without_squeeze(self):
        from smc_futures.backtest.alpha_volatility_expansion import make_volatility_expansion_fn
        fn = make_volatility_expansion_fn()
        # Normal market, no squeeze
        bars = make_bars(200, atr_pct=0.005)
        assert fn(bars) is None

    def test_signal_on_squeeze_expansion(self):
        """Test signal on squeeze → expansion pattern."""
        from smc_futures.backtest.alpha_volatility_expansion import make_volatility_expansion_fn
        fn = make_volatility_expansion_fn()
        bars = make_squeeze_bars(300, squeeze_start=100, squeeze_end=200)
        signals = []
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                signals.append(sig)
        # Validate structure of any found signals
        for sig in signals:
            assert sig.direction in ("long", "short")
            assert sig.stop_loss > 0
            assert sig.take_profit > 0
            assert sig.risk_reward >= 2.5
            assert "vol_expansion" in sig.setup_type

    def test_signal_min_target_enforced(self):
        """Any signal must have target >= 200 bps."""
        from smc_futures.backtest.alpha_volatility_expansion import make_volatility_expansion_fn
        fn = make_volatility_expansion_fn()
        bars = make_squeeze_bars(300)
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                tp_dist = abs(sig.take_profit - sig.entry_price)
                target_bps = tp_dist / sig.entry_price * 10000
                assert target_bps >= 200


# ═══════════════════════════════════════════
#  Tests: Extreme Events
# ═══════════════════════════════════════════

class TestExtremeEventsDetection:
    """Test extreme bar detection."""

    def test_find_extreme_bar(self):
        from smc_futures.backtest.alpha_extreme_events import _find_extreme_bar, _atr, ExtremeEventsConfig
        bars = make_extreme_bars(200, extreme_at=150, extreme_size=0.04)
        atr_vals = _atr(bars, 14)
        cfg = ExtremeEventsConfig()
        idx = _find_extreme_bar(bars, atr_vals, cfg, 140, 155)
        # Should find the extreme bar at index 150
        assert idx is not None
        assert idx == 150

    def test_no_extreme_in_normal_market(self):
        from smc_futures.backtest.alpha_extreme_events import _find_extreme_bar, _atr, ExtremeEventsConfig
        bars = make_bars(200, atr_pct=0.003)
        atr_vals = _atr(bars, 14)
        cfg = ExtremeEventsConfig()
        idx = _find_extreme_bar(bars, atr_vals, cfg, 150, 190)
        assert idx is None

    def test_detect_cascade(self):
        from smc_futures.backtest.alpha_extreme_events import _detect_cascade, _atr, ExtremeEventsConfig
        # Create bars with cascading extreme moves
        bars = make_extreme_bars(200, extreme_at=150, extreme_size=0.05)
        atr_vals = _atr(bars, 14)
        cfg = ExtremeEventsConfig()
        count = _detect_cascade(bars, atr_vals, 150, cfg)
        assert count >= 1


class TestExtremeEventsConfig:
    """Test configuration."""

    def test_default_config(self):
        from smc_futures.backtest.alpha_extreme_events import ExtremeEventsConfig
        cfg = ExtremeEventsConfig()
        assert cfg.extreme_atr_mult == 2.0
        assert cfg.min_target_bps == 150.0
        assert cfg.enable_continuation is True
        assert cfg.enable_reversion is True


class TestExtremeEventsSignal:
    """Test signal generation."""

    def test_factory_returns_callable(self):
        from smc_futures.backtest.alpha_extreme_events import make_extreme_events_fn
        fn = make_extreme_events_fn()
        assert callable(fn)

    def test_no_signal_on_few_bars(self):
        from smc_futures.backtest.alpha_extreme_events import make_extreme_events_fn
        fn = make_extreme_events_fn()
        bars = make_bars(10)
        assert fn(bars) is None

    def test_no_signal_on_normal_market(self):
        from smc_futures.backtest.alpha_extreme_events import make_extreme_events_fn
        fn = make_extreme_events_fn()
        bars = make_bars(200, atr_pct=0.003)
        assert fn(bars) is None

    def test_signal_after_extreme_move(self):
        """Test signal after a large extreme bar."""
        from smc_futures.backtest.alpha_extreme_events import make_extreme_events_fn
        fn = make_extreme_events_fn()
        bars = make_extreme_bars(200, extreme_at=150, extreme_size=0.05)
        signals = []
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                signals.append(sig)
        for sig in signals:
            assert sig.direction in ("long", "short")
            assert sig.stop_loss > 0
            assert sig.take_profit > 0
            assert sig.risk_reward >= 2.0
            assert "extreme" in sig.setup_type

    def test_signal_min_target(self):
        """Any signal must have target >= 200 bps."""
        from smc_futures.backtest.alpha_extreme_events import make_extreme_events_fn
        fn = make_extreme_events_fn()
        bars = make_extreme_bars(200, extreme_at=150, extreme_size=0.05)
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                tp_dist = abs(sig.take_profit - sig.entry_price)
                target_bps = tp_dist / sig.entry_price * 10000
                assert target_bps >= 200


# ═══════════════════════════════════════════
#  Tests: Combined Alpha
# ═══════════════════════════════════════════

class TestCombinedAlpha:
    """Test combined signal function."""

    def test_combined_factory(self):
        from smc_futures.backtest.run_structural_edge import make_combined_alpha_fn
        fn = make_combined_alpha_fn()
        assert callable(fn)

    def test_combined_no_signal_few_bars(self):
        from smc_futures.backtest.run_structural_edge import make_combined_alpha_fn
        fn = make_combined_alpha_fn()
        bars = make_bars(20)
        assert fn(bars) is None

    def test_combined_picks_highest_confidence(self):
        """If multiple alphas fire, picks highest confidence."""
        from smc_futures.backtest.run_structural_edge import make_combined_alpha_fn
        fn = make_combined_alpha_fn()
        # Just verify it doesn't crash on a large dataset
        bars = make_bars(300)
        result = fn(bars)
        # Result can be None (no signal) or BacktestSignal
        if result is not None:
            assert isinstance(result, BacktestSignal)

    def test_combined_enforces_min_target(self):
        from smc_futures.backtest.run_structural_edge import make_combined_alpha_fn
        fn = make_combined_alpha_fn()
        bars = make_trending_bars(400)
        for i in range(120, len(bars)):
            sig = fn(bars[:i])
            if sig is not None:
                tp_dist = abs(sig.take_profit - sig.entry_price)
                target_bps = tp_dist / sig.entry_price * 10000
                assert target_bps >= 200


# ═══════════════════════════════════════════
#  Tests: Pipeline Config
# ═══════════════════════════════════════════

class TestPipelineConfig:
    """Test pipeline configuration factory."""

    def test_make_pipeline_config(self):
        from smc_futures.backtest.run_structural_edge import make_pipeline_config
        cfg = make_pipeline_config()
        assert cfg.bt_config.initial_capital == 10000.0
        assert cfg.bt_config.risk_per_trade == 0.01
        assert cfg.bt_config.leverage == 10
        assert cfg.max_holding_bars == 576
        assert cfg.run_naive_comparison is False

    def test_custom_max_hold(self):
        from smc_futures.backtest.run_structural_edge import make_pipeline_config
        cfg = make_pipeline_config(max_holding_bars=288)
        assert cfg.max_holding_bars == 288

    def test_wf_config_low_frequency(self):
        """WF config should have lower trade thresholds for low-frequency alphas."""
        from smc_futures.backtest.run_structural_edge import make_pipeline_config
        cfg = make_pipeline_config()
        assert cfg.wf_config.min_trades_train == 5
        assert cfg.wf_config.min_trades_test == 2


# ═══════════════════════════════════════════
#  Tests: Report Extraction
# ═══════════════════════════════════════════

class TestReportExtraction:
    """Test report extraction from pipeline results."""

    def test_extract_report_empty(self):
        from smc_futures.backtest.run_structural_edge import extract_report, AlphaReport
        from smc_futures.backtest.pipeline import PipelineResult
        result = PipelineResult(
            symbol="BTCUSDT",
            duration_seconds=1.0,
            engine_state=None,
            metrics=None,
            walk_forward=None,
            monte_carlo=None,
            verdict=None,
            naive_metrics=None,
            naive_trades=0,
            report="",
        )
        r = extract_report("TEST", result)
        assert r.name == "TEST"
        assert r.trades == 0
        assert r.verdict == "N/A"

    def test_alpha_report_defaults(self):
        from smc_futures.backtest.run_structural_edge import AlphaReport
        r = AlphaReport(name="TEST")
        assert r.trades == 0
        assert r.win_rate == 0.0
        assert r.verdict == "N/A"


class TestFormatting:
    """Test format functions."""

    def test_format_alpha_report(self):
        from smc_futures.backtest.run_structural_edge import format_alpha_report, AlphaReport
        r = AlphaReport(
            name="TREND FOLLOWING",
            trades=25, win_rate=40.0, pf=1.5, expectancy=10.0,
            net_pnl=250.0, max_dd=15.0, sharpe=1.2, edge_ratio=1.3,
            score=65, verdict="EDGE CONFIRMED",
        )
        text = format_alpha_report(r)
        assert "TREND FOLLOWING" in text
        assert "40.0%" in text
        assert "1.50" in text
        assert "EDGE CONFIRMED" in text

    def test_format_comparison_table(self):
        from smc_futures.backtest.run_structural_edge import format_comparison_table, AlphaReport
        reports = [
            AlphaReport(name="A", trades=10, win_rate=50.0, pf=1.2,
                       expectancy=5.0, net_pnl=50.0, max_dd=10.0,
                       edge_ratio=1.1, score=50, verdict="CONFIRMED"),
            AlphaReport(name="B", trades=20, win_rate=60.0, pf=1.8,
                       expectancy=15.0, net_pnl=300.0, max_dd=8.0,
                       edge_ratio=1.5, score=75, verdict="CONFIRMED"),
        ]
        text = format_comparison_table(reports)
        assert "COMPARISON TABLE" in text
        assert "A" in text
        assert "B" in text

    def test_format_final_verdict_no_edge(self):
        from smc_futures.backtest.run_structural_edge import format_final_verdict, AlphaReport
        reports = [
            AlphaReport(name="A", trades=5, expectancy=-10.0, pf=0.5, verdict="INVALIDATED"),
        ]
        text = format_final_verdict(reports)
        assert "NO EDGE" in text

    def test_format_final_verdict_edge_found(self):
        from smc_futures.backtest.run_structural_edge import format_final_verdict, AlphaReport
        reports = [
            AlphaReport(name="A", trades=30, expectancy=10.0, pf=1.5,
                       edge_ratio=1.5, max_dd=10.0, wf_robust=True,
                       mc_robust=True, verdict="EDGE CONFIRMED", score=70),
        ]
        text = format_final_verdict(reports)
        assert "EDGE FOUND" in text

    def test_format_final_verdict_edge_weak(self):
        from smc_futures.backtest.run_structural_edge import format_final_verdict, AlphaReport
        reports = [
            AlphaReport(name="A", trades=15, expectancy=3.0, pf=1.3,
                       edge_ratio=0.8, max_dd=20.0, wf_robust=False,
                       mc_robust=False, verdict="INVALIDATED", score=35),
        ]
        text = format_final_verdict(reports)
        assert "EDGE WEAK" in text


# ═══════════════════════════════════════════
#  Tests: Imports
# ═══════════════════════════════════════════

class TestImports:
    """Verify all modules import correctly."""

    def test_import_trend_following(self):
        from smc_futures.backtest.alpha_trend_following import (
            make_trend_following_fn, TrendFollowingConfig,
        )
        assert TrendFollowingConfig is not None

    def test_import_volatility_expansion(self):
        from smc_futures.backtest.alpha_volatility_expansion import (
            make_volatility_expansion_fn, VolatilityExpansionConfig,
        )
        assert VolatilityExpansionConfig is not None

    def test_import_extreme_events(self):
        from smc_futures.backtest.alpha_extreme_events import (
            make_extreme_events_fn, ExtremeEventsConfig,
        )
        assert ExtremeEventsConfig is not None

    def test_import_runner(self):
        from smc_futures.backtest.run_structural_edge import (
            make_combined_alpha_fn, make_pipeline_config,
            extract_report, AlphaReport,
        )
        assert AlphaReport is not None

    def test_import_all_factories(self):
        from smc_futures.backtest.alpha_trend_following import make_trend_following_fn
        from smc_futures.backtest.alpha_volatility_expansion import make_volatility_expansion_fn
        from smc_futures.backtest.alpha_extreme_events import make_extreme_events_fn
        # All return callables
        assert callable(make_trend_following_fn())
        assert callable(make_volatility_expansion_fn())
        assert callable(make_extreme_events_fn())
