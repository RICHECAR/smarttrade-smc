"""
Tests Chantier 16 — Stress, Sensitivity, Walk-Forward, Safeguards, Live Readiness
═════════════════════════════════════════════════════════════════════════════════
"""

import numpy as np
import pandas as pd
import pytest


def _make_prices(n_days=500, n_assets=10, seed=42) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    tickers = [f"ASSET_{i}" for i in range(n_assets)] + ["SPY", "BND"]

    data = {}
    for i, t in enumerate(tickers):
        drift = 0.0003 * (i - n_assets // 2)
        vol = 0.01 + 0.003 * i
        returns = drift + vol * rng.randn(n_days)
        data[t] = 100.0 * np.exp(np.cumsum(returns))

    return pd.DataFrame(data, index=dates)


@pytest.fixture
def prices():
    return _make_prices()


# ══════════════════════════════════════════════════════════════════
# STRESS TESTS
# ══════════════════════════════════════════════════════════════════

class TestStressTests:
    def test_sub_periods_returns_results(self, prices):
        from portfolio.stress_test import stress_sub_periods, _config_score_top5
        results = stress_sub_periods(prices, _config_score_top5, "test")
        assert len(results) >= 1
        for r in results:
            assert r.name.startswith("period_")
            assert isinstance(r.sharpe, float)

    def test_universe_dropout(self, prices):
        from portfolio.stress_test import stress_universe_dropout, _config_score_top5
        results = stress_universe_dropout(prices, _config_score_top5, "test", n_trials=2)
        assert len(results) == 2

    def test_cost_variation(self, prices):
        from portfolio.stress_test import stress_costs, _config_score_top5
        results = stress_costs(prices, _config_score_top5, "test")
        assert len(results) == 5
        # Higher costs should generally reduce Sharpe
        sharpes = [r.sharpe for r in results]
        # At minimum, zero-cost should be >= highest-cost
        assert sharpes[0] >= sharpes[-1] - 0.5  # Allow some noise

    def test_top_n_variation(self, prices):
        from portfolio.stress_test import stress_top_n, _config_score_top5
        results = stress_top_n(prices, _config_score_top5, "test")
        assert len(results) == 5

    def test_weight_variations(self, prices):
        from portfolio.stress_test import stress_weight_variations, _config_score_top5
        results = stress_weight_variations(prices, _config_score_top5, "test")
        assert len(results) == 5

    def test_format_results(self, prices):
        from portfolio.stress_test import stress_costs, _config_score_top5, format_stress_results
        results = stress_costs(prices, _config_score_top5, "test")
        text = format_stress_results(results)
        assert "STRESS TEST" in text
        assert "Win rate" in text


# ══════════════════════════════════════════════════════════════════
# SENSITIVITY
# ══════════════════════════════════════════════════════════════════

class TestSensitivity:
    def test_momentum_sensitivity(self, prices):
        from portfolio.sensitivity import sensitivity_momentum_weights
        from portfolio.backtest import BacktestConfig
        from portfolio.allocator import AllocatorConfig, WeightMethod

        cfg = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=3, max_weight=0.40, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", cost_bps=5.0, warmup=150,
        )
        df = sensitivity_momentum_weights(prices, cfg)
        assert len(df) > 0
        assert "sharpe" in df.columns
        assert "mom_63" in df.columns

    def test_topn_maxweight_sensitivity(self, prices):
        from portfolio.sensitivity import sensitivity_topn_maxweight
        from portfolio.backtest import BacktestConfig
        from portfolio.allocator import AllocatorConfig, WeightMethod

        cfg = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=3, max_weight=0.40, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", cost_bps=5.0, warmup=150,
        )
        df = sensitivity_topn_maxweight(prices, cfg)
        assert len(df) > 0

    def test_text_heatmap(self):
        from portfolio.sensitivity import text_heatmap
        df = pd.DataFrame({
            "row": [1, 1, 2, 2],
            "col": [10, 20, 10, 20],
            "val": [0.5, 0.8, 0.6, 0.7],
        })
        text = text_heatmap(df, "row", "col", "val", "Test Heatmap")
        assert "Test Heatmap" in text
        assert "Best=" in text


