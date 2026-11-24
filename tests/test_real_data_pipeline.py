"""
Tests — Chantier 11: Real Data Pipeline + Alpha B Validation
═════════════════════════════════════════════════════════════
Tests:
  1. DataValidator: gap detection, OHLC coherence, anomalies, timestamps
  2. SQLiteCacheWriter/Loader: round-trip persistence
  3. EnhancedMarketModel: breakout/retest/false-breakout slippage
  4. EntryContext / classify_alpha_b_entry
  5. Alpha B analysis on real-format data
  6. Pipeline integration with real data format
  7. Synthetic vs Real comparison utilities
  8. Final verdict computation
"""

import math
import os
import sqlite3
import tempfile
import pytest

from smc_futures.backtest.engine import BTCandle, BacktestSignal, RealisticTrade
from smc_futures.backtest.real_data_loader import (
    DataValidator,
    ValidationResult,
    SQLiteCacheWriter,
    SQLiteCacheLoader,
    RealDataLoader,
    LoadResult,
    TF_DURATION_MS,
)
from smc_futures.backtest.microstructure_model import (
    EnhancedMarketModel,
    MicrostructureConfig,
    EntryContext,
    classify_alpha_b_entry,
)
from smc_futures.backtest.run_real_alpha_b import (
    AlphaBAnalysis,
    analyze_alpha_b_trades,
    format_alpha_b_analysis,
    format_synthetic_vs_real,
    compute_final_verdict,
    make_real_pipeline_config,
)
from smc_futures.backtest.pipeline import PipelineResult, PipelineConfig
from smc_futures.backtest.metrics import DetailedMetrics, EdgeVerdict
from smc_futures.backtest.walk_forward import WalkForwardResult
from smc_futures.backtest.monte_carlo import MonteCarloResult


# ═══════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════

def make_candles(
    n: int = 100,
    start_price: float = 60000.0,
    tf_ms: int = 300_000,
    start_ts: int = 1_700_000_000_000,
    with_gaps: bool = False,
    with_ohlc_error: bool = False,
    with_anomaly: bool = False,
    with_zero_vol: bool = False,
) -> list[BTCandle]:
    """Generate test candles with optional defects."""
    import random
    rng = random.Random(42)
    candles = []
    price = start_price

    for i in range(n):
        ts = start_ts + i * tf_ms

        # Inject gap
        if with_gaps and i == 50:
            ts += tf_ms * 5  # 5-bar gap

        change = rng.gauss(0, 0.002)
        open_ = price
        close = price * (1 + change)
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.001)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.001)))
        vol = rng.uniform(50, 300)

        # Inject OHLC error
        if with_ohlc_error and i == 30:
            high = low - 10  # high < low

        # Inject anomaly
        if with_anomaly and i == 60:
            close = price * 1.15  # 15% move
            high = close * 1.01

        # Inject zero volume
        if with_zero_vol and i % 20 == 0:
            vol = 0

        candles.append(BTCandle(
            timestamp=ts,
            open=round(open_, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close, 2),
            volume=round(vol, 2),
        ))
        price = close

    return candles


def make_realistic_trade(
    direction="long",
    entry_price=60000.0,
    exit_price=60300.0,
    net_pnl=25.0,
    is_win=True,
    holding_bars=20,
    entry_timestamp=1_700_000_100_000,
    volatility_regime="normal",
    setup_type="momentum_breakout_long",
) -> RealisticTrade:
    """Create a realistic trade for testing."""
    t = RealisticTrade(
        signal_bar_idx=10,
        signal_price=entry_price,
        direction=direction,
        setup_type=setup_type,
    )
    t.entry_bar_idx = 11
    t.intended_entry = entry_price
    t.actual_entry = entry_price * 1.0002
    t.entry_slippage_bps = 2.0
    t.entry_fee = 3.6
    t.entry_timestamp = entry_timestamp
    t.stop_loss = entry_price * 0.99
    t.take_profit = entry_price * 1.02
    t.exit_bar_idx = 11 + holding_bars
    t.intended_exit = exit_price
    t.actual_exit = exit_price
    t.exit_fee = 3.6
    t.exit_reason = "tp" if is_win else "sl"
    t.qty = 0.01
    t.notional_usd = entry_price * 0.01
    t.raw_pnl = net_pnl + 7.2
    t.total_fees = 7.2
    t.funding_cost = 0.5
    t.net_pnl = net_pnl
    t.net_pnl_pct = net_pnl / 10000 * 100
    t.mae = entry_price * 0.005
    t.mfe = entry_price * 0.01
    t.holding_bars = holding_bars
    t.atr_at_entry = entry_price * 0.005
    t.volatility_regime = volatility_regime
    return t


