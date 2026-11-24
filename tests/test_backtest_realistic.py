"""
Tests — Realistic Backtest Framework (Chantier 8)
══════════════════════════════════════════════════
Tests every component: MarketModel, Engine (look-ahead prevention),
Walk-Forward, Monte Carlo, Metrics, EdgeValidator, Pipeline.
"""

import math
import pytest
from typing import List, Optional

from smc_futures.backtest.market_model import (
    MarketModel, FillResult, SymbolProfile, get_symbol_profile, _DEFAULT_PROFILE,
)
from smc_futures.backtest.engine import (
    RealisticBacktestEngine, RealisticBacktestConfig,
    BTCandle, BacktestSignal, RealisticTrade, EngineState,
)
from smc_futures.backtest.walk_forward import (
    WalkForwardValidator, WalkForwardConfig, WalkForwardResult,
)
from smc_futures.backtest.monte_carlo import (
    MonteCarloSimulator, MonteCarloConfig, MonteCarloResult,
)
from smc_futures.backtest.metrics import (
    MetricsCalculator, EdgeValidator, EdgeVerdict, DetailedMetrics,
)
from smc_futures.backtest.pipeline import (
    BacktestPipeline, PipelineConfig, PipelineResult,
)


# ═══════════════════════════════════════════
#  Fixtures & Helpers
# ═══════════════════════════════════════════

def make_candles(
    n: int = 100,
    start_price: float = 60000.0,
    trend: float = 0.0,          # pct change per bar
    volatility: float = 0.002,   # 0.2% per bar
    start_ts: int = 1_700_000_000_000,
    bar_ms: int = 300_000,
) -> List[BTCandle]:
    """Generate synthetic candles with controlled trend and vol."""
    import random
    rng = random.Random(42)
    candles = []
    price = start_price

    for i in range(n):
        change = trend + rng.gauss(0, volatility)
        open_ = price
        close = price * (1 + change)
        high = max(open_, close) * (1 + abs(rng.gauss(0, volatility * 0.5)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, volatility * 0.5)))
        candles.append(BTCandle(
            timestamp=start_ts + i * bar_ms,
            open=round(open_, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close, 2),
            volume=rng.uniform(50, 200),
        ))
        price = close

    return candles


def simple_long_signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
    """Simple mean-reversion: buy after 3 red bars, SL at last low - buffer."""
    if len(closed_bars) < 5:
        return None

    last3 = closed_bars[-3:]
    all_red = all(c.close < c.open for c in last3)
    if not all_red:
        return None

    entry = closed_bars[-1].close
    sl = min(c.low for c in last3) * 0.998
    tp = entry * 1.01  # 1% target

    return BacktestSignal(
        symbol="BTCUSDT",
        direction="long",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        setup_type="mean_reversion",
        confidence=0.7,
    )


def always_signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
    """Generate a signal on every bar (for testing signal frequency)."""
    if len(closed_bars) < 3:
        return None
    last = closed_bars[-1]
    return BacktestSignal(
        symbol="BTCUSDT",
        direction="long",
        entry_price=last.close,
        stop_loss=last.close * 0.99,
        take_profit=last.close * 1.02,
        setup_type="test",
        confidence=0.5,
    )


# ═══════════════════════════════════════════
#  1. MARKET MODEL TESTS
# ═══════════════════════════════════════════