# ══════════════════════════════════════════════════════════════════
# WALK-FORWARD
# ══════════════════════════════════════════════════════════════════

class TestWalkForward:
    def test_walk_forward_runs(self, prices):
        from portfolio.walk_forward import run_walk_forward
        wf = run_walk_forward(prices, train_days=200, test_days=100, step_days=80, warmup=80)
        assert "folds" in wf
        assert len(wf["folds"]) >= 1
        assert "avg_oos_sharpe" in wf

    def test_walk_forward_no_leakage(self, prices):
        """Test dates don't overlap: train_end < test_start."""
        from portfolio.walk_forward import run_walk_forward
        wf = run_walk_forward(prices, train_days=200, test_days=100, step_days=80, warmup=80)
        for f in wf["folds"]:
            assert f["train_end"] < f["test_start"]

    def test_format_walk_forward(self, prices):
        from portfolio.walk_forward import run_walk_forward, format_walk_forward
        wf = run_walk_forward(prices, train_days=200, test_days=100, step_days=80, warmup=80)
        text = format_walk_forward(wf)
        assert "WALK-FORWARD" in text
        assert "Avg OOS Sharpe" in text


# ══════════════════════════════════════════════════════════════════
# BENCHMARKS
# ══════════════════════════════════════════════════════════════════

class TestBenchmarks:
    def test_buy_hold(self, prices):
        from portfolio.benchmarks import benchmark_buy_hold
        m = benchmark_buy_hold(prices, "SPY")
        assert m is not None
        assert m.cagr != 0.0

    def test_equal_weight_universe(self, prices):
        from portfolio.benchmarks import benchmark_equal_weight_universe
        m = benchmark_equal_weight_universe(prices)
        assert m.cagr != 0.0

    def test_top10_naive(self, prices):
        from portfolio.benchmarks import benchmark_top10_naive
        m = benchmark_top10_naive(prices, lookback=63)
        assert isinstance(m.sharpe, float)

    def test_60_40(self, prices):
        from portfolio.benchmarks import benchmark_60_40
        m = benchmark_60_40(prices)
        assert m is not None

    def test_dual_momentum(self, prices):
        from portfolio.benchmarks import benchmark_dual_momentum
        m = benchmark_dual_momentum(prices, lookback=63)
        assert isinstance(m.sharpe, float)

    def test_format_benchmarks(self, prices):
        from portfolio.benchmarks import run_all_benchmarks, format_benchmarks
        from portfolio.metrics import MetricResult
        benchmarks = run_all_benchmarks(prices)
        strat = MetricResult(cagr=0.15, sharpe=1.0, sortino=1.2, max_drawdown=0.10,
                             calmar=1.5, annual_vol=0.12, total_return=0.60, avg_turnover=0.30)
        text = format_benchmarks(benchmarks, strat)
        assert "BENCHMARK" in text


# ══════════════════════════════════════════════════════════════════
# SAFEGUARDS
# ══════════════════════════════════════════════════════════════════