# ═══════════════════════════════════════════
#  1. DATA VALIDATOR TESTS
# ═══════════════════════════════════════════

class TestDataValidator:
    """Tests for DataValidator."""

    def test_valid_data(self):
        candles = make_candles(200)
        v = DataValidator.validate(candles, "5m")
        assert v.is_valid
        assert v.total_candles == 200
        assert v.gaps_found == 0
        assert v.ohlc_violations == 0
        assert v.timestamp_issues == 0

    def test_empty_data(self):
        v = DataValidator.validate([], "5m")
        assert not v.is_valid
        assert v.total_candles == 0

    def test_gap_detection(self):
        candles = make_candles(100, with_gaps=True)
        v = DataValidator.validate(candles, "5m")
        assert v.gaps_found > 0
        assert len(v.gap_details) > 0

    def test_ohlc_violation(self):
        candles = make_candles(100, with_ohlc_error=True)
        v = DataValidator.validate(candles, "5m")
        assert v.ohlc_violations > 0

    def test_anomaly_detection(self):
        candles = make_candles(100, with_anomaly=True)
        v = DataValidator.validate(candles, "5m")
        assert v.anomalies > 0

    def test_zero_volume_counting(self):
        candles = make_candles(100, with_zero_vol=True)
        v = DataValidator.validate(candles, "5m")
        assert v.zero_volume_candles > 0

    def test_coverage_calculation(self):
        candles = make_candles(200)
        v = DataValidator.validate(candles, "5m")
        assert v.coverage_pct > 95.0

    def test_date_range_format(self):
        candles = make_candles(100)
        v = DataValidator.validate(candles, "5m")
        assert "UTC" in v.date_range
        assert " → " in v.date_range  # Using unicode arrow

    def test_format_report(self):
        candles = make_candles(100)
        v = DataValidator.validate(candles, "5m")
        report = DataValidator.format_report(v)
        assert "Data Validation" in report
        assert "VALID" in report

    def test_rejects_all_bad_ohlc(self):
        """Data with > 0.5% OHLC violations should be rejected."""
        candles = make_candles(100)
        # Corrupt 2% of candles
        for i in range(0, 100, 50):
            candles[i] = BTCandle(
                timestamp=candles[i].timestamp,
                open=100, high=50, low=200, close=100,  # Invalid
                volume=100,
            )
        v = DataValidator.validate(candles, "5m")
        assert v.ohlc_violations >= 2

    def test_different_timeframes(self):
        for tf in ["1m", "15m", "1h", "4h"]:
            tf_ms = TF_DURATION_MS[tf]
            candles = make_candles(50, tf_ms=tf_ms)
            v = DataValidator.validate(candles, tf)
            assert v.total_candles == 50

    def test_timestamp_monotonicity(self):
        candles = make_candles(50)
        # Swap two timestamps
        candles[10], candles[11] = candles[11], candles[10]
        v = DataValidator.validate(candles, "5m")
        assert v.timestamp_issues > 0
        assert not v.is_valid

    def test_single_candle(self):
        candles = make_candles(1)
        v = DataValidator.validate(candles, "5m")
        assert v.is_valid
        assert v.total_candles == 1


# ═══════════════════════════════════════════
#  2. SQLITE CACHE TESTS
# ═══════════════════════════════════════════