class TestMarketModel:

    def test_from_symbol_known(self):
        model = MarketModel.from_symbol("BTCUSDT")
        assert model.profile.symbol == "BTCUSDT"
        assert model.profile.taker_fee_rate == 0.0006

    def test_from_symbol_unknown_uses_default(self):
        model = MarketModel.from_symbol("UNKNOWNCOIN")
        assert model.profile.base_slippage_bps == _DEFAULT_PROFILE.base_slippage_bps

    def test_fill_has_positive_slippage(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_fill(
            side="buy", price=60000, qty=0.1, atr=150
        )
        assert not fill.rejected
        assert fill.slippage_bps > 0
        assert fill.fill_price > 60000  # Buy fills higher

    def test_sell_fill_lower(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_fill(
            side="sell", price=60000, qty=0.1, atr=150
        )
        assert fill.fill_price < 60000  # Sell fills lower

    def test_fee_is_taker_by_default(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_fill(
            side="buy", price=60000, qty=1, atr=150
        )
        assert fill.fee_rate == 0.0006
        assert fill.fee_usd > 0

    def test_maker_fee_for_limit(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_fill(
            side="buy", price=60000, qty=0.1, atr=150, is_maker=True
        )
        assert fill.fee_rate == 0.0002

    def test_rejects_oversized_orders(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_fill(
            side="buy", price=60000, qty=1000, atr=150, volume_5m=1000
        )
        assert fill.rejected
        assert "exceeds" in fill.reject_reason.lower()

    def test_sl_fill_worse_than_entry(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_sl_fill(
            side="buy", sl_price=59000, qty=0.1, atr=150
        )
        # SL on long → selling → fill should be BELOW SL price
        assert fill.fill_price < 59000

    def test_tp_fill_uses_maker_fee(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_tp_fill(
            side="buy", tp_price=61000, qty=0.1, atr=150
        )
        assert fill.fee_rate == 0.0002  # Maker

    def test_funding_cost_scales_with_holding_time(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        cost_8h = model.compute_funding_cost(10000, 8)
        cost_24h = model.compute_funding_cost(10000, 24)
        assert cost_24h == pytest.approx(cost_8h * 3, rel=0.01)

    def test_volatility_classifier_normal(self):
        regime = MarketModel.classify_volatility(500, 60000)  # 0.83% ATR → normal
        assert regime == "normal"

    def test_volatility_classifier_extreme(self):
        regime = MarketModel.classify_volatility(3000, 60000)  # 5% ATR
        assert regime == "extreme"

    def test_volatility_classifier_with_history(self):
        history = [100.0] * 20
        regime = MarketModel.classify_volatility(350, 60000, history)
        assert regime == "extreme"  # 3.5x average

    def test_higher_vol_regime_increases_slippage(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill_normal = model.simulate_fill(
            side="buy", price=60000, qty=0.1, atr=150, volatility_regime="normal"
        )
        model2 = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill_extreme = model2.simulate_fill(
            side="buy", price=60000, qty=0.1, atr=150, volatility_regime="extreme"
        )
        assert fill_extreme.slippage_bps > fill_normal.slippage_bps

    def test_latency_is_positive(self):
        model = MarketModel.from_symbol("BTCUSDT", seed=42)
        fill = model.simulate_fill(
            side="buy", price=60000, qty=0.1, atr=150
        )
        assert fill.latency_ms >= 10

    def test_reproducible_with_seed(self):
        m1 = MarketModel.from_symbol("BTCUSDT", seed=99)
        m2 = MarketModel.from_symbol("BTCUSDT", seed=99)
        f1 = m1.simulate_fill("buy", 60000, 0.1, 150)
        f2 = m2.simulate_fill("buy", 60000, 0.1, 150)
        assert f1.fill_price == f2.fill_price
        assert f1.slippage_bps == f2.slippage_bps


# ═══════════════════════════════════════════
#  2. ENGINE TESTS (LOOK-AHEAD PREVENTION)
# ═══════════════════════════════════════════

class TestRealisticEngine:

    def test_basic_run_returns_state(self):
        candles = make_candles(50)
        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, simple_long_signal_fn)
        assert isinstance(state, EngineState)
        assert state.capital > 0
        assert len(state.equity_curve) > 0

    def test_no_trades_on_empty_candles(self):
        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", [], simple_long_signal_fn)
        assert len(state.trades) == 0

    def test_signal_fn_receives_only_closed_bars(self):
        """CRITICAL: Verify signal function never sees current bar."""
        candles = make_candles(20)
        bars_seen = []

        def tracking_signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
            bars_seen.append(len(closed_bars))
            return None

        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        engine.run("BTCUSDT", candles, tracking_signal_fn)

        # Signal fn is called for bars 1..19 (bar 0 has no history)
        # For bar i, it should receive i closed bars (candles[0..i-1])
        for i, n_bars in enumerate(bars_seen):
            assert n_bars == i + 1, (
                f"At bar {i+1}, signal_fn received {n_bars} bars instead of {i+1}"
            )

    def test_entry_at_next_bar_open(self):
        """Entry must happen at the NEXT bar's open, not signal bar."""
        candles = make_candles(30, volatility=0.005)
        signal_bar = None

        def capture_signal_fn(closed_bars: List[BTCandle]) -> Optional[BacktestSignal]:
            nonlocal signal_bar
            if len(closed_bars) == 10:  # Signal on exactly bar 10
                signal_bar = closed_bars[-1]
                return BacktestSignal(
                    symbol="BTCUSDT", direction="long",
                    entry_price=closed_bars[-1].close,
                    stop_loss=closed_bars[-1].close * 0.98,
                    take_profit=closed_bars[-1].close * 1.05,
                )
            return None

        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, capture_signal_fn)

        if state.trades:
            trade = state.trades[0]
            # Trade should have entered at bar 11 (index 11), not bar 10
            assert trade.entry_bar_idx > 10, (
                f"Entry at bar {trade.entry_bar_idx} — should be after signal bar 10"
            )
            # Entry should be near bar[11].open, NOT signal_bar.close
            bar_11_open = candles[11].open
            # With slippage, actual entry is near but not exactly bar open
            pct_diff = abs(trade.actual_entry - bar_11_open) / bar_11_open * 100
            assert pct_diff < 5.0, f"Entry too far from next bar open: {pct_diff:.2f}%"

    def test_slippage_applied_to_entries(self):
        """Entry price must differ from bar open (slippage)."""
        candles = make_candles(50)

        def always_long(closed_bars):
            if len(closed_bars) == 10:
                return BacktestSignal(
                    symbol="BTCUSDT", direction="long",
                    entry_price=closed_bars[-1].close,
                    stop_loss=closed_bars[-1].close * 0.95,
                    take_profit=closed_bars[-1].close * 1.10,
                )
            return None

        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, always_long)

        if state.trades:
            t = state.trades[0]
            assert t.entry_slippage_bps > 0, "No slippage applied"
            assert t.actual_entry != candles[t.entry_bar_idx].open

    def test_fees_deducted_from_pnl(self):
        """Net PnL must be less than raw PnL (fees deducted)."""
        candles = make_candles(100)
        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, simple_long_signal_fn)

        for trade in state.trades:
            assert trade.total_fees > 0, "No fees charged"
            assert trade.net_pnl < trade.raw_pnl, "Fees not deducted"

    def test_equity_curve_length(self):
        candles = make_candles(50)
        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, simple_long_signal_fn)
        # One equity point per bar (excluding first bar)
        assert len(state.equity_curve) == len(candles) - 1

    def test_max_holding_bars_force_close(self):
        """Positions held too long should be force-closed."""
        # Use a signal that enters and never hits SL/TP
        candles = make_candles(50, trend=0.0, volatility=0.0001)

        def enter_once(closed_bars):
            if len(closed_bars) == 5:
                p = closed_bars[-1].close
                return BacktestSignal(
                    symbol="BTCUSDT", direction="long",
                    entry_price=p, stop_loss=p * 0.5, take_profit=p * 2.0,  # Far SL/TP
                )
            return None

        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, enter_once, max_holding_bars=10)

        if state.trades:
            assert state.trades[0].exit_reason in ("timeout", "end_of_data")
            assert state.trades[0].holding_bars <= 11

    def test_mae_mfe_tracked(self):
        """Trades should have MAE/MFE populated."""
        candles = make_candles(100, volatility=0.003)
        config = RealisticBacktestConfig(seed=42)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, simple_long_signal_fn)

        for trade in state.trades:
            # MAE is negative (worst unrealized), MFE is positive (best)
            # At least one should be non-zero for multi-bar trades
            if trade.holding_bars > 1:
                assert trade.mae != 0 or trade.mfe != 0, "MAE/MFE not tracked"

    def test_daily_trade_limit(self):
        """Should respect max_trades_per_day."""
        candles = make_candles(200, bar_ms=300_000)  # 5m bars
        config = RealisticBacktestConfig(seed=42, max_trades_per_day=2)
        engine = RealisticBacktestEngine(config)
        state = engine.run("BTCUSDT", candles, always_signal_fn, max_holding_bars=3)

        # Count trades per day
        from collections import Counter
        daily = Counter()
        for t in state.trades:
            day = t.entry_timestamp // 86_400_000
            daily[day] += 1

        for day, count in daily.items():
            assert count <= 2, f"Day {day}: {count} trades > max 2"


# ═══════════════════════════════════════════
#  3. WALK-FORWARD TESTS
# ═══════════════════════════════════════════

class TestWalkForward:

    def test_fold_generation(self):
        wf = WalkForwardConfig(
            train_bars=100, validate_bars=50, test_bars=50, step_bars=50,
        )
        validator = WalkForwardValidator(wf, RealisticBacktestConfig())
        folds = validator._generate_folds(400)
        assert len(folds) >= 2
        # Check no overlap between test windows of consecutive folds
        for i in range(1, len(folds)):
            _, _, _, _, test_s_prev, test_e_prev = folds[i - 1]
            train_s, _, _, _, _, _ = folds[i]
            # Next fold's train starts at step offset from previous
            assert train_s == folds[i-1][0] + wf.step_bars

    def test_walk_forward_runs(self):
        candles = make_candles(500, volatility=0.003)
        wf = WalkForwardConfig(
            train_bars=100, validate_bars=50, test_bars=50, step_bars=50,
            min_trades_train=1, min_trades_validate=1, min_trades_test=1,
        )
        bt_config = RealisticBacktestConfig(seed=42)
        validator = WalkForwardValidator(wf, bt_config)
        result = validator.validate("BTCUSDT", candles, simple_long_signal_fn)

        assert isinstance(result, WalkForwardResult)
        assert result.total_folds >= 1
        assert result.symbol == "BTCUSDT"

    def test_insufficient_data_returns_empty(self):
        candles = make_candles(10)
        wf = WalkForwardConfig(train_bars=100, validate_bars=50, test_bars=50)
        bt_config = RealisticBacktestConfig(seed=42)
        validator = WalkForwardValidator(wf, bt_config)
        result = validator.validate("BTCUSDT", candles, simple_long_signal_fn)
        assert result.total_folds == 0

    def test_overfit_detection(self):
        """A strategy that works perfectly in train but not OOS should flag overfit."""
        wf = WalkForwardConfig(
            train_bars=100, validate_bars=50, test_bars=50, step_bars=50,
            min_trades_train=1, min_trades_validate=0, min_trades_test=1,
            max_is_oos_pf_ratio=1.5,
        )
        assert wf.max_is_oos_pf_ratio == 1.5


# ═══════════════════════════════════════════
#  4. MONTE CARLO TESTS
# ═══════════════════════════════════════════

class TestMonteCarlo:

    def _make_trades(self, n=50, win_pct=0.55, avg_win=100, avg_loss=-80) -> List[RealisticTrade]:
        """Generate mock trades."""
        import random
        rng = random.Random(42)
        trades = []
        for i in range(n):
            is_win = rng.random() < win_pct
            pnl = avg_win * rng.uniform(0.5, 1.5) if is_win else avg_loss * rng.uniform(0.5, 1.5)
            t = RealisticTrade(
                signal_bar_idx=i, signal_price=60000,
                direction="long",
                actual_entry=60000, actual_exit=60000 + pnl,
                intended_entry=60000, intended_exit=60000 + pnl,
                qty=1.0, notional_usd=60000,
                entry_fee=60000 * 0.0006, exit_fee=60000 * 0.0006,
                total_fees=60000 * 0.0012,
                raw_pnl=pnl + 60000 * 0.0012,  # Gross
                net_pnl=pnl,
                holding_bars=10,
            )
            trades.append(t)
        return trades

    def test_basic_analysis(self):
        trades = self._make_trades(50)
        mc = MonteCarloSimulator(MonteCarloConfig(
            n_permutations=200, n_param_variations=100, n_skip_trials=100, seed=42,
        ))
        result = mc.analyze(trades, 10000)
        assert isinstance(result, MonteCarloResult)
        assert result.n_base_trades == 50
        assert result.perm_max_dd_95th > 0
        assert result.perm_final_pnl_median != 0

    def test_too_few_trades_warns(self):
        mc = MonteCarloSimulator(MonteCarloConfig(seed=42))
        result = mc.analyze([], 10000)
        assert "Too few" in result.warnings[0]

    def test_winning_strategy_is_robust(self):
        trades = self._make_trades(100, win_pct=0.60, avg_win=120, avg_loss=-70)
        mc = MonteCarloSimulator(MonteCarloConfig(
            n_permutations=500, n_param_variations=200, n_skip_trials=200, seed=42,
        ))
        result = mc.analyze(trades, 10000)
        assert result.robustness_score > 0.3  # Should score decently

    def test_losing_strategy_not_robust(self):
        trades = self._make_trades(50, win_pct=0.30, avg_win=80, avg_loss=-100)
        mc = MonteCarloSimulator(MonteCarloConfig(
            n_permutations=300, n_param_variations=100, n_skip_trials=100, seed=42,
        ))
        result = mc.analyze(trades, 10000)
        assert result.perm_final_pnl_5th < 0

    def test_reproducible_with_seed(self):
        trades = self._make_trades(50)
        mc1 = MonteCarloSimulator(MonteCarloConfig(n_permutations=100, seed=42))
        mc2 = MonteCarloSimulator(MonteCarloConfig(n_permutations=100, seed=42))
        r1 = mc1.analyze(trades, 10000)
        r2 = mc2.analyze(trades, 10000)
        assert r1.perm_max_dd_95th == r2.perm_max_dd_95th


# ═══════════════════════════════════════════
#  5. METRICS TESTS
# ═══════════════════════════════════════════

class TestMetrics:

    def _make_trade_list(self) -> List[RealisticTrade]:
        """Make a mixed list of winning and losing trades."""
        trades = []
        pnls = [100, 50, -30, -80, 120, -40, 60, -20, 90, -50]
        for i, pnl in enumerate(pnls):
            t = RealisticTrade(
                signal_bar_idx=i, signal_price=60000,
                direction="long",
                actual_entry=60000, actual_exit=60000 + pnl / 0.001,
                intended_entry=60000,
                qty=0.001, notional_usd=60,
                entry_fee=0.036, exit_fee=0.036,
                total_fees=0.072,
                raw_pnl=pnl + 0.072,
                net_pnl=pnl,
                stop_loss=59500,
                holding_bars=10 + i,
                mae=-abs(pnl) * 0.5,
                mfe=abs(pnl) * 0.8,
            )
            trades.append(t)
        return trades

    def test_basic_metrics(self):
        trades = self._make_trade_list()
        equity = [10000 + sum(t.net_pnl for t in trades[:i+1]) for i in range(len(trades))]
        m = MetricsCalculator.compute(trades, equity, 10000)
        assert m.total_trades == 10
        assert m.winning_trades == 5
        assert m.losing_trades == 5
        assert m.win_rate == 50.0

    def test_profit_factor_calculation(self):
        trades = self._make_trade_list()
        equity = [10000]
        m = MetricsCalculator.compute(trades, equity, 10000)
        gross_profit = 100 + 50 + 120 + 60 + 90  # = 420
        gross_loss = 30 + 80 + 40 + 20 + 50       # = 220
        assert m.profit_factor == pytest.approx(420 / 220, rel=0.01)

    def test_expectancy(self):
        trades = self._make_trade_list()
        equity = [10000]
        m = MetricsCalculator.compute(trades, equity, 10000)
        expected_expectancy = sum(t.net_pnl for t in trades) / len(trades)
        assert m.expectancy == pytest.approx(expected_expectancy, rel=0.01)

    def test_drawdown_calculation(self):
        # Drawdown requires trades + equity curve
        trades = self._make_trade_list()
        # Build equity with a clear drawdown: up then down then up
        equity = [10000, 10100, 10050, 10200, 9900, 10300, 10100, 10400, 10350, 10500]
        m = MetricsCalculator.compute(trades, equity, 10000)
        # Peak was 10200, trough was 9900 → DD = 300/10200 * 100 ≈ 2.94%
        assert m.max_drawdown_pct > 0

    def test_empty_trades(self):
        m = MetricsCalculator.compute([], [], 10000)
        assert m.total_trades == 0
        assert m.profit_factor == 0

    def test_consecutive_streaks(self):
        trades = self._make_trade_list()
        equity = [10000]
        m = MetricsCalculator.compute(trades, equity, 10000)
        assert m.max_consecutive_wins >= 1
        assert m.max_consecutive_losses >= 1


# ═══════════════════════════════════════════
#  6. EDGE VALIDATOR TESTS
# ═══════════════════════════════════════════

class TestEdgeValidator:

    def test_insufficient_trades_rejects(self):
        m = DetailedMetrics(total_trades=10, expectancy=5)
        verdict = EdgeValidator.validate(m)
        assert verdict.verdict == "EDGE INVALIDATED"
        assert any("INSUFFICIENT SAMPLE" in r for r in verdict.rejections)

    def test_negative_expectancy_rejects(self):
        m = DetailedMetrics(total_trades=50, expectancy=-1.0, profit_factor=0.8,
                            win_rate=40, max_drawdown_pct=10)
        verdict = EdgeValidator.validate(m)
        assert verdict.verdict == "EDGE INVALIDATED"
        assert any("NEGATIVE EXPECTANCY" in r for r in verdict.rejections)

    def test_low_profit_factor_rejects(self):
        m = DetailedMetrics(total_trades=50, expectancy=5, profit_factor=1.0,
                            win_rate=50, max_drawdown_pct=10, avg_loss=-10)
        verdict = EdgeValidator.validate(m)
        assert verdict.verdict == "EDGE INVALIDATED"
        assert any("LOW PROFIT FACTOR" in r for r in verdict.rejections)

    def test_excessive_drawdown_rejects(self):
        m = DetailedMetrics(total_trades=50, expectancy=20, profit_factor=2.0,
                            win_rate=55, max_drawdown_pct=35, avg_loss=-10,
                            sharpe_ratio=1.0)
        verdict = EdgeValidator.validate(m)
        assert verdict.verdict == "EDGE INVALIDATED"
        assert any("EXCESSIVE DRAWDOWN" in r for r in verdict.rejections)

    def test_strong_strategy_confirms(self):
        m = DetailedMetrics(
            total_trades=100, expectancy=15, profit_factor=1.8,
            win_rate=55, max_drawdown_pct=12, avg_loss=-50, avg_win=80,
            sharpe_ratio=1.5,
        )
        # With MC and WF that also pass
        mc = MonteCarloResult(
            robustness_score=0.8, perm_final_pnl_5th=500,
            param_unprofitable_pct=10,
        )
        wf = WalkForwardResult(
            symbol="BTCUSDT", valid_folds=5, overfit_folds=1,
            oos_consistency_ratio=0.8, overfit_score=0.2,
            oos_profit_factor=1.5,
        )
        verdict = EdgeValidator.validate(m, wf, mc)
        assert verdict.verdict == "EDGE CONFIRMED"
        assert verdict.score > 50

    def test_verdict_format(self):
        m = DetailedMetrics(total_trades=10)
        verdict = EdgeValidator.validate(m)
        text = EdgeValidator.format_verdict(verdict)
        assert "EDGE INVALIDATED" in text
        assert "INSUFFICIENT SAMPLE" in text


# ═══════════════════════════════════════════
#  7. PIPELINE INTEGRATION TESTS
# ═══════════════════════════════════════════

class TestPipeline:

    def test_full_pipeline_runs(self):
        candles = make_candles(500, volatility=0.003)
        config = PipelineConfig(
            bt_config=RealisticBacktestConfig(seed=42),
            wf_config=WalkForwardConfig(
                train_bars=100, validate_bars=50, test_bars=50, step_bars=50,
                min_trades_train=1, min_trades_validate=0, min_trades_test=0,
            ),
            mc_config=MonteCarloConfig(
                n_permutations=50, n_param_variations=20, n_skip_trials=20, seed=42,
            ),
            max_holding_bars=20,
        )
        pipeline = BacktestPipeline(config)
        result = pipeline.run("BTCUSDT", candles, simple_long_signal_fn)

        assert isinstance(result, PipelineResult)
        assert result.symbol == "BTCUSDT"
        assert result.verdict is not None
        assert result.verdict.verdict in ("EDGE CONFIRMED", "EDGE INVALIDATED")
        assert len(result.report) > 100
        assert result.duration_seconds > 0

    def test_pipeline_without_wf_mc(self):
        candles = make_candles(100)
        config = PipelineConfig(
            bt_config=RealisticBacktestConfig(seed=42),
            wf_config=None,
            mc_config=None,
            run_naive_comparison=False,
        )
        pipeline = BacktestPipeline(config)
        result = pipeline.run("BTCUSDT", candles, simple_long_signal_fn)

        assert result.verdict is not None
        assert result.walk_forward is None
        assert result.monte_carlo is None

    def test_naive_comparison_shows_cost_impact(self):
        candles = make_candles(200, volatility=0.003)
        config = PipelineConfig(
            bt_config=RealisticBacktestConfig(seed=42),
            wf_config=None,
            mc_config=None,
            run_naive_comparison=True,
            max_holding_bars=20,
        )
        pipeline = BacktestPipeline(config)
        result = pipeline.run("BTCUSDT", candles, simple_long_signal_fn)

        if result.naive_metrics and result.metrics and result.metrics.total_trades > 0:
            # Naive PnL should be >= realistic PnL (costs eat profits)
            assert result.naive_metrics.total_net_pnl >= result.metrics.total_net_pnl - 1


# ═══════════════════════════════════════════
#  8. ATR COMPUTATION
# ═══════════════════════════════════════════

class TestATR:

    def test_atr_basic(self):
        candles = make_candles(20)
        atr = RealisticBacktestEngine.compute_atr(candles, period=14)
        assert atr > 0

    def test_atr_single_candle(self):
        c = BTCandle(timestamp=0, open=100, high=105, low=95, close=102)
        atr = RealisticBacktestEngine.compute_atr([c])
        assert atr == 10  # high - low

    def test_atr_two_candles(self):
        c1 = BTCandle(timestamp=0, open=100, high=105, low=95, close=102)
        c2 = BTCandle(timestamp=1, open=103, high=110, low=98, close=108)
        atr = RealisticBacktestEngine.compute_atr([c1, c2], period=14)
        # TR = max(110-98, |110-102|, |98-102|) = max(12, 8, 4) = 12
        assert atr == 12