class TestSafeguards:
    def test_data_freshness_ok(self, prices):
        from portfolio.safeguards import check_data_freshness
        passed, msg = check_data_freshness(prices, reference_date=prices.index[-1])
        assert passed

    def test_data_freshness_stale(self, prices):
        from portfolio.safeguards import check_data_freshness, SafeguardConfig
        cfg = SafeguardConfig(max_data_age_days=1)
        future = prices.index[-1] + pd.Timedelta(days=10)
        passed, msg = check_data_freshness(prices, reference_date=future, config=cfg)
        assert not passed
        assert "BLOCKER" in msg

    def test_valid_tickers(self, prices):
        from portfolio.safeguards import check_valid_tickers
        passed, msg = check_valid_tickers(prices)
        assert passed

    def test_valid_tickers_insufficient(self):
        from portfolio.safeguards import check_valid_tickers, SafeguardConfig
        cfg = SafeguardConfig(min_valid_tickers=100)
        tiny = pd.DataFrame({"A": [1, 2], "B": [3, 4]})
        passed, msg = check_valid_tickers(tiny, config=cfg)
        assert not passed

    def test_scores_ok(self):
        from portfolio.safeguards import check_scores
        scores = pd.Series({"A": 0.8, "B": 0.6, "C": 0.4, "D": 0.3, "E": 0.2,
                            "F": 0.7, "G": 0.5, "H": 0.35, "I": 0.25, "J": 0.15})
        passed, msg = check_scores(scores)
        assert passed

    def test_scores_nan_saturation(self):
        from portfolio.safeguards import check_scores, SafeguardConfig
        cfg = SafeguardConfig(max_nan_pct=0.10)
        scores = pd.Series([np.nan] * 8 + [0.5, 0.3])
        passed, msg = check_scores(scores, config=cfg)
        assert not passed

    def test_scores_no_signal(self):
        from portfolio.safeguards import check_scores, SafeguardConfig
        cfg = SafeguardConfig(min_score_range=0.1)
        scores = pd.Series([0.50, 0.50, 0.50, 0.50, 0.50,
                            0.50, 0.50, 0.50, 0.50, 0.50])
        passed, msg = check_scores(scores, config=cfg)
        assert not passed

    def test_weights_concentration(self):
        from portfolio.safeguards import check_weights, SafeguardConfig
        cfg = SafeguardConfig(max_single_weight=0.30)
        weights = {"A": 0.50, "B": 0.30, "C": 0.20}
        passed, msg = check_weights(weights, config=cfg)
        assert not passed

    def test_turnover_cap(self):
        from portfolio.safeguards import check_turnover, SafeguardConfig
        cfg = SafeguardConfig(max_turnover_per_rebal=0.30)
        old = {"A": 0.50, "B": 0.50}
        new = {"C": 0.50, "D": 0.50}
        passed, msg = check_turnover(old, new, config=cfg)
        assert not passed  # 100% turnover

    def test_run_all_safeguards(self, prices):
        from portfolio.safeguards import run_all_safeguards
        scores = pd.Series({c: 0.5 + 0.02 * i for i, c in enumerate(prices.columns)})
        weights = {"SPY": 0.20, "ASSET_0": 0.20, "ASSET_1": 0.20}
        sg = run_all_safeguards(prices, scores, weights, reference_date=prices.index[-1])
        assert isinstance(sg.passed, bool)
        assert "data_freshness" in sg.checks


# ══════════════════════════════════════════════════════════════════
# LIVE READINESS
# ══════════════════════════════════════════════════════════════════

class TestLiveReadiness:
    def test_portfolio_state(self):
        from portfolio.live_readiness import PortfolioState
        state = PortfolioState.from_weights({"AAPL": 0.30, "MSFT": 0.20}, total_value=100_000)
        assert state.total_value == 100_000
        assert abs(state.cash - 50_000) < 1
        assert state.positions["AAPL"].value == 30_000

    def test_rebalance_plan(self):
        from portfolio.live_readiness import PortfolioState, compute_rebalance_plan
        current = PortfolioState.from_weights({"AAPL": 0.30, "MSFT": 0.20}, total_value=100_000)
        target = {"AAPL": 0.20, "GOOGL": 0.25, "MSFT": 0.10}
        plan = compute_rebalance_plan(current, target, total_value=100_000)

        assert len(plan.actions) >= 3
        sells = [a for a in plan.actions if a.action == "SELL"]
        buys = [a for a in plan.actions if a.action == "BUY"]
        assert len(sells) >= 1  # AAPL should decrease, MSFT should decrease
        assert len(buys) >= 1  # GOOGL should be bought

    def test_order_report(self):
        from portfolio.live_readiness import PortfolioState, compute_rebalance_plan, generate_order_report
        current = PortfolioState.from_weights({}, total_value=50_000)
        target = {"SPY": 0.30, "QQQ": 0.20, "AAPL": 0.15}
        plan = compute_rebalance_plan(current, target, total_value=50_000)
        report = generate_order_report(plan)
        assert "REBALANCE ORDER PLAN" in report
        assert "BUY" in report