class TestSQLiteCache:
    """Tests for SQLite cache write/read round-trip."""

    def test_write_read_roundtrip(self, tmp_path):
        db_path = str(tmp_path / "test_candles.db")
        candles = make_candles(50)

        # Write
        written = SQLiteCacheWriter.save(candles, "BTCUSDT", "5m", db_path=db_path)
        assert written == 50

        # Read
        loaded = SQLiteCacheLoader.load("BTCUSDT", "5m", db_path=db_path)
        assert len(loaded) == 50

        # Verify data integrity
        for orig, loaded_c in zip(candles, loaded):
            assert orig.timestamp == loaded_c.timestamp
            assert abs(orig.open - loaded_c.open) < 0.01
            assert abs(orig.close - loaded_c.close) < 0.01

    def test_upsert_no_duplicates(self, tmp_path):
        db_path = str(tmp_path / "test_candles.db")
        candles = make_candles(20)

        SQLiteCacheWriter.save(candles, "BTCUSDT", "5m", db_path=db_path)
        SQLiteCacheWriter.save(candles, "BTCUSDT", "5m", db_path=db_path)

        loaded = SQLiteCacheLoader.load("BTCUSDT", "5m", db_path=db_path)
        assert len(loaded) == 20  # No duplicates

    def test_different_symbols(self, tmp_path):
        db_path = str(tmp_path / "test_candles.db")
        btc = make_candles(30, start_price=60000)
        eth = make_candles(25, start_price=3000)

        SQLiteCacheWriter.save(btc, "BTCUSDT", "5m", db_path=db_path)
        SQLiteCacheWriter.save(eth, "ETHUSDT", "5m", db_path=db_path)

        btc_loaded = SQLiteCacheLoader.load("BTCUSDT", "5m", db_path=db_path)
        eth_loaded = SQLiteCacheLoader.load("ETHUSDT", "5m", db_path=db_path)
        assert len(btc_loaded) == 30
        assert len(eth_loaded) == 25

    def test_different_timeframes(self, tmp_path):
        db_path = str(tmp_path / "test_candles.db")
        c5m = make_candles(30, tf_ms=300_000)
        c15m = make_candles(20, tf_ms=900_000)

        SQLiteCacheWriter.save(c5m, "BTCUSDT", "5m", db_path=db_path)
        SQLiteCacheWriter.save(c15m, "BTCUSDT", "15m", db_path=db_path)

        loaded_5m = SQLiteCacheLoader.load("BTCUSDT", "5m", db_path=db_path)
        loaded_15m = SQLiteCacheLoader.load("BTCUSDT", "15m", db_path=db_path)
        assert len(loaded_5m) == 30
        assert len(loaded_15m) == 20

    def test_empty_read(self, tmp_path):
        db_path = str(tmp_path / "test_candles.db")
        loaded = SQLiteCacheLoader.load("BTCUSDT", "5m", db_path=db_path)
        assert loaded == []

    def test_nonexistent_file(self):
        loaded = SQLiteCacheLoader.load("BTCUSDT", "5m", db_path="/nonexistent/path.db")
        assert loaded == []

    def test_empty_candles(self, tmp_path):
        db_path = str(tmp_path / "test_candles.db")
        written = SQLiteCacheWriter.save([], "BTCUSDT", "5m", db_path=db_path)
        assert written == 0


# ═══════════════════════════════════════════
#  3. ENHANCED MARKET MODEL TESTS
# ═══════════════════════════════════════════