# ══════════════════════════════════════════════════════════════════
# TURNOVER ANALYSIS
# ══════════════════════════════════════════════════════════════════

class TestTurnoverAnalysis:
    def test_turnover_stats(self):
        from portfolio.backtest import run_backtest, BacktestConfig
        from portfolio.allocator import AllocatorConfig, WeightMethod
        from portfolio.turnover_analysis import compute_turnover_stats

        prices = _make_prices(n_days=400, n_assets=8, seed=55)
        config = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=3, max_weight=0.40, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", warmup=150, cost_bps=5.0,
        )
        result = run_backtest(prices, config)
        stats = compute_turnover_stats(result)
        assert stats["n_rebalances"] > 0
        assert stats["annual_turnover"] > 0

    def test_position_stability(self):
        from portfolio.backtest import run_backtest, BacktestConfig
        from portfolio.allocator import AllocatorConfig, WeightMethod
        from portfolio.turnover_analysis import compute_position_stability

        prices = _make_prices(n_days=400, n_assets=8, seed=55)
        config = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=3, max_weight=0.40, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", warmup=150, cost_bps=5.0,
        )
        result = run_backtest(prices, config)
        stats = compute_position_stability(result)
        assert stats["unique_tickers_held"] > 0

    def test_tax_impact(self):
        from portfolio.backtest import run_backtest, BacktestConfig
        from portfolio.allocator import AllocatorConfig, WeightMethod
        from portfolio.turnover_analysis import estimate_tax_impact

        prices = _make_prices(n_days=400, n_assets=8, seed=55)
        config = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=3, max_weight=0.40, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", warmup=150, cost_bps=5.0,
        )
        result = run_backtest(prices, config)
        tax = estimate_tax_impact(result)
        assert 0 <= tax["blended_tax_rate"] <= 0.5


# ══════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════

class TestReporting:
    def test_institutional_report(self):
        from portfolio.backtest import run_backtest, BacktestConfig
        from portfolio.allocator import AllocatorConfig, WeightMethod
        from portfolio.reporting import generate_institutional_report

        prices = _make_prices(n_days=500, n_assets=8, seed=77)
        config = BacktestConfig(
            allocator_config=AllocatorConfig(top_n=3, max_weight=0.40, method=WeightMethod.SCORE_WEIGHTED),
            rebal_freq="M", warmup=150, cost_bps=5.0,
        )
        result = run_backtest(prices, config)
        report = generate_institutional_report(result, prices)
        assert "INSTITUTIONAL" in report
        assert "PERFORMANCE SUMMARY" in report
        assert "MONTHLY ANALYSIS" in report
        assert "DRAWDOWN" in report

    def test_rolling_sharpe(self):
        from portfolio.reporting import rolling_sharpe
        rng = np.random.RandomState(0)
        rets = pd.Series(rng.randn(300) * 0.01, index=pd.bdate_range("2020-01-01", periods=300))
        rs = rolling_sharpe(rets, window=63)
        assert len(rs) > 100

    def test_hit_ratio(self):
        from portfolio.reporting import hit_ratio_monthly
        rng = np.random.RandomState(0)
        rets = pd.Series(0.001 + rng.randn(500) * 0.01, index=pd.bdate_range("2020-01-01", periods=500))
        hr = hit_ratio_monthly(rets)
        assert 0 < hr <= 1.0