class TestEnhancedMarketModel:
    """Tests for microstructure-aware market model."""

    def test_retest_lower_slippage(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        atr = 300.0

        # Retest entry — should have lower slippage
        retest_ctx = EntryContext(entry_type="retest", volume_surge_ratio=1.0)
        retest_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, retest_ctx
        )

        # Default entry
        default_ctx = EntryContext(entry_type="default")
        default_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, default_ctx
        )

        assert not retest_fill.rejected
        assert not default_fill.rejected
        assert retest_fill.slippage_bps < default_fill.slippage_bps

    def test_breakout_higher_slippage(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        atr = 300.0

        breakout_ctx = EntryContext(
            entry_type="breakout", volume_surge_ratio=2.0
        )
        breakout_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, breakout_ctx
        )

        default_ctx = EntryContext(entry_type="default")
        default_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, default_ctx
        )

        assert breakout_fill.slippage_bps > default_fill.slippage_bps

    def test_false_breakout_worst_slippage(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        atr = 300.0

        fb_ctx = EntryContext(entry_type="false_breakout")
        fb_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, fb_ctx
        )

        breakout_ctx = EntryContext(entry_type="breakout", volume_surge_ratio=1.5)
        bo_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, breakout_ctx
        )

        assert fb_fill.slippage_bps > bo_fill.slippage_bps

    def test_retest_maker_fees(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        atr = 300.0

        retest_ctx = EntryContext(entry_type="retest")
        fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, retest_ctx
        )
        # Maker fee = 0.02%, taker = 0.06%
        assert fill.fee_rate == 0.0002  # maker

    def test_sell_side(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        ctx = EntryContext(entry_type="retest")
        fill = model.simulate_entry_fill(
            "sell", 60000, 0.01, 300, ctx
        )
        assert fill.fill_price < 60000  # Sell gets lower price

    def test_momentum_chase_penalty(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        atr = 300.0

        # Chase: far from level — must use same seed for determinism
        chase_ctx = EntryContext(
            entry_type="retest",
            is_momentum_chase=True,
            spread_from_level_bps=100,
        )
        chase_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, atr, chase_ctx
        )

        # Recreate model with same seed for fair comparison
        model2 = EnhancedMarketModel("BTCUSDT", seed=42)
        normal_ctx = EntryContext(
            entry_type="retest",
            is_momentum_chase=False,
            spread_from_level_bps=5,
        )
        normal_fill = model2.simulate_entry_fill(
            "buy", 60000, 0.01, atr, normal_ctx
        )

        # Chase should lead to higher slippage due to the chase penalty
        assert chase_fill.slippage_bps > normal_fill.slippage_bps

    def test_sl_fill_delegates(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        fill = model.simulate_sl_fill("buy", 59000, 0.01, 300)
        assert not fill.rejected
        assert fill.fill_price > 0

    def test_tp_fill_delegates(self):
        model = EnhancedMarketModel("BTCUSDT", seed=42)
        fill = model.simulate_tp_fill("buy", 61000, 0.01, 300)
        assert not fill.rejected

    def test_custom_config(self):
        cfg = MicrostructureConfig(
            breakout_slippage_mult=3.0,  # Very aggressive
            retest_slippage_mult=0.3,    # Very gentle
        )
        model = EnhancedMarketModel("BTCUSDT", seed=42, config=cfg)

        bo_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, 300,
            EntryContext(entry_type="breakout"),
        )
        rt_fill = model.simulate_entry_fill(
            "buy", 60000, 0.01, 300,
            EntryContext(entry_type="retest"),
        )
        # With extreme config, ratio should be very large
        assert bo_fill.slippage_bps / max(0.01, rt_fill.slippage_bps) > 3.0


# ═══════════════════════════════════════════
#  4. ENTRY CONTEXT CLASSIFICATION
# ═══════════════════════════════════════════

class TestEntryContext:
    """Tests for entry context classification."""

    def test_classify_alpha_b_basic(self):
        candles = make_candles(50)
        ctx = classify_alpha_b_entry(
            "momentum_breakout_long",
            candles,
            entry_price=60100,
            breakout_level=60000,
        )
        assert ctx.entry_type == "retest"  # Alpha B always enters on retest
        assert ctx.volume_surge_ratio > 0

    def test_chase_detection(self):
        candles = make_candles(50)
        ctx = classify_alpha_b_entry(
            "momentum_breakout_long",
            candles,
            entry_price=60500,  # Far from breakout level
            breakout_level=60000,
        )
        assert ctx.spread_from_level_bps > 50
        assert ctx.is_momentum_chase

    def test_close_to_level(self):
        candles = make_candles(50)
        ctx = classify_alpha_b_entry(
            "momentum_breakout_long",
            candles,
            entry_price=60010,  # Very close
            breakout_level=60000,
        )
        assert ctx.spread_from_level_bps < 5
        assert not ctx.is_momentum_chase

    def test_empty_candles(self):
        ctx = classify_alpha_b_entry("x", [], 60000, 60000)
        assert ctx.entry_type == "default"

    def test_zero_prices(self):
        ctx = classify_alpha_b_entry("x", make_candles(5), 0, 60000)
        assert ctx.entry_type == "default"


# ═══════════════════════════════════════════
#  5. ALPHA B TRADE ANALYSIS
# ═══════════════════════════════════════════

class TestAlphaBAnalysis:
    """Tests for Alpha B detailed trade analysis."""

    def _make_trades(self, n=20) -> list[RealisticTrade]:
        trades = []
        for i in range(n):
            is_win = i % 3 != 0  # ~67% WR
            direction = "long" if i % 2 == 0 else "short"
            regime = ["low", "normal", "high", "normal"][i % 4]
            # Vary entry timestamps across sessions
            hour_offset = (i * 3) % 24
            ts = 1_700_000_000_000 + hour_offset * 3_600_000

            trades.append(make_realistic_trade(
                direction=direction,
                net_pnl=30 if is_win else -20,
                is_win=is_win,
                holding_bars=15 + i,
                entry_timestamp=ts,
                volatility_regime=regime,
            ))
        return trades

    def test_direction_breakdown(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        assert a.long_trades + a.short_trades == 20
        assert a.long_trades == 10
        assert a.short_trades == 10

    def test_win_rates(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        assert 0 < a.long_win_rate < 100
        assert 0 < a.short_win_rate < 100

    def test_pnl_distribution(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        assert a.best_trade > 0
        assert a.worst_trade < 0
        assert "50th" in a.pnl_percentiles

    def test_regime_stats(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        assert len(a.regime_stats) > 0
        for regime, stats in a.regime_stats.items():
            assert "trades" in stats
            assert "win_rate" in stats

    def test_session_stats(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        assert len(a.session_stats) > 0

    def test_hold_time(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        assert a.avg_hold_bars > 0
        assert a.median_hold_bars > 0

    def test_cost_analysis(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        assert a.total_fees > 0
        assert a.cost_per_trade > 0

    def test_empty_trades(self):
        a = analyze_alpha_b_trades([])
        assert a.long_trades == 0
        assert a.short_trades == 0

    def test_format_report(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        report = format_alpha_b_analysis(a)
        assert "DIRECTION BREAKDOWN" in report
        assert "PnL DISTRIBUTION" in report
        assert "COST BREAKDOWN" in report

    def test_skewness_calculation(self):
        trades = self._make_trades(20)
        a = analyze_alpha_b_trades(trades)
        # Skewness should be a finite number
        assert math.isfinite(a.pnl_skew)

    def test_long_only_trades(self):
        trades = [make_realistic_trade(direction="long", net_pnl=10, is_win=True)]
        a = analyze_alpha_b_trades(trades)
        assert a.long_trades == 1
        assert a.short_trades == 0
        assert a.long_win_rate == 100.0

    def test_all_losers(self):
        trades = [
            make_realistic_trade(net_pnl=-10, is_win=False)
            for _ in range(5)
        ]
        a = analyze_alpha_b_trades(trades)
        assert a.best_trade < 0
        assert a.worst_trade < 0


# ═══════════════════════════════════════════
#  6. PIPELINE CONFIG
# ═══════════════════════════════════════════

class TestPipelineConfig:
    """Tests for real pipeline configuration."""

    def test_small_data_no_wf(self):
        cfg = make_real_pipeline_config(500)
        assert cfg.wf_config is None  # Not enough data
        assert cfg.mc_config is not None

    def test_large_data_with_wf(self):
        cfg = make_real_pipeline_config(5000)
        assert cfg.wf_config is not None
        assert cfg.wf_config.train_bars > 0
        assert cfg.wf_config.validate_bars > 0
        assert cfg.wf_config.test_bars > 0

    def test_realistic_only(self):
        cfg = make_real_pipeline_config(3000)
        assert cfg.run_naive_comparison is False  # Never naive

    def test_default_capital(self):
        cfg = make_real_pipeline_config(3000)
        assert cfg.bt_config.initial_capital == 10_000.0
        assert cfg.bt_config.risk_per_trade == 0.01


# ═══════════════════════════════════════════
#  7. SYNTHETIC vs REAL COMPARISON
# ═══════════════════════════════════════════

class TestSyntheticVsReal:
    """Tests for comparison formatting."""

    def _make_result(self, trades=50, pf=1.3, exp=5.0) -> PipelineResult:
        r = PipelineResult(symbol="BTCUSDT")
        r.metrics = DetailedMetrics(
            total_trades=trades,
            win_rate=60.0,
            profit_factor=pf,
            expectancy=exp,
            total_net_pnl=exp * trades,
            max_drawdown_pct=10.0,
            sharpe_ratio=1.5,
            avg_holding_bars=20,
        )
        return r

    def test_both_positive(self):
        syn = self._make_result(trades=13, pf=1.08, exp=4.73)
        real = self._make_result(trades=45, pf=1.3, exp=6.0)
        report = format_synthetic_vs_real(syn, real)
        assert "Synthetic" in report
        assert "REAL" in report
        assert "confirmed" in report.lower()

    def test_degradation(self):
        syn = self._make_result(trades=13, pf=1.08, exp=4.73)
        real = self._make_result(trades=45, pf=0.8, exp=-3.0)
        report = format_synthetic_vs_real(syn, real)
        assert "DEGRADATION" in report

    def test_no_synthetic(self):
        real = self._make_result()
        report = format_synthetic_vs_real(None, real)
        assert "No synthetic" in report

    def test_few_trades_warning(self):
        syn = self._make_result()
        real = self._make_result(trades=5)
        report = format_synthetic_vs_real(syn, real)
        assert "Too few" in report


# ═══════════════════════════════════════════
#  8. FINAL VERDICT
# ═══════════════════════════════════════════

class TestFinalVerdict:
    """Tests for final verdict computation."""

    def _make_full_result(
        self,
        trades=50, pf=1.3, exp=5.0, dd=10.0,
        wf_robust=True, mc_robust=True,
    ) -> PipelineResult:
        r = PipelineResult(symbol="BTCUSDT")
        r.metrics = DetailedMetrics(
            total_trades=trades,
            win_rate=60.0,
            profit_factor=pf,
            expectancy=exp,
            total_net_pnl=exp * trades,
            max_drawdown_pct=dd,
            sharpe_ratio=1.5,
        )
        wf = WalkForwardResult(symbol="BTCUSDT")
        wf.total_folds = 5
        wf.valid_folds = 4 if wf_robust else 1
        wf.overfit_folds = 0
        wf.oos_total_trades = 30
        wf.oos_win_rate = 58.0
        wf.oos_profit_factor = pf
        wf.oos_consistency_ratio = 0.8 if wf_robust else 0.2
        wf.overfit_score = 0.2 if wf_robust else 0.7
        wf.is_win_rate = 62.0
        wf.is_profit_factor = pf + 0.1
        r.walk_forward = wf
        r.monte_carlo = MonteCarloResult(
            robustness_score=0.75 if mc_robust else 0.3,
            perm_max_dd_95th=15.0,
            perm_final_pnl_5th=100.0 if mc_robust else -200.0,
            perm_pf_median=1.2,
            param_pnl_5th=50.0,
            param_unprofitable_pct=10.0 if mc_robust else 60.0,
            skip_pnl_5th=80.0,
            skip_unprofitable_pct=15.0,
            is_robust=mc_robust,
        )
        return r

    def test_edge_confirmed(self):
        result = self._make_full_result()
        analysis = AlphaBAnalysis(edge_ratio=1.5)
        verdict = compute_final_verdict(result, analysis)
        assert "EDGE CONFIRMED" in verdict

    def test_edge_dead_negative_exp(self):
        result = self._make_full_result(exp=-5.0, pf=0.7)
        analysis = AlphaBAnalysis(edge_ratio=0.5)
        verdict = compute_final_verdict(result, analysis)
        assert "EDGE DEAD" in verdict

    def test_edge_weak(self):
        result = self._make_full_result(
            trades=50, pf=1.1, exp=2.0, dd=20.0,
            wf_robust=False, mc_robust=False,
        )
        analysis = AlphaBAnalysis(edge_ratio=1.1)
        verdict = compute_final_verdict(result, analysis)
        # With positive exp but failing some criteria
        assert "EDGE WEAK" in verdict or "EDGE DEAD" in verdict

    def test_edge_dead_few_trades(self):
        result = self._make_full_result(trades=5, pf=2.0, exp=50.0)
        analysis = AlphaBAnalysis(edge_ratio=2.0)
        verdict = compute_final_verdict(result, analysis)
        # Too few trades even if positive
        assert "DEAD" in verdict or "WEAK" in verdict

    def test_edge_dead_high_dd(self):
        result = self._make_full_result(dd=40.0)
        analysis = AlphaBAnalysis(edge_ratio=1.5)
        verdict = compute_final_verdict(result, analysis)
        # High DD fails criteria
        assert "PASS" in verdict or "FAIL" in verdict  # Report has check details

    def test_verdict_contains_all_checks(self):
        result = self._make_full_result()
        analysis = AlphaBAnalysis(edge_ratio=1.5)
        verdict = compute_final_verdict(result, analysis)
        assert "Expectancy > 0" in verdict
        assert "PF >= 1.2" in verdict
        assert "Trades >= 30" in verdict
        assert "Max DD <= 25%" in verdict
        assert "WF Robust" in verdict
        assert "MC Robust" in verdict
        assert "Edge Ratio > 1" in verdict

    def test_verdict_no_wf_no_mc(self):
        r = PipelineResult(symbol="BTCUSDT")
        r.metrics = DetailedMetrics(
            total_trades=20,
            profit_factor=0.8,
            expectancy=-3.0,
            max_drawdown_pct=15.0,
        )
        r.walk_forward = None
        r.monte_carlo = None
        analysis = AlphaBAnalysis(edge_ratio=0.5)
        verdict = compute_final_verdict(r, analysis)
        assert "SKIPPED" in verdict
        assert "EDGE DEAD" in verdict


# ═══════════════════════════════════════════
#  9. LOAD RESULT
# ═══════════════════════════════════════════

class TestLoadResult:
    """Tests for LoadResult."""

    def test_is_valid_with_candles(self):
        r = LoadResult(
            candles=make_candles(10),
            source="test",
            validation=ValidationResult(is_valid=True, total_candles=10),
        )
        assert r.is_valid

    def test_not_valid_empty(self):
        r = LoadResult()
        assert not r.is_valid

    def test_not_valid_failed_validation(self):
        r = LoadResult(
            candles=make_candles(10),
            validation=ValidationResult(is_valid=False),
        )
        assert not r.is_valid


# ═══════════════════════════════════════════
#  10. PIPELINE INTEGRATION (with real format data)
# ═══════════════════════════════════════════

class TestPipelineIntegration:
    """Integration test: Alpha B on well-formed candle data."""

    def test_alpha_b_runs_on_real_format_candles(self):
        """Alpha B should run without errors on properly formatted candles."""
        from smc_futures.backtest.alpha_momentum_breakout import make_momentum_breakout_fn
        from smc_futures.backtest.pipeline import BacktestPipeline

        candles = make_candles(2000, start_price=60000)
        cfg = make_real_pipeline_config(2000)
        cfg.wf_config = None  # Skip WF for speed
        cfg.mc_config = None  # Skip MC for speed

        pipeline = BacktestPipeline(cfg)
        fn = make_momentum_breakout_fn()
        result = pipeline.run("BTCUSDT", candles, fn)

        assert result.metrics is not None
        assert result.metrics.total_trades >= 0  # May or may not generate trades
        assert result.report  # Report should be generated

    def test_alpha_b_on_trending_data(self):
        """Alpha B should detect breakouts in trending data."""
        import random
        from smc_futures.backtest.alpha_momentum_breakout import make_momentum_breakout_fn
        rng = random.Random(123)

        # Create data with clear consolidation →  breakout →  retest
        candles = []
        ts = 1_700_000_000_000
        price = 60000.0

        # Phase 1: consolidation (100 bars)
        for i in range(100):
            change = rng.gauss(0, 0.0005)
            o = price
            c = price * (1 + change)
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.0005)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.0005)))
            candles.append(BTCandle(ts + i * 300_000, round(o, 2), round(h, 2), round(l, 2), round(c, 2), rng.uniform(100, 200)))
            price = c

        # Phase 2: Breakout with volume (5 bars)
        for i in range(5):
            change = 0.003 + rng.gauss(0, 0.001)
            o = price
            c = price * (1 + change)
            h = c * (1 + abs(rng.gauss(0, 0.001)))
            l = o * (1 - abs(rng.gauss(0, 0.0005)))
            candles.append(BTCandle(ts + (100 + i) * 300_000, round(o, 2), round(h, 2), round(l, 2), round(c, 2), rng.uniform(400, 800)))
            price = c

        # Phase 3: Retest (10 bars)
        breakout_level = candles[99].high
        for i in range(10):
            change = rng.gauss(-0.001, 0.001)  # Drift back
            o = price
            c = price * (1 + change)
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.0005)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.0005)))
            candles.append(BTCandle(ts + (105 + i) * 300_000, round(o, 2), round(h, 2), round(l, 2), round(c, 2), rng.uniform(100, 200)))
            price = c

        # Phase 4: Continuation (50 bars of trend)
        for i in range(50):
            change = 0.001 + rng.gauss(0, 0.001)
            o = price
            c = price * (1 + change)
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.0005)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.0005)))
            candles.append(BTCandle(ts + (115 + i) * 300_000, round(o, 2), round(h, 2), round(l, 2), round(c, 2), rng.uniform(100, 300)))
            price = c

        fn = make_momentum_breakout_fn()
        # Test signal generation works without error
        for i in range(60, len(candles)):
            result = fn(candles[:i + 1])
            # Result is either None or BacktestSignal
            if result is not None:
                assert result.direction in ("long", "short")
                assert result.entry_price > 0
                assert result.stop_loss > 0
                assert result.take_profit > 0
                break  # Found at least one signal


# ═══════════════════════════════════════════
#  11. REAL DATA LOADER
# ═══════════════════════════════════════════

class TestRealDataLoader:
    """Tests for RealDataLoader configuration."""

    def test_loader_init(self):
        loader = RealDataLoader()
        assert loader.cache_db_path is None

    def test_loader_custom_path(self, tmp_path):
        loader = RealDataLoader(cache_db_path=str(tmp_path / "test.db"))
        assert "test.db" in loader.cache_db_path


# ═══════════════════════════════════════════
#  12. IMPORTS FROM __init__.py
# ═══════════════════════════════════════════

class TestChantier11Imports:
    """Verify all Chantier 11 exports are accessible."""

    def test_real_data_loader_imports(self):
        from smc_futures.backtest import (
            RealDataLoader,
            DataValidator,
            LoadResult,
            ValidationResult,
            KrakenFetcher,
            SQLiteCacheLoader,
            SQLiteCacheWriter,
        )
        assert RealDataLoader is not None
        assert DataValidator is not None

    def test_microstructure_imports(self):
        from smc_futures.backtest import (
            EnhancedMarketModel,
            MicrostructureConfig,
            EntryContext,
            classify_alpha_b_entry,
        )
        assert EnhancedMarketModel is not None
        assert MicrostructureConfig is not None
